"""Catalog state and background-process coordination."""
from collections import deque
import json
import os
from pathlib import Path
import subprocess
import uuid

import bpy
import PIL

from . import catalog, layers, previews, search, svn_authors
from .appearance import tr
from .paths import CatalogPaths, absolute, find_svn, readable_path
from .shared_data import FAVORITES, IGNORE_LIST, SharedCatalog, legacy_metadata_directory

WORKER_STATUS = {
    "scan": "Indexing textures…",
    "author": "Reading SVN history…",
    "authors": "Caching SVN authors…",
    "strip": "Loading gallery thumbnails…",
    "preview": "Loading texture preview…",
    "preview_exr": "Loading texture preview…",
    "layer_cover": "Preparing 4L preview…",
    "gallery_cover": "Preparing large cover…",
}


def author_error(message):
    if "E731001" in message or "E731002" in message:
        return tr("SVN host not found.")
    if any(code in message for code in ("E170001", "E215004", "E175013")):
        return tr("SVN access denied.")
    if "E170013" in message or "timed out" in message:
        return tr("SVN server unreachable.")
    return tr(message)


def preferences(context):
    return context.preferences.addons[__package__].preferences


def stamp(path):
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return None


def clean_metadata(value):
    clean = dict(value)
    tags = clean.get("tags")
    if "tags" in clean:
        clean["tags"] = [tag for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else []
    if not isinstance(clean.get("name"), str):
        clean.pop("name", None)
    layers = clean.get("layers")
    if "layers" in clean:
        clean["layers"] = [layer for layer in layers if isinstance(layer, str)] if isinstance(layers, list) else []
    return clean


class State:
    def __init__(self, config, *, auto_index=True, index_on_open=True):
        paths = CatalogPaths.from_preferences(config)
        self.root, self.cache, self.shared = paths.root, paths.cache, paths.covers
        self.exchange = SharedCatalog(paths.exchange, legacy_metadata_directory(self.root, self.shared))
        self.svn = find_svn(config, self.cache)
        self.shader = str(absolute(config.shader_path)) if config.shader_path else ""
        self.auto_index = auto_index
        self.entries = {}
        self.metadata = {}
        self.tag_vocabulary = []
        self.favorites = set()
        self.ignored = set()
        self.collections = {}
        self.presets = {}
        self.shared_presets = {}
        self.authors = {}
        self.author_errors = {}
        self.map_previews = {}
        self.map_thumbnails = {}
        self.gallery_covers = {}
        self.queue = deque()
        self.job = None
        self.author_queue = deque()
        self.author_job = None
        self.status = ""
        self.stamps = None
        self.cover_current = False
        self.search_fields = {}
        migration_error = ""
        try:
            self.exchange.migrate_local_collections(self.cache / "collections.json",
                                                    self.cache / "shared-collections-migrated.json")
        except (OSError, TypeError, ValueError, KeyError) as error:
            migration_error = str(error)
        self.refresh()
        if migration_error:
            self.status = migration_error
        if auto_index and index_on_open and not self.cover_current:
            self.index()
        for key in self.presets if auto_index else ():
            if key in self.entries:
                entry = self.entries[key]
                preview = entry.get("preset_preview")
                if not preview or not (self.cache / preview).is_file():
                    self.request_preset_cover(key, entry)

    @property
    def metadata_file(self):
        return self.exchange.tags_file

    @property
    def presets_file(self):
        return self.cache / "layer-presets.json"

    def refresh(self):
        index = self.cache / catalog.COVER_INDEX
        favorites_file = self.cache / "favorites.json"
        authors_file = self.cache / svn_authors.CACHE_FILE
        paths = (index, favorites_file, authors_file, self.presets_file, *self.exchange.watched_paths(),
                 *(self.cache / name for name in catalog.LEGACY_INDEXES))
        current = tuple((path, stamp(path)) for path in paths)
        if current == self.stamps:
            return False
        try:
            data = catalog.read_index(self.cache)
            entries = data.get("entries", []) if data.get("root") == os.path.normcase(str(self.root)) else []
            self.cover_current = (data.get("root") == os.path.normcase(str(self.root))
                                  and data.get("catalog_schema") == catalog.CATALOG_SCHEMA
                                  and data.get("cover_schema") == previews.SCHEMA
                                  and data.get("cover_root") == os.path.normcase(str(self.shared)))
            shared = self.exchange.read_tags()
            collections = self.exchange.read_collections()
            ignored = self.exchange.read_ignored()
            self.entries = {entry["id"]: entry for entry in entries}
            def indexing_settings(entries):
                return {key: (value.get("layers"), value.get("shader_type")) for key, value in entries.items()
                        if value.get("layers") or value.get("shader_type")}

            old_layers = indexing_settings(self.metadata)
            self.metadata = {key: clean_metadata(value) for key, value in shared["materials"].items()
                             if isinstance(value, dict)}
            for entry in entries:
                if entry["id"] not in self.metadata:
                    alternate = next((self.metadata[key] for key in entry.get("aliases", ()) if key in self.metadata), None)
                    if alternate is not None:
                        self.metadata[entry["id"]] = alternate
                entry["kind"] = catalog.material_kind(entry, self.metadata.get(entry["id"], {}))
            new_layers = indexing_settings(self.metadata)
            if self.auto_index and self.stamps is not None and old_layers != new_layers:
                self.index()
            self.favorites = set(catalog.read_json(favorites_file, []))
            self.favorites.update(entry["id"] for entry in entries
                                  if self.favorites.intersection(entry.get("aliases", ())))
            self.authors = catalog.read_json(authors_file, {})
            self.presets = catalog.read_json(self.presets_file, {})
            self.shared_presets = {}
            for _, _, recipes in collections.values():
                for key, recipe in recipes.items():
                    self.shared_presets.setdefault(key, recipe)
            saved_entries = {}
            source_aliases = {alias: (entry["id"], next(iter(entry["groups"])))
                              for entry in entries for alias in entry.get("aliases", ())}
            recipes = {**self.presets, **self.shared_presets}
            for key, preset in reversed(tuple(recipes.items())):
                try:
                    refs = [source_aliases.get(source, (source, group)) if source not in self.entries else (source, group)
                            for source, group in preset["refs"]]
                    record = layers.compose(self.entries, refs, key, preset["name"])
                    first = self.entries[refs[0][0]]
                except (KeyError, TypeError, ValueError):
                    continue
                record["thumb"] = first.get("thumb")
                record["preview_type"] = first.get("preview_type")
                record["preset_preview"] = self.presets.get(key, {}).get("preview")
                saved_entries[key] = record
                if key not in self.shared_presets:
                    self.metadata[key] = clean_metadata({"tags": preset.get("tags", [])})
                if key in self.presets and self.presets[key].get("favorite", True):
                    self.favorites.add(key)
            self.entries = {**saved_entries, **self.entries}
            preset_tags = {tag for key in saved_entries for tag in self.metadata.get(key, {}).get("tags", ())}
            self.tag_vocabulary = sorted(preset_tags.union(shared["tags"]))
            self.ignored = ignored
            self.ignored.update(source_aliases[alias][0] for alias in tuple(ignored) if alias in source_aliases)
            self.collections = {key: (name, members) for key, (name, members, _) in collections.items()}
            for _, members in self.collections.values():
                members.update(source_aliases[alias][0] for alias in tuple(members) if alias in source_aliases)
            self.search_fields = {}
            self.status = (tr("{count} materials", count=len(self.entries)) if self.entries
                           else tr("Click Index to scan this catalog."))
        except (OSError, TypeError, ValueError, AttributeError, KeyError) as error:
            self.status = str(error)
        self.stamps = current
        return True

    def collection_options(self, include_ignore=True):
        custom = sorted(self.collections.items(), key=lambda item: item[1][0].casefold())
        options = [(FAVORITES, "Favorites")]
        if include_ignore:
            options.append((IGNORE_LIST, "Ignore List"))
        return [*options, *((key, name) for key, (name, _) in custom)]

    def collection_members(self, key):
        if key == FAVORITES:
            return self.favorites
        if key == IGNORE_LIST:
            return self.ignored
        return self.collections.get(key, ("", set()))[1]

    def create_collection(self, name):
        key = self.exchange.create_collection(name)
        self.refresh()
        return key

    def toggle_collection_material(self, collection, key):
        if collection == FAVORITES:
            self.toggle_favorite(key)
            return
        recipe = self.shared_presets.get(key) or self.presets.get(key)
        self.exchange.toggle_member(collection, key, self.entries[key].get("aliases", ()), recipe)
        self.refresh()

    def set_ignored(self, key, hidden):
        self.exchange.set_ignored(key, self.entries[key].get("aliases", ()), hidden)
        self.refresh()

    def toggle_favorite(self, key):
        if key in self.presets:
            self.update_preset(key, {"favorite": key not in self.favorites})
            return
        favorite_file = self.cache / "favorites.json"
        favorites = set(catalog.read_json(favorite_file, []))
        aliases = set(self.entries[key].get("aliases", ()))
        if key in self.favorites:
            favorites.difference_update(aliases | {key})
        else:
            favorites.add(key)
        catalog.write_json(self.cache / "favorites.json", sorted(favorites))
        self.refresh()

    def update_preset(self, key, fields):
        current = catalog.read_json(self.presets_file, {})
        current[key].update(fields)
        catalog.write_json(self.presets_file, current)
        self.refresh()

    def save_preset(self, name, refs, collection=FAVORITES):
        name = name.strip()
        if not name:
            raise ValueError("Enter a name for the 4L material.")
        current = catalog.read_json(self.presets_file, {})
        if any(preset["name"].casefold() == name.casefold()
               for preset in (*current.values(), *self.shared_presets.values())):
            raise ValueError("A saved 4L material already uses that name.")
        key = f"preset::{uuid.uuid4().hex}"
        record = layers.compose(self.entries, refs, key, name)
        recipe = {"name": name, "refs": record["layer_refs"]}
        if collection == FAVORITES:
            current[key] = {**recipe, "favorite": True, "tags": []}
            catalog.write_json(self.presets_file, current)
        else:
            self.exchange.toggle_member(collection, key, (), recipe)
        self.refresh()
        if collection == FAVORITES:
            self.request_preset_cover(key, record)
        return key

    def request_preset_cover(self, key, record):
        recipe = self.cache / "layer-recipes" / f"{key.split('::', 1)[1]}.json"
        catalog.write_json(recipe, record)
        self.enqueue("layer_cover", key, str(self.root), str(self.cache), str(recipe))

    def save_metadata(self, key, expected, fields):
        if key in self.presets and key not in self.shared_presets:
            if self.metadata.get(key, {}) != expected:
                raise ValueError("Preset tags changed. Reopen the editor before saving.")
            self.update_preset(key, fields)
            return
        # Detect another edit or an SVN update that arrived while the dialog was open.
        current = self.exchange.read_tags()
        materials = current["materials"]
        alternate = next((materials[alias] for alias in self.entries[key].get("aliases", ()) if alias in materials), {})
        actual = materials.get(key, alternate)
        if not isinstance(actual, dict) or clean_metadata(actual) != expected:
            raise ValueError("Shared metadata changed. Reopen the editor before saving.")
        materials[key] = {**actual, **fields}
        self.exchange.write_tags(current)
        self.refresh()

    def matches(self, query, category, tag, collection):
        terms = search.parse(query)
        tag_filter = search.normalize(tag)
        ranked = []
        members = self.collection_members(FAVORITES if collection is True else collection) if collection else None
        ignored_view = collection == IGNORE_LIST
        for key, entry in self.entries.items():
            if (key in self.ignored) != ignored_view:
                continue
            data = self.metadata.get(key, {})
            tags = data.get("tags", [])
            if category and entry["category"] != category or tag and not any(
                    search.normalize(candidate) == tag_filter for candidate in tags):
                continue
            if members is not None and key not in members:
                continue
            if not terms:
                yield entry
                continue
            if key not in self.search_fields:
                name = data.get("name") or entry["name"]
                self.search_fields[key] = search.fields(name, tags, f'{entry["name"]} {key} {entry["kind"]}')
            if score := search.rank(terms, self.search_fields[key]):
                ranked.append((score, entry))
        for _, entry in sorted(ranked, key=lambda item: -item[0]):
            yield entry

    def enqueue(self, operation, key, *args, priority=False):
        request = operation, key, args
        queue, job = (self.author_queue, self.author_job) if operation in {"author", "authors"} else (self.queue, self.job)
        if request not in queue and (job is None or job[0] != request):
            if priority:
                queue.appendleft(request)
            else:
                queue.append(request)

    @property
    def busy(self):
        return bool(self.job or self.queue or self.author_job or self.author_queue)

    @property
    def indexing(self):
        return ((self.job is not None and self.job[0][0] == "scan")
                or any(request[0] == "scan" for request in self.queue))

    def index(self):
        self.enqueue("scan", "", str(self.root), str(self.cache), str(self.shared), str(self.exchange.directory))

    def cover(self, entry):
        if entry.get("preset_preview"):
            return readable_path(self.cache / entry["preset_preview"])
        relative = entry.get("thumb")
        if relative:
            storage = self.shared if entry.get("preview_type") in {"generated", "placeholder"} else self.cache
            return readable_path(storage / relative)
        return None

    def author(self, entry):
        key = entry["id"]
        if entry.get("layer_refs"):
            return
        target = svn_authors.material_path(entry)
        if not target:
            self.author_errors[key] = "Material has no source texture."
            self.status = tr(self.author_errors[key])
            return
        self.author_errors.pop(key, None)
        self.enqueue("author", key, self.svn, str(catalog.source_path(self.root, target)), str(self.cache),
                     "1" if bpy.app.online_access else "0")

    def populate_authors(self):
        if not self.entries:
            self.status = tr("Index the library first.")
            return
        self.enqueue("authors", "", self.svn, str(self.root), str(self.cache),
                     "1" if bpy.app.online_access else "0")

    def preview(self, relative):
        if not self.map_previews.get(relative, {}).get("path"):
            self.map_previews.pop(relative, None)
            operation = "preview_exr" if Path(relative).suffix.casefold() == ".exr" else "preview"
            self.enqueue(operation, relative, str(self.root), str(self.cache), relative, "1024", priority=True)

    def preview_strip(self, relatives):
        needed = [path for path in dict.fromkeys(relatives)
                  if path not in self.map_thumbnails and path not in self.map_previews]
        if needed:
            self.map_thumbnails.update(dict.fromkeys(needed, {}))
            self.enqueue("strip", "", str(self.root), str(self.cache), json.dumps(needed))

    def gallery_cover(self, entry):
        key = entry["id"]
        if key not in self.gallery_covers:
            recipe = {"record": {field: entry[field] for field in ("kind", "groups")},
                      "metadata": self.metadata.get(key, {})}
            self.enqueue("gallery_cover", key, str(self.root), str(self.cache), json.dumps(recipe))

    def advance(self):
        redraw = self.refresh()
        author_changed = self.advance_worker(self.author_queue, "author_job")
        previews_changed = self.advance_worker(self.queue, "job")
        return redraw or author_changed or previews_changed

    def advance_worker(self, queue, job_name):
        redraw = False
        job = getattr(self, job_name)
        if job is not None:
            request, process, log, handle = job
            if process.poll() is None:
                if request[0] == "scan":
                    try:
                        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
                        progress = next((line for line in reversed(lines) if line.startswith("PROGRESS ")), "")
                    except OSError:
                        progress = ""
                    if progress:
                        label = tr("Catalog previews  {progress}", progress=progress.removeprefix("PROGRESS "))
                        if label != self.status:
                            self.status = label
                            redraw = True
                return redraw
            setattr(self, job_name, None)
            handle.close()
            operation, key, _ = request
            try:
                output = log.read_text(encoding="utf-8", errors="replace")
                if process.returncode:
                    raise ValueError(output.strip().splitlines()[-1] if output.strip() else "Background operation failed.")
                if operation == "author":
                    result = json.loads(output)
                    if "error" in result:
                        raise ValueError(result["error"])
                    path = self.cache / svn_authors.CACHE_FILE
                    catalog.write_json(path, {**catalog.read_json(path, {}), key: result})
                if operation == "preview":
                    self.map_previews[key] = json.loads(output)
                if operation == "preview_exr":
                    preview = next((line.removeprefix("RTC_PREVIEW ") for line in output.splitlines()
                                    if line.startswith("RTC_PREVIEW ")), None)
                    if preview is None:
                        raise ValueError("Blender did not return the OpenEXR preview.")
                    self.map_previews[key] = json.loads(preview)
                if operation == "strip":
                    self.map_thumbnails.update(json.loads(output))
                if operation == "gallery_cover":
                    result = json.loads(output)
                    self.gallery_covers[key] = self.cache / result["thumb"] if result["preview_type"] == "generated" else None
                if operation == "layer_cover" and key in self.presets:
                    self.update_preset(key, {"preview": json.loads(output)["thumb"]})
                if operation in {"scan", "author", "authors"}:
                    self.refresh()
                if operation == "scan":
                    result = json.loads(output.strip().splitlines()[-1])
                    self.status = (tr("{count} materials", count=result["materials"]) + " · "
                                   + tr("{count} new previews", count=result["created"]))
                    if result["placeholders"]:
                        self.status += " · " + tr("{count} unreadable", count=result["placeholders"])
                if operation == "authors":
                    result = json.loads(output)
                    self.status = (author_error(result["error"]) if result.get("error") else
                                   tr("SVN authors: {found}/{total}", found=result["authors"], total=result["total"]))
                elif self.status == WORKER_STATUS[operation]:
                    self.status = tr("{count} materials", count=len(self.entries))
            except (OSError, ValueError) as error:
                self.status = str(error)
                if operation == "author":
                    self.author_errors[key] = str(error)
                    self.status = author_error(str(error))
                if operation in {"preview", "preview_exr"}:
                    self.map_previews[key] = {"error": str(error)}
            finally:
                log.unlink(missing_ok=True)
            redraw = True
        if queue:
            request = queue.popleft()
            operation, _, args = request
            self.cache.mkdir(parents=True, exist_ok=True)
            log = self.cache / f".rtc_worker_{os.getpid()}_{id(self)}_{job_name}.log"
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            dependency = str(Path(PIL.__file__).resolve().parent.parent)
            worker = str(Path(__file__).with_name("worker.py"))
            py = Path(bpy.app.binary_path).parent / f"{bpy.app.version[0]}.{bpy.app.version[1]}" / "python/bin/python.exe"
            if operation == "preview_exr":
                command = [bpy.app.binary_path, "--background", "--factory-startup", "--threads", "2",
                           "--python-exit-code", "1", "--python", worker, "--", dependency, "texture_preview.py", *args]
            else:
                command = [str(py), "-I", "-X", "utf8", worker, dependency, "catalog.py", operation, *args]
            handle = log.open("wb")
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
                process = subprocess.Popen(command, env=env, stdout=handle, stderr=subprocess.STDOUT,
                                           creationflags=flags)
            except OSError as error:
                handle.close()
                log.unlink(missing_ok=True)
                self.status = str(error)
            else:
                setattr(self, job_name, (request, process, log, handle))
                self.status = WORKER_STATUS[operation]
            redraw = True
        return redraw

    def close(self):
        for job_name in ("job", "author_job"):
            if job := getattr(self, job_name):
                _, process, log, handle = job
                if process.poll() is None:
                    process.terminate()
                handle.close()
                try:
                    log.unlink(missing_ok=True)
                except PermissionError:
                    pass
                setattr(self, job_name, None)
        self.queue.clear()
        self.author_queue.clear()
