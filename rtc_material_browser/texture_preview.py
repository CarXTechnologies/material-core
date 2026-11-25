"""Decode an OpenEXR thumbnail in a separate Blender process."""
import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rtc_material_browser.catalog import thumbnail_target


root, cache, relative, edge = sys.argv[sys.argv.index("--") + 1:]
edge = int(edge)
source, target = thumbnail_target(Path(root), Path(cache), relative, edge)
image = bpy.data.images.load(str(source))
width, height = image.size
if not width or not height:
    raise ValueError(f"Cannot read {source.name}")
if not target.is_file():
    factor = min(1, edge / max(width, height))
    image.scale(max(1, round(width * factor)), max(1, round(height * factor)))
    target.parent.mkdir(parents=True, exist_ok=True)
    image.file_format = "PNG"
    temporary = target.with_name(f".{target.name}.tmp.png")
    image.save(filepath=str(temporary))
    temporary.replace(target)
print("RTC_PREVIEW " + json.dumps({"path": str(target), "size": f"{width} × {height}"}), flush=True)
