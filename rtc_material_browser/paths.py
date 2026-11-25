"""Resolve a library and all derived locations from Blender preferences."""
from dataclasses import dataclass
from pathlib import Path
import os
import shutil

import bpy

from .shared_data import FOLDER_NAME


def absolute(value):
    return Path(bpy.path.abspath(value)).resolve()


def readable_path(value):
    path = Path(value)
    filename = str(path)
    if os.name == "nt" and path.is_absolute() and len(filename) >= 248 and not filename.startswith("\\\\?\\"):
        prefix = "\\\\?\\UNC\\" if filename.startswith("\\\\") else "\\\\?\\"
        return Path(prefix + (filename[2:] if filename.startswith("\\\\") else filename))
    return path


def library_path(config):
    return absolute(config.library_path) if config.library_path.strip() else None


@dataclass(frozen=True)
class CatalogPaths:
    root: Path
    cache: Path
    covers: Path
    exchange: Path

    @classmethod
    def from_preferences(cls, config):
        root = library_path(config)
        if root is None or not root.is_dir():
            raise ValueError("Choose the texture library folder in Material Core preferences.")
        cache = (absolute(config.cache_path) if config.cache_path else
                 Path(bpy.utils.user_resource("DATAFILES", path="rtc_material_browser", create=True)))
        covers = absolute(config.shared_path) if config.shared_path else cache
        if covers == root / ".rtc_material_browser":
            covers = cache
        exchange = getattr(config, "exchange_path", "")
        return cls(root, cache, covers, absolute(exchange) if exchange else root / FOLDER_NAME)


def find_svn(config, cache):
    local = Path(os.environ.get("LOCALAPPDATA", str(cache)))
    program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    options = (
        absolute(config.svn_path) if config.svn_path else None,
        shutil.which("svn"),
        cache / "tools/subversion-1.14.5/bin/svn.exe",
        local / "Material Core/tools/subversion-1.14.5/bin/svn.exe",
        program_files / "TortoiseSVN/bin/svn.exe",
        program_files / "SlikSvn/bin/svn.exe",
    )
    return next((str(path) for path in options if path and Path(path).is_file()), "")
