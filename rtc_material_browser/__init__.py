"""RTC Material Browser extension."""
import bpy
from bpy.props import BoolProperty, StringProperty

from . import layers, materials, settings, state
from .appearance import tr
from .paths import library_path
from .settings import RTC_OT_catalog_maintenance, RTC_Preferences
from .canvas import BROWSERS, RTC_OT_material_canvas, close_browsers, prepare_layout_undo, region_for, restore_saved_editors


class RTC_OT_open_catalog(bpy.types.Operator):
    bl_idname = "rtc.open_catalog"
    bl_label = "Open Material Core"
    separate_window: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        root = library_path(state.preferences(context))
        if root is None or not root.is_dir():
            self.report({"WARNING"}, tr("Choose the texture library folder in Material Core preferences."))
            return {"CANCELLED"}
        window, screen = context.window, context.screen
        area = context.area
        if area is None or area.type not in {"VIEW_3D", "IMAGE_EDITOR"}:
            area = max((candidate for candidate in screen.areas
                        if self.separate_window or candidate.type == "VIEW_3D"),
                       key=lambda candidate: candidate.width * candidate.height, default=None)
        if area is None:
            self.report({"WARNING"}, tr("Choose Image Editor in the area where Material Core should open."))
            return {"CANCELLED"}
        region = region_for(area)
        if self.separate_window:
            before = {item.as_pointer() for item in context.window_manager.windows}
            with bpy.context.temp_override(window=window, area=area, region=region):
                if bpy.ops.screen.area_dupli("INVOKE_DEFAULT") != {"FINISHED"}:
                    self.report({"ERROR"}, tr("Blender could not open a separate window."))
                    return {"CANCELLED"}
            owner_id = window.as_pointer()
            manager = context.window_manager
            new_window = next((item for item in manager.windows if item.as_pointer() not in before), None)
            window_id = new_window.as_pointer() if new_window else None

            def open_detached():
                candidates = [item for item in manager.windows if item.as_pointer() not in before]
                detached = next((item for item in candidates if item.as_pointer() == window_id), None)
                if detached is None and window_id is None and len(candidates) == 1:
                    detached = candidates[0]
                if not detached or not detached.screen.areas:
                    return None
                duplicate = detached.screen.areas[0]
                if duplicate.type not in {"IMAGE_EDITOR", "VIEW_3D"}:
                    duplicate.type = "IMAGE_EDITOR"
                duplicate_region = region_for(duplicate)
                if duplicate_region is None or duplicate_region.width < 1:
                    return 0.1
                with bpy.context.temp_override(window=detached, area=duplicate, region=duplicate_region):
                    bpy.ops.rtc.material_canvas("INVOKE_DEFAULT", owner_id=str(owner_id))
                return None

            bpy.app.timers.register(open_detached, first_interval=0.08)
            return {"FINISHED"}
        existing = next((browser for browser in BROWSERS.values() if browser.screen == screen), None)
        if existing and area.type != "IMAGE_EDITOR":
            self.report({"INFO"}, tr("Material Core is already open in this workspace."))
            return {"CANCELLED"}
        if area.type == "IMAGE_EDITOR":
            with bpy.context.temp_override(window=window, area=area, region=region):
                result = bpy.ops.rtc.material_canvas("INVOKE_DEFAULT")
            return {"FINISHED"} if "RUNNING_MODAL" in result else {"CANCELLED"}
        before = {area.as_pointer() for area in screen.areas}
        with bpy.context.temp_override(window=window, area=area, region=region):
            if bpy.ops.screen.area_split(direction="VERTICAL", factor=0.50) != {"FINISHED"}:
                self.report({"ERROR"}, tr("Blender could not split this 3D View."))
                return {"CANCELLED"}

        def open_split():
            try:
                if window.screen != screen:
                    return None
                area = next((area for area in screen.areas if area.as_pointer() not in before), None)
                if area is None:
                    return None
                region = region_for(area)
                if region is None or region.width < 1:
                    return 0.1
                with bpy.context.temp_override(window=window, area=area, region=region):
                    bpy.ops.rtc.material_canvas("INVOKE_DEFAULT")
            except ReferenceError:
                pass
            return None

        bpy.app.timers.register(open_split, first_interval=0.12)
        return {"FINISHED"}


class RTC_OT_apply_catalog_material(bpy.types.Operator):
    bl_idname = "rtc.apply_catalog_material"
    bl_label = "Apply RTC Material"
    bl_options = {"REGISTER", "UNDO"}

    key: StringProperty(options={"HIDDEN"})
    target: StringProperty(options={"HIDDEN"})
    browser_id: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        browser = BROWSERS.get(int(self.browser_id)) if self.browser_id.isdigit() else None
        if browser is None:
            return {"CANCELLED"}
        data = browser.config
        obj = context.view_layer.objects.get(self.target)
        material = None
        try:
            if not materials.can_assign_to(obj):
                raise ValueError(tr("Choose an editable mesh in Object Mode."))
            previous = obj.active_material
            record = browser.draft_record() if self.key == layers.DRAFT_ID else data.entries[self.key]
            metadata = data.metadata.get(self.key, {})
            material = materials.build(data.root, data.shader or materials.default_shader(), record, metadata)
            materials.assign(obj, material)
        except (KeyError, OSError, RuntimeError, ValueError) as error:
            if material is not None and material.users == 0:
                materials.remove_unused_catalog_material(material)
            data.status = str(error)
            self.report({"WARNING"}, str(error))
            return {"CANCELLED"}
        materials.remove_unused_catalog_material(previous)
        return {"FINISHED"}


def draw_window_menu(self, _context):
    self.layout.separator()
    self.layout.operator("rtc.open_catalog", text="Material Core", icon="ASSET_MANAGER").separate_window = True


def draw_image_header(self, context):
    if getattr(context.space_data, "ui_type", None) != "UV":
        self.layout.operator("rtc.open_catalog", text="Material Core", icon="ASSET_MANAGER")


CLASSES = (RTC_Preferences, RTC_OT_catalog_maintenance, RTC_OT_material_canvas, RTC_OT_open_catalog,
           RTC_OT_apply_catalog_material)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    config = bpy.context.preferences.addons.get(__package__)
    if config:
        settings.update_appearance(config.preferences, bpy.context)
    bpy.app.handlers.load_pre.append(close_browsers)
    bpy.app.handlers.undo_pre.append(prepare_layout_undo)
    bpy.app.handlers.redo_pre.append(prepare_layout_undo)
    bpy.types.TOPBAR_MT_window.append(draw_window_menu)
    bpy.types.IMAGE_HT_header.append(draw_image_header)
    if not bpy.app.background:
        bpy.app.timers.register(restore_saved_editors, first_interval=0.3, persistent=True)


def unregister():
    settings.stop_maintenance()
    bpy.app.handlers.load_pre.remove(close_browsers)
    bpy.app.handlers.undo_pre.remove(prepare_layout_undo)
    bpy.app.handlers.redo_pre.remove(prepare_layout_undo)
    bpy.types.TOPBAR_MT_window.remove(draw_window_menu)
    bpy.types.IMAGE_HT_header.remove(draw_image_header)
    if bpy.app.timers.is_registered(restore_saved_editors):
        bpy.app.timers.unregister(restore_saved_editors)
    close_browsers()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
