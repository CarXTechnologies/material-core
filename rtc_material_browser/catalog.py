"""Filesystem index and preview worker. This module never imports bpy."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
from subprocess import TimeoutExpired
import sys
from xml.etree.ElementTree import ParseError

from PIL import Image

IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".tga", ".tif", ".tiff", ".webp", ".bmp", ".exr"}
IGNORED_DIRS = {"proxy", "proxies", ".svn", ".rtc_material_browser"}
CHANNELS = {
    "base_color": "Diffuse", "basecolor": "Diffuse", "base_colour": "Diffuse", "albedo": "Diffuse",
    "diffuse": "Diffuse", "diifuse": "Diffuse", "normal_opengl": "Normal", "normal_ogl": "Normal",
    "normalgl": "Normal", "normal": "Normal", "nrm": "Normal",
    "roughness": "Roughness", "rougness": "Roughness", "rough": "Roughness",
    "opacity_blend": "Alpha", "opacity_test": "Alpha", "opacity_b": "Alpha", "opacity": "Alpha", "alpha": "Alpha",
    "ambient_occlusion": "aoMap", "aomap": "aoMap", "ao": "aoMap",
    "metalness": "Metalic", "metallic": "Metalic", "emissive": "Emission",
    "emission": "Emission", "emmission": "Emission", "preview": "Preview",
}
SUFFIX = re.compile(r"^(.+?)[_ -]+(" + "|".join(key.replace("_", "[_ -]+") for key in CHANNELS)
                    + r")(?:[_ -]+(\d+))?$", re.I)
COPY_SUFFIX = re.compile(r"[ _-]+copy(?:[ _-]+\d+)?$", re.I)
VARIANT_SUFFIX = re.compile(r"\s*\((\d+)\)$")
AUXILIARY = re.compile(r"_(?:height|displacement|occlusion|glossiness|gloss|bump)(?:$|_)", re.I)
FOUR_LAYER = re.compile(r"(?:^|[_ /-])(?:4l|4[_ -]?layers?|layered|four[_ -]?layers?)(?:$|[_ /-])", re.I)
DECAL = re.compile(r"(?:^|[_ /-])(?:decals?|graffiti|stickers?|dt|du)(?:$|[_ /-])", re.I)
COVER_INDEX = "index-material-core-library.json"
LEGACY_INDEXES = ("index-material-core-alpha.json", "index-material-core.json", "index-covers-v2.json", "index-covers.json", "index.json")
CATALOG_SCHEMA = 4


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_index(cache):
    for name in (COVER_INDEX, *LEGACY_INDEXES):
        value = read_json(cache / name, None)
        if value is not None:
            return value
    return {}


def source_path(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Path is outside the textures library: {relative}")
    return path


def thumbnail_target(root, cache, relative, edge):
    path = source_path(root, relative)
    stat = path.stat()
    token = f"{path}|{stat.st_size}|{stat.st_mtime_ns}|thumb-{edge}-v1"
    return path, cache / "thumbs" / (hashlib.sha256(token.encode()).hexdigest() + ".png")


def thumbnail(root, cache, relative, edge=1024):
    if __package__:
        from .previews import source_image
    else:
        from previews import source_image
    path, target = thumbnail_target(root, cache, relative, edge)
    if target.is_file():
        with Image.open(target) as cached:
            return target, cached.info.get("source_size", "")
    from PIL.PngImagePlugin import PngInfo
    if path.suffix.casefold() == ".exr":
        raise ValueError(f"{path.name}: OpenEXR needs Blender to decode it.")
    image, size = source_image(path, edge=edge)
    info = PngInfo()
    info.add_text("source_size", size)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f".{os.getpid()}.tmp")
    try:
        image.save(temporary, format="PNG", pnginfo=info)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target, size


def map_token(filename):
    original = Path(filename).stem
    stem = COPY_SUFFIX.sub("", original).strip()
    is_copy = original != stem
    variant = VARIANT_SUFFIX.search(stem)
    if variant:
        stem = stem[:variant.start()].rstrip()
    alternate = re.fullmatch(r"(.+?)[_ -]+(?:normal(?:gl)?|nrm)(?:[_ -]+(?:var|ver|v)?[_ -]*(\d+)|"
                             r"(\d+)|\([^)]*\)|[_ -]+(?:dirt|directx|dx|dx11))", stem, re.I)
    if alternate:
        index = alternate[2] or alternate[3] or (variant[1] if variant else None)
        return alternate[1], "Normal", int(index) if index is not None else None, (True, True)
    normalized = re.sub(r"[ -]+", "_", stem)
    match = SUFFIX.fullmatch(stem)
    if not match:
        return (stem, "Diffuse", int(variant[1]) if variant else None, (is_copy, True)) if not AUXILIARY.search(normalized) else None
    name, suffix, index = match.groups()
    # Prefer an explicit canonical channel over an unsuffixed or duplicated export.
    suffix = re.sub(r"[ -]+", "_", suffix.casefold())
    channel = CHANNELS[suffix]
    rank = is_copy, suffix not in {channel.casefold(), "normal_opengl"}
    return name, channel, int(index) if index is not None else int(variant[1]) if variant else None, rank


def texture_groups(folder, files):
    candidates = []
    for filename in sorted(files, key=str.casefold):
        file = Path(filename)
        if file.suffix.casefold() not in IMAGE_TYPES or file.stem.casefold().endswith(".proxy"):
            continue
        token = map_token(filename)
        if token:
            candidates.append((*token, filename))
    indexed_colors = {(name.casefold(), index) for name, channel, index, _, _ in candidates if channel == "Diffuse"}
    groups = {}
    priorities = {}
    variant_bases = {}
    for name, channel, index, rank, filename in candidates:
        if index is not None and (name.casefold(), index) in indexed_colors:
            base = name.casefold()
            name = f"{name}_{index}"
            variant_bases[name.casefold()] = base
            rank = (*rank, 0)
        else:
            rank = (*rank, index + 1 if index is not None else 0)
        key = name.casefold()
        group = groups.setdefault(key, {"name": name, "maps": {}})
        relative = (Path(folder) / filename).as_posix()
        group["maps"].setdefault(channel, []).append(relative)
        priorities[relative] = rank
    for group in groups.values():
        for maps in group["maps"].values():
            maps.sort(key=lambda path: (priorities[path], path.casefold()))
    for variant, base in variant_bases.items():
        if base in groups:
            for channel, candidates in groups[base]["maps"].items():
                if channel not in {"Diffuse", "Preview"}:
                    groups[variant]["maps"].setdefault(channel, candidates)
    return {key: group for key, group in groups.items() if "Diffuse" in group["maps"]}


def four_layer_folder(folder, groups):
    keys = set(groups)
    numbered = {key.lstrip("0") for key in keys if key.isdigit()}
    return (bool(FOUR_LAYER.search(folder)) or {"1", "2", "3", "4"} <= numbered
            or ("base" in keys and len(numbered & {"1", "2", "3"}) >= 2)
            or {"black", "red", "green", "blue"} <= keys)


def named_layer_sets(groups):
    layers = {}
    for key, group in groups.items():
        match = re.fullmatch(r"(.+?)[_ -]+(?:layer[_ -]*)?(base|0?[1-9][0-9]*)", key)
        if match and FOUR_LAYER.search(match[1]):
            layers.setdefault(match[1], {})[key] = group
    return {prefix: group for prefix, group in layers.items() if len(group) >= 2}


def material_record(folder, category, name, groups, material_id, kind):
    anchor = folder if material_id == folder or name.casefold() == Path(folder).name.casefold() else ""
    return {"id": material_id, "folder": folder, "category": category, "name": name,
            "kind": kind, "groups": groups, "anchor": anchor}


def alpha_info(root, relative, previous):
    path = root / relative
    try:
        stat = path.stat()
        signature = [relative, stat.st_size, stat.st_mtime_ns]
        old = previous.get(relative)
        if old is not None and old["signature"] == signature:
            return old
        transparent = False
        with Image.open(path) as image:
            if "A" in image.getbands():
                transparent = image.getchannel("A").getextrema()[0] < 255
            elif "transparency" in image.info:
                transparent = image.convert("RGBA").getchannel("A").getextrema()[0] < 255
        return {"signature": signature, "transparent": transparent}
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def annotate_alpha(entries, previous, root):
    old = {info["signature"][0]: info for entry in previous.values() for group in entry["groups"].values()
           if (info := group.get("diffuse_alpha"))}
    groups = {id(group): group for entry in entries for group in entry["groups"].values()}
    sources = {group["maps"]["Diffuse"][0] for group in groups.values() if not group["maps"].get("Alpha")}
    sources = sorted(path for path in sources if Path(path).suffix.lower() not in {".jpg", ".jpeg"})
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = dict(zip(sources, workers.map(lambda path: alpha_info(root, path, old), sources)))
    for group in groups.values():
        if not group["maps"].get("Alpha") and (info := results.get(group["maps"]["Diffuse"][0])):
            group["diffuse_alpha"] = info


def has_alpha(group):
    return bool(group.get("opacity_alpha", {}).get("transparent", bool(group["maps"].get("Alpha")))
                or group.get("diffuse_alpha", {}).get("transparent"))


def annotate_opacity_maps(entries, previous, root):
    old = {value["signature"][0]: value for entry in previous.values() for group in entry["groups"].values()
           if (value := group.get("opacity_alpha"))}

    def inspect(relative):
        try:
            path = root / relative
            stat = path.stat()
            signature = [relative, stat.st_size, stat.st_mtime_ns]
            if relative in old and old[relative]["signature"] == signature:
                return old[relative]
            with Image.open(path) as image:
                if image.mode.startswith("I;16"):
                    transparent = image.getextrema()[0] < 65535
                else:
                    transparent = image.convert("L").getextrema()[0] < 255
            return {"signature": signature, "transparent": transparent}
        except (OSError, ValueError, Image.DecompressionBombError):
            return None

    groups = {id(group): group for entry in entries
              for group in entry["groups"].values() if group["maps"].get("Alpha")}
    paths = sorted({group["maps"]["Alpha"][0] for group in groups.values()})
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = dict(zip(paths, workers.map(inspect, paths)))
    for group in groups.values():
        if value := results[group["maps"]["Alpha"][0]]:
            group["opacity_alpha"] = value


def scan(root, cache, shared, progress=None, exchange=None):
    if __package__:
        from . import previews
        from .shared_data import FOLDER_NAME, SharedCatalog, legacy_metadata_directory
    else:
        import previews
        from shared_data import FOLDER_NAME, SharedCatalog, legacy_metadata_directory
    if not root.is_dir():
        raise ValueError(f"Library directory does not exist: {root}")
    index = cache / COVER_INDEX
    data = read_index(cache)
    root_key = os.path.normcase(str(root))
    previous = {entry["id"]: entry for entry in data.get("entries", [])} if data.get("root") == root_key else {}
    exchange = exchange or root / FOLDER_NAME
    metadata = SharedCatalog(exchange, legacy_metadata_directory(root, shared)).read_tags()["materials"]
    entries = []
    for directory, directories, files in os.walk(root):
        parent = Path(directory)
        directories[:] = sorted(d for d in directories if d.casefold() not in IGNORED_DIRS
                               and parent / d not in {shared, exchange} and d.casefold() != FOLDER_NAME.casefold())
        folder = parent.relative_to(root).as_posix()
        category = folder.split("/", 1)[0] if folder != "." else "Root"
        groups = texture_groups(folder, files)
        if not groups:
            continue
        if four_layer_folder(folder, groups):
            override = metadata.get(folder, {})
            kind = (override.get("shader_type") or "4L-mat") if isinstance(override, dict) else "4L-mat"
            kind = kind if kind in {"Standart", "4L-mat", "Decal"} else "4L-mat"
            selected = next((key for key in (parent.name.casefold(), "base", "1") if key in groups), next(iter(groups)))
            numeric = {key: group for key, group in groups.items() if key == "base" or key.isdigit()}
            colored = {key: groups[key] for key in ("black", "red", "green", "blue") if key in groups}
            pack = groups if FOUR_LAYER.search(folder) else colored if len(colored) == 4 else numeric
            entries.append(material_record(folder, category, parent.name,
                                           pack if kind == "4L-mat" else {selected: groups[selected]}, folder, kind))
            groups = {key: group for key, group in groups.items() if key not in pack}
        folder_groups = groups.copy()
        for prefix, pack in named_layer_sets(groups).items():
            identifier = f"{folder}::4l::{prefix}"
            override = metadata.get(identifier, {})
            kind = override.get("shader_type") if isinstance(override, dict) else None
            kind = kind if kind in {"Standart", "4L-mat", "Decal"} else "4L-mat"
            selected = next(iter(pack))
            entries.append(material_record(folder, category, prefix, pack if kind == "4L-mat" else
                                           {selected: pack[selected]}, identifier, kind))
            groups = {key: group for key, group in groups.items() if key not in pack}
        for key, group in groups.items():
            identifier = f"{folder}::{key}"
            aliases = [f"{folder}::{Path(path).stem.casefold()}" for path in group["maps"]["Diffuse"]
                       if (token := map_token(Path(path).name)) and
                       (token[2] is not None or "diifuse" in Path(path).stem.casefold())]
            override = metadata.get(identifier, next((metadata[key] for key in aliases if key in metadata), {}))
            default = "Decal" if DECAL.search(f"{folder}/{key}") else "Standart"
            kind = (override.get("shader_type") or default) if isinstance(override, dict) else default
            kind = kind if kind in {"Standart", "4L-mat", "Decal"} else default
            record = material_record(folder, category, group["name"],
                                     folder_groups if kind == "4L-mat" else {key: group}, identifier, kind)
            if aliases:
                record["aliases"] = aliases
            entries.append(record)

    entries.sort(key=lambda entry: (entry["category"].casefold(), entry["name"].casefold(), entry["id"]))
    annotate_alpha(entries, previous, root)
    annotate_opacity_maps(entries, previous, root)
    for entry in entries:
        if entry["kind"] != "Decal":
            continue
        override = next((metadata[key] for key in (entry["id"], *entry.get("aliases", ()))
                         if isinstance(metadata.get(key), dict)), {})
        if override.get("shader_type") != "Decal" and not any(
                group.get("opacity_alpha", group.get("diffuse_alpha", {})).get("transparent")
                for group in entry["groups"].values()):
            entry["kind"] = "Standart"
    result = previews.populate(entries, previous, metadata, root, shared, progress)
    write_json(index, {"schema": 1, "catalog_schema": CATALOG_SCHEMA, "root": root_key, "cover_schema": previews.SCHEMA,
                       "cover_root": os.path.normcase(str(shared)), "entries": entries})
    return result


if __name__ == "__main__":
    operation, *args = sys.argv[1:]
    if operation == "scan":
        root, cache, shared, *exchange = (Path(arg).resolve() for arg in args)
        result = scan(root, cache, shared, lambda done, total, made: print(f"PROGRESS {done}/{total} · {made} new", flush=True),
                      exchange=next(iter(exchange), None))
        print(json.dumps(result), flush=True)
    elif operation == "preview":
        root, cache, relative, edge = args
        path, size = thumbnail(Path(root), Path(cache), relative, int(edge))
        print(json.dumps({"path": str(path), "size": size}, ensure_ascii=True))
    elif operation == "strip":
        root, cache, payload = args
        result = {}
        for relative in json.loads(payload):
            try:
                path, size = thumbnail(Path(root), Path(cache), relative, 160)
                result[relative] = {"path": str(path), "size": size}
            except (OSError, ValueError, Image.DecompressionBombError):
                result[relative] = {}
        print(json.dumps(result, ensure_ascii=True))
    elif operation in {"author", "authors"}:
        import svn_authors
        executable, location, *options = args
        cache = options[0] if options else None
        allow_download = len(options) > 1 and options[1] == "1"
        try:
            if not executable:
                from svn_runtime import ensure_svn
                executable = ensure_svn(executable, cache, allow_download)
            result = (svn_authors.first_add(executable, location) if operation == "author"
                      else svn_authors.populate(executable, location, cache))
        except (OSError, ValueError, TimeoutExpired, ParseError) as error:
            result = {"error": str(error)}
        print(json.dumps(result, ensure_ascii=True))
    elif operation == "layer_cover":
        import previews
        root, cache, recipe = map(Path, args)
        record = read_json(recipe, {})
        data, _ = previews.generate(record, {}, {}, root.resolve(), cache.resolve())
        print(json.dumps(data, ensure_ascii=True))
    elif operation == "gallery_cover":
        import previews
        root, cache, payload = args
        recipe = json.loads(payload)
        data, _ = previews.generate(recipe["record"], recipe["metadata"], {}, Path(root), Path(cache), edge=768)
        print(json.dumps(data, ensure_ascii=True))
    else:
        raise SystemExit(f"Unknown operation: {operation}")
