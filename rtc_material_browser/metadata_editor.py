"""Draft metadata and keyboard editing for the embedded material editor."""
from . import layers, search


class TextField:
    def __init__(self, text=""):
        self.text = text
        self.cursor = self.anchor = len(text)

    @property
    def selection(self):
        return sorted((self.cursor, self.anchor))

    def select(self, position, extend=False):
        self.cursor = max(0, min(len(self.text), position))
        if not extend:
            self.anchor = self.cursor

    def replace(self, value):
        start, end = self.selection
        self.text = self.text[:start] + value + self.text[end:]
        self.select(start + len(value))

    def key(self, event, manager):
        kind = event.type
        start, end = self.selection
        if event.ctrl and kind == "A":
            self.anchor, self.cursor = 0, len(self.text)
        elif event.ctrl and kind in {"C", "X"}:
            if start != end:
                manager.clipboard = self.text[start:end]
                if kind == "X":
                    self.replace("")
        elif event.ctrl and kind == "V":
            self.replace(" ".join(manager.clipboard.splitlines()))
        elif kind in {"LEFT_ARROW", "RIGHT_ARROW", "HOME", "END"}:
            if kind == "HOME":
                pos = 0
            elif kind == "END":
                pos = len(self.text)
            elif start != end and not event.shift:
                pos = start if kind == "LEFT_ARROW" else end
            else:
                pos = self.cursor + (-1 if kind == "LEFT_ARROW" else 1)
            self.select(pos, event.shift)
        elif kind in {"BACK_SPACE", "DEL"}:
            if start == end:
                self.select(self.cursor + (-1 if kind == "BACK_SPACE" else 1), True)
            self.replace("")
        elif not event.ctrl and event.unicode and event.unicode.isprintable():
            self.replace(event.unicode)


class NameEditor:
    def __init__(self, title, action, *, name="", collection=None, refs=(), rename_palette=False):
        self.title = title
        self.action = action
        self.collection = collection
        self.refs = refs
        self.rename_palette = rename_palette
        self.fields = {"name": TextField(name)}
        self.focus = "name"
        self.field_views = {}
        self.error = ""


class MetadataEditor:
    def __init__(self, config, key, tags_only):
        self.key = key
        self.entry = config.entries[key]
        self.initial = dict(config.metadata.get(key, {}))
        self.tags_only = tags_only
        self.preset = key in config.presets and key not in config.shared_presets
        self.fields = {
            "tag": TextField(),
            "layers": TextField(", ".join(self.initial.get("layers", ()))),
        }
        self.tags = list(dict.fromkeys(self.initial.get("tags", ())))
        self.locked = set(self.tags) if tags_only else set()
        self.vocabulary = {tag: search.normalize(tag) for tag in config.tag_vocabulary}
        self.focus = "tag"
        self.scroll = self.scroll_limit = 0
        self.field_views = {}
        self.error = ""

    @property
    def has_layers(self):
        return not self.tags_only and not self.entry.get("layer_refs") and self.entry["kind"] == "4L-mat"

    def suggestions(self):
        query = search.normalize(self.fields["tag"].text.rsplit(",", 1)[-1].strip())
        if not query or self.focus != "tag":
            return []
        matches = [(not normalized.startswith(query), tag) for tag, normalized in self.vocabulary.items()
                   if tag not in self.tags and query in normalized]
        return [tag for _, tag in sorted(matches)[:3]]

    def add_tags(self, suggestion=None):
        field = self.fields["tag"]
        source = field.text
        if suggestion:
            head, sep, _ = source.rpartition(",")
            source = head + sep + suggestion
        for raw in source.split(","):
            tag = raw.strip().casefold()
            if tag and tag not in self.tags:
                self.tags.append(tag)
        self.fields["tag"] = TextField()
        self.focus = "tag"

    def fields_to_save(self):
        focus = self.focus
        self.add_tags()
        self.focus = focus
        result = {"tags": sorted(self.tags)}
        if self.has_layers:
            chosen = [key.strip().casefold() for key in self.fields["layers"].text.split(",") if key.strip()]
            layers.layer_keys(self.entry, {"layers": chosen})
            result["layers"] = chosen
        return result
