"""Versioned JSON files shared through the texture library's SVN working copy."""
from pathlib import Path
import re
import uuid

if __package__:
    from .catalog import read_json, write_json
else:
    from catalog import read_json, write_json


FOLDER_NAME = "Material Core"
FAVORITES = "@favorites"
IGNORE_LIST = "@ignore"
RESERVED_NAMES = {"all collections", "favorites", "ignore list"}
EMPTY_TAGS = {"schema": 1, "tags": [], "materials": {}}
EMPTY_MEMBERS = {"schema": 1, "materials": []}


def legacy_metadata_directory(root, previews):
    return previews if (previews / "metadata.json").is_file() else root / ".rtc_material_browser"


def string_list(value, filename):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Invalid list in {filename}.")
    return value


def document(path, default):
    value = read_json(path, default)
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ValueError(f"Unsupported format in {path.name}.")
    return value


class SharedCatalog:
    def __init__(self, directory, legacy_directory):
        self.directory = directory
        self.tags_file = directory / "tags.json"
        self.ignore_file = directory / "ignore-list.json"
        self.collections_dir = directory / "collections"
        self.legacy_tags = legacy_directory / "metadata.json"

    def collection_paths(self):
        return sorted(path for path in self.collections_dir.glob("*.json") if path.is_file())

    def watched_paths(self):
        fallback = () if self.tags_file.is_file() else (self.legacy_tags,)
        return (self.tags_file, self.ignore_file, *fallback, *self.collection_paths())

    def read_tags(self):
        source = self.tags_file if self.tags_file.is_file() else self.legacy_tags
        data = document(source, EMPTY_TAGS)
        records = data.get("materials")
        if not isinstance(records, dict) or not all(isinstance(value, dict) for value in records.values()):
            raise ValueError(f"Invalid materials in {source.name}.")
        vocabulary = set(string_list(data.get("tags", []), source.name))
        for value in records.values():
            vocabulary.update(string_list(value.get("tags", []), source.name))
        return {"schema": 1, "tags": sorted(vocabulary), "materials": records}

    def write_tags(self, data):
        for row in data["materials"].values():
            data["tags"].extend(row.get("tags", ()))
        data["tags"] = sorted(set(data["tags"]))
        write_json(self.tags_file, data)

    def initialize(self):
        self.collections_dir.mkdir(parents=True, exist_ok=True)
        if not self.tags_file.exists():
            self.write_tags(self.read_tags())
        if not self.ignore_file.exists():
            write_json(self.ignore_file, EMPTY_MEMBERS)

    def read_ignored(self):
        return set(string_list(document(self.ignore_file, EMPTY_MEMBERS).get("materials"), self.ignore_file.name))

    def set_ignored(self, key, aliases, hidden):
        members = self.read_ignored()
        members.difference_update({key, *aliases})
        if hidden:
            members.add(key)
        write_json(self.ignore_file, {"schema": 1, "materials": sorted(members)})

    def collection_path(self, key):
        if not key or Path(key).name != key or key in {".", ".."} or ":" in key:
            raise ValueError("Invalid collection ID.")
        return self.collections_dir / f"{key}.json"

    def read_collections(self):
        collections = {}
        for path in self.collection_paths():
            row = document(path, {})
            name = row.get("name")
            if not isinstance(name, str) or not name.strip() or name.casefold() in RESERVED_NAMES:
                raise ValueError(f"Invalid collection name in {path.name}.")
            members = set(string_list(row.get("materials"), path.name))
            presets = row.get("presets", {})
            if not isinstance(presets, dict):
                raise ValueError(f"Invalid presets in {path.name}.")
            collections[path.stem] = (name, members, {
                key: value for key, value in presets.items() if key in members and key.startswith("preset::")
                and isinstance(value, dict) and isinstance(value.get("name"), str)
                and isinstance(value.get("refs"), list)
            })
        return collections

    def create_collection(self, name):
        name = name.strip()
        if not name:
            raise ValueError("Enter a collection name.")
        if name.casefold() in RESERVED_NAMES or any(
            title.casefold() == name.casefold() for title, _, _ in self.read_collections().values()
        ):
            raise ValueError("A collection already uses that name.")
        slug = re.sub(r"[^\w-]+", "-", name.casefold()).strip("-_")[:56].rstrip("-_")
        key = f"{slug or 'collection'}-{uuid.uuid4().hex[:12]}"
        write_json(self.collection_path(key), {"schema": 1, "name": name, "materials": []})
        return key

    def toggle_member(self, collection, key, aliases, preset=None):
        path = self.collection_path(collection)
        row = document(path, {})
        members = set(string_list(row.get("materials"), path.name))
        equivalent = {key, *aliases}
        if members.intersection(equivalent):
            members.difference_update(equivalent)
            for member in equivalent:
                row.get("presets", {}).pop(member, None)
        else:
            members.add(key)
            if preset is not None:
                row.setdefault("presets", {})[key] = {"name": preset["name"], "refs": preset["refs"]}
        if not row.get("presets"):
            row.pop("presets", None)
        row["materials"] = sorted(members)
        write_json(path, row)

    def migrate_local_collections(self, path, marker):
        if marker.exists() or not path.is_file():
            return
        for key, row in read_json(path, {}).items():
            target = self.collection_path(key)
            name = row["name"]
            if name.casefold() in RESERVED_NAMES:
                name += " (imported)"
            if not target.exists():
                write_json(target, {"schema": 1, "name": name,
                                    "materials": string_list(row["materials"], path.name)})
        write_json(marker, {"schema": 1})
