"""Layer order shared by the material builder and preview worker."""
import re

LIMIT = 4
DRAFT_ID = "@draft-4l"
LAYER_CHANNELS = {"Diffuse", "Normal", "Roughness", "Alpha", "aoMap"}


def layer_keys(record, metadata):
    groups = record["groups"]
    supplied = metadata.get("layers")
    if supplied:
        if not (isinstance(supplied, list) and 1 <= len(supplied) <= LIMIT
                and all(isinstance(key, str) and key in groups for key in supplied)
                and len(set(supplied)) == len(supplied)):
            raise ValueError("Choose one to four distinct layers from this material.")
        return supplied
    # Ignore auxiliary files and numbered backup copies misidentified as diffuse.
    candidates = [key for key in groups if not re.search(r"_height$|_occlusion$|\([^)]*\)$", key)]
    keys = candidates or list(groups)
    priorities = {"base": -4, "black": -4, "red": -3, "green": -2, "blue": -1}
    return sorted(keys, key=lambda key: (
        priorities.get(key, 0), [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", key)]
    ))[:LIMIT]


def references(record, metadata):
    if record.get("layer_refs"):
        return [tuple(pair) for pair in record["layer_refs"]]
    keys = layer_keys(record, metadata) if record["kind"] == "4L-mat" else list(record["groups"])
    return [(record["id"], key) for key in keys[:LIMIT]]


def compose(entries, refs, identifier, name):
    if not 1 <= len(refs) <= LIMIT:
        raise ValueError("Add one to four materials to the 4L builder.")
    groups = {}
    first = None
    for slot, (source, group) in enumerate(refs, 1):
        entry = entries.get(source)
        if entry is None or group not in entry["groups"]:
            raise ValueError(f"Layer {slot} is missing from the SVN catalog.")
        if first is None:
            first = entry
        groups[f"{slot:02}"] = entry["groups"][group]
    return {"id": identifier, "name": name, "category": "Custom 4L", "kind": "4L-mat",
            "folder": first["folder"], "anchor": "", "size": first.get("size", ""),
            "layer_refs": [list(pair) for pair in refs], "groups": groups}
