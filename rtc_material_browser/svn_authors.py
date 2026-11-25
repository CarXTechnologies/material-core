"""Cache the original SVN upload of each material's source texture."""
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

if __package__:
    from . import catalog
else:
    import catalog


CACHE_FILE = "authors-first-upload.json"


def material_path(entry):
    groups = [group["maps"] for group in entry["groups"].values()]
    return next((paths[0] for maps in groups for channel, paths in maps.items()
                 if channel == "Diffuse" and paths), None) or next(
        (paths[0] for maps in groups for channel, paths in maps.items() if channel != "Preview" and paths), None)


def command(svn, *args, output=subprocess.PIPE, timeout=45):
    result = subprocess.run([svn, "--non-interactive", *args], stdout=output,
                            stderr=subprocess.PIPE, timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        error = " ".join(result.stderr.decode("utf-8", errors="replace").splitlines()).strip()
        raise ValueError(error or "SVN returned an error.")
    return result.stdout


def repository_path(url, root_url):
    root = unquote(urlsplit(root_url).path).rstrip("/")
    return unquote(urlsplit(url).path)[len(root):]


def revision_author(element):
    return {"revision": int(element.attrib["revision"]), "author": element.findtext("author") or ""}


def additions(xml, targets):
    result = {}
    tracked = {}
    watchers = {}

    def track(key, path, peg):
        tracked[key] = path, peg
        for parent in (path, *map(str, PurePosixPath(path).parents)):
            watchers.setdefault(parent, set()).add(key)

    for path, materials in targets.items():
        for key, peg in materials:
            track(key, path, peg)

    stack = []
    for event, node in ET.iterparse(xml, events=("start", "end")):
        if event == "start":
            stack.append(node)
            continue
        if node.tag == "logentry":
            entry_revision = int(node.attrib["revision"])
            changes = [change for change in node.findall("paths/path")
                       if change.text in watchers and change.get("action") in {"A", "R", "D"}]
            handled = set()
            # Explicit texture changes take precedence over any ancestor copy in the same revision.
            for change in sorted(changes, key=lambda item: len(item.text), reverse=True):
                source = change.text
                for key in tuple(watchers[source]):
                    if key in handled or key not in tracked:
                        continue
                    target, peg = tracked[key]
                    if entry_revision > peg or not (source == target or target.startswith(source + "/")):
                        continue
                    handled.add(key)
                    if origin := change.get("copyfrom-path"):
                        track(key, origin + target[len(source):], int(change.attrib["copyfrom-rev"]))
                    else:
                        if source == target and change.get("action") in {"A", "R"}:
                            result[key] = revision_author(node)
                        del tracked[key]
            if len(stack) > 1:
                stack[-2].remove(node)
        stack.pop()
    return result


def first_add(svn, path):
    entry = ET.fromstring(command(svn, "info", "--xml", "--", path + "@")).find("entry")
    target = repository_path(entry.findtext("url"), entry.findtext("repository/root"))
    with tempfile.TemporaryFile() as log:
        command(svn, "log", "--xml", "-v", "--", path + "@", output=log)
        log.seek(0)
        result = additions(log, {target: [("material", int(entry.attrib["revision"]))]})
    return result.get("material", {"error": "SVN history has no original Add for this texture."})


def populate(svn, root, cache):
    cache = Path(cache)
    entries = catalog.read_index(cache).get("entries", [])
    keys = {}
    for entry in entries:
        if path := material_path(entry):
            keys.setdefault(path, []).append(entry["id"])
    info = ET.fromstring(command(svn, "info", "--xml", "--depth", "infinity", "--", root + "@", timeout=120))
    library = info.find("entry")
    url, repo = library.findtext("url"), library.findtext("repository/root")
    targets = {}
    root_path = Path(root).resolve()
    for item in info.findall("entry"):
        relative = Path(item.attrib["path"]).relative_to(root_path).as_posix()
        if relative in keys and item.get("kind") == "file":
            revisions = [(key, int(item.attrib["revision"])) for key in keys[relative]]
            target = repository_path(item.findtext("url"), repo)
            targets.setdefault(target, []).extend(revisions)
    try:
        peg = max((peg for revisions in targets.values() for _, peg in revisions), default=1)
        with tempfile.TemporaryFile() as log:
            command(svn, "log", "--xml", "-v", "--stop-on-copy", f"{url}@{peg}", output=log, timeout=600)
            log.seek(0)
            authors = additions(log, targets)
    except (ValueError, subprocess.TimeoutExpired, ET.ParseError) as error:
        return {"error": str(error)}
    path = cache / CACHE_FILE
    catalog.write_json(path, {**catalog.read_json(path, {}), **authors})
    return {"authors": len(authors), "total": len(entries)}
