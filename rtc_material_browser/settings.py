"""Simple installation preferences and background catalog maintenance."""
import bpy
from bpy.props import EnumProperty, FloatVectorProperty, StringProperty

from . import appearance, state
from .appearance import tr
from .canvas import BROWSERS
from .paths import library_path


maintenance = None
last_status = ""


def redraw():
    manager = bpy.context.window_manager
    if manager is None:
        return
    for window in manager.windows:
        for area in window.screen.areas:
            if area.type in {"PREFERENCES", "IMAGE_EDITOR"}:
                area.tag_redraw()


def update_appearance(config, _context):
    appearance.configure(config.language, tuple(config.accent_color))
    redraw()


def read_target(config):
    root = library_path(config)
    browser = max((item for item in BROWSERS.values() if item.config.root == root
                   and item.detail in item.config.entries
                   and not item.config.entries[item.detail].get("layer_refs")),
                  key=lambda item: item.detail_opened_at, default=None)
    return (browser.config.entries[browser.detail], browser.config.metadata.get(browser.detail, {})) if browser else None


def poll_maintenance():
    global maintenance, last_status
    if maintenance is None:
        return None
    try:
        maintenance.advance()
        last_status = maintenance.status
    except OSError as error:
        last_status = str(error)
        maintenance.close()
    redraw()
    if maintenance.busy:
        return 0.3
    for browser in BROWSERS.values():
        if browser.config.root == maintenance.root:
            browser.config.author_errors.update(maintenance.author_errors)
    maintenance.close()
    maintenance = None
    return None


def stop_maintenance():
    global maintenance
    for callback in (poll_maintenance, prepare_library):
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
    if maintenance:
        maintenance.close()
        maintenance = None


def start(config, action, entry=None, *, automatic=False):
    global maintenance, last_status
    if maintenance and maintenance.busy:
        raise ValueError(tr("An operation is already running."))
    task = state.State(config, auto_index=False)
    if action == "INDEX":
        if automatic and task.cover_current:
            return
        for browser in BROWSERS.values():
            if browser.config.root != task.root:
                continue
            if browser.config.indexing:
                if not automatic:
                    last_status = tr("Indexing textures…")
                return
        task.index()
    elif action == "READ":
        task.author(entry)
    else:
        task.populate_authors()
    last_status = task.status
    if not task.busy:
        return
    maintenance = task
    if not bpy.app.timers.is_registered(poll_maintenance):
        bpy.app.timers.register(poll_maintenance, first_interval=0.1, persistent=True)


def prepare_library():
    global last_status
    config = state.preferences(bpy.context)
    root = library_path(config)
    if root and root.is_dir():
        if maintenance and maintenance.busy:
            return 0.5
        try:
            start(config, "INDEX", automatic=True)
        except (OSError, ValueError) as error:
            last_status = str(error)
    return None


def library_changed(_config, _context):
    if bpy.app.background:
        return
    if bpy.app.timers.is_registered(prepare_library):
        bpy.app.timers.unregister(prepare_library)
    bpy.app.timers.register(prepare_library, first_interval=0.5, persistent=True)


class RTC_Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    library_path: StringProperty(name="Texture library", subtype="DIR_PATH", update=library_changed)
    cache_path: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})
    shared_path: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})
    exchange_path: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})
    shader_path: StringProperty(subtype="FILE_PATH", options={"HIDDEN"})
    svn_path: StringProperty(subtype="FILE_PATH", options={"HIDDEN"})
    accent_color: FloatVectorProperty(name="Accent color", subtype="COLOR_GAMMA", size=3, min=0.0, max=1.0,
                                      default=appearance.DEFAULT_ACCENT, update=update_appearance)
    language: EnumProperty(name="Language", items=(("EN", "English", ""), ("RU", "Русский", "")),
                           default="EN", update=update_appearance)

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "library_path", text=tr("Texture library"))
        look = layout.box()
        look.label(text=tr("Interface"))
        look.prop(self, "accent_color", text=tr("Accent color"))
        look.prop(self, "language", text=tr("Language"))
        tools = layout.box()
        tools.label(text=tr("Maintenance"))
        row = tools.row(align=True)
        root = library_path(self)
        row.enabled = bool(root and root.is_dir() and not (maintenance and maintenance.busy))
        row.operator("rtc.catalog_maintenance", text=tr("Index"), icon="FILE_REFRESH").action = "INDEX"
        target = read_target(self)
        read = row.row(align=True)
        read.enabled = bool(target)
        read.operator("rtc.catalog_maintenance", text=tr("Read"), icon="INFO").action = "READ"
        row.operator("rtc.catalog_maintenance", text=tr("Cache SVN"), icon="DISK_DRIVE").action = "CACHE_SVN"
        if target:
            entry, metadata = target
            tools.label(text=f'{tr("Read")} · {metadata.get("name") or entry["name"]}')
        else:
            tools.label(text=tr("Open a material in Material Core to use Read."))
        if last_status:
            tools.label(text=tr(last_status), icon="TIME" if maintenance else "INFO")


class RTC_OT_catalog_maintenance(bpy.types.Operator):
    bl_idname = "rtc.catalog_maintenance"
    bl_label = "Material Core maintenance"
    action: EnumProperty(items=(("INDEX", "Index", ""), ("READ", "Read", ""), ("CACHE_SVN", "Cache SVN", "")),
                         options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        config = state.preferences(context)
        target = read_target(config) if self.action == "READ" else None
        try:
            if self.action == "READ" and not target:
                raise ValueError("Open a material in Material Core to use Read.")
            start(config, self.action, target[0] if target else None)
        except (OSError, ValueError) as error:
            self.report({"WARNING"}, tr(str(error)))
            return {"CANCELLED"}
        redraw()
        return {"FINISHED"}
