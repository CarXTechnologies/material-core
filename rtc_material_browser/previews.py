"""Fast catalog thumbnails rendered from texture maps without a Blender scene."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import os

from PIL import Image, ImageDraw, ImageOps

if __package__:
    from .layers import layer_keys
    from .studio_preview import SIZE, TEXTURE_EDGE, decal, sphere
else:
    from layers import layer_keys
    from studio_preview import SIZE, TEXTURE_EDGE, decal, sphere

SCHEMA = 2
DIRECTORY = f"previews/covers-v{SCHEMA}"
MAX_WORKERS = 2


def source_image(path, edge=SIZE):
    with Image.open(path) as image:
        size = image.size
        image.draft("RGB", (edge, edge))
        if image.getexif().get(274, 1) not in {0, 1}:
            image = ImageOps.exif_transpose(image)
        if image.mode.startswith("I;16"):
            image = image.convert("F").point(lambda value: value / 257.0).convert("L")
        elif image.mode in {"I", "F"}:
            low, high = image.getextrema()
            scale = 255.0 / (high - low) if high != low else 0.0
            image = image.convert("F").point(lambda value: value * scale - low * scale).convert("L")
        image.thumbnail((edge, edge), Image.Resampling.BILINEAR, reducing_gap=2.0)
        return image.convert("RGBA"), f"{size[0]} × {size[1]}"


def missing_preview():
    image = Image.new("RGB", (SIZE, SIZE), (35, 41, 48))
    draw = ImageDraw.Draw(image)
    draw.rectangle((50, 50, 142, 142), outline=(94, 104, 116), width=2)
    draw.line((55, 132, 93, 87, 135, 129), fill=(94, 104, 116), width=3)
    return image


def inputs(entry, meta):
    groups = entry["groups"]
    mode = entry["kind"]
    if mode == "4L-mat":
        try:
            keys = layer_keys(entry, meta)
        except ValueError:
            keys = layer_keys(entry, {})
    else:
        keys = list(groups)[:1]
    channels = (("Diffuse", "Normal", "Roughness") if mode == "4L-mat" else
                ("Diffuse", "Normal", "Roughness", "Metalic", "Alpha") if mode == "Decal" else
                ("Diffuse", "Normal", "Roughness", "Metalic"))
    return [{channel: groups[key]["maps"][channel] for channel in channels if groups[key]["maps"].get(channel)}
            for key in keys[:4]]


def local_source(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Path is outside the textures library: {relative}")
    try:
        stat = path.stat()
        return path, [relative, stat.st_size, stat.st_mtime_ns]
    except FileNotFoundError:
        return path, [relative, -1, -1]


def content_token(profile, paths):
    digest = hashlib.sha256(profile.encode())
    for path, stamp in paths:
        digest.update(stamp[0].encode("utf-8"))
        digest.update(b"\0")
        if stamp[1] >= 0:
            with path.open("rb") as stream:
                digest.update(hashlib.file_digest(stream, "sha256").digest())
    return digest.hexdigest()


def save_preview(image, target, comment):
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_name = f".{target.stem}.{os.getpid()}.tmp"
    temporary = target.with_name(safe_name)
    try:
        image.save(temporary, format="JPEG", quality=90, subsampling=0, comment=comment.encode("utf-8"))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def generate(entry, meta, previous, root, shared, edge=SIZE):
    layers = [{channel: [local_source(root, path) for path in candidates]
               for channel, candidates in layer.items()} for layer in inputs(entry, meta)]
    paths = [source for layer in layers for candidates in layer.values() for source in candidates]
    stamps = [version for _, version in paths]
    directory = DIRECTORY if edge == SIZE else f"gallery-covers-v1/{edge}"
    if previous.get("preview_inputs") == stamps and previous.get("preview_type") in {"generated", "placeholder"}:
        filename = previous["thumb"]
        if filename.startswith(directory + "/") and (shared / filename).is_file():
            return {key: previous[key] for key in ("thumb", "size", "preview_type", "preview_inputs")}, False

    def open_map(candidates):
        for path, _ in candidates:
            try:
                return source_image(path, edge=max(TEXTURE_EDGE, edge))
            except (OSError, ValueError, Image.DecompressionBombError):
                continue
        return None, ""

    mode = entry["kind"]
    token = (content_token(f"{SCHEMA}/{mode}", paths) if edge == SIZE else
             hashlib.sha256(repr((root, mode, stamps)).encode("utf-8")).hexdigest())
    filename = f"{directory}/{token[:2]}/{token}.jpg"
    target = shared / filename
    if target.is_file() and previous.get("thumb") == filename:
        return {"thumb": filename, "size": previous.get("size", ""), "preview_type": previous["preview_type"],
                "preview_inputs": stamps}, False
    if target.is_file():
        # A published thumbnail can be reused from another SVN working copy.
        try:
            with Image.open(target) as saved:
                marker = saved.info.get("comment", b"").decode("utf-8")
            state, _, size = marker.partition("|")
            if state in {"generated", "placeholder"}:
                return {"thumb": filename, "size": size, "preview_type": state, "preview_inputs": stamps}, False
        except (OSError, UnicodeError):
            pass
    materials = []
    size = ""
    for layer in layers:
        diffuse, resolution = open_map(layer.get("Diffuse", ()))
        if diffuse is None:
            continue
        size = size or resolution
        maps = {"Diffuse": diffuse}
        for channel, candidates in layer.items():
            if channel != "Diffuse":
                image, _ = open_map(candidates)
                if image is not None:
                    maps[channel] = image
        materials.append(maps)
    if not materials:
        cover, preview_type = missing_preview(), "placeholder"
    elif mode == "Decal":
        cover, preview_type = decal(materials[0], edge), "generated"
    else:
        cover, preview_type = sphere(materials, edge), "generated"
    save_preview(cover, target, f"{preview_type}|{size}")
    return {"thumb": filename, "size": size, "preview_type": preview_type, "preview_inputs": stamps}, True


def populate(entries, previous, metadata, root, shared, progress):
    def job(entry):
        key = entry["id"]
        value = metadata.get(key)
        cached = previous.get(key, {})
        if cached.get("kind") != entry["kind"]:
            cached = {}
        return generate(entry, value if isinstance(value, dict) else {}, cached, root, shared)

    built = 0
    placeholder = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as workers:
        for count, (entry, (data, created)) in enumerate(zip(entries, workers.map(job, entries)), 1):
            entry.update(data)
            built += created
            placeholder += data["preview_type"] == "placeholder"
            if progress and (count % 50 == 0 or count == len(entries)):
                progress(count, len(entries), built)
    return {"materials": len(entries), "created": built, "reused": len(entries) - built, "placeholders": placeholder}
