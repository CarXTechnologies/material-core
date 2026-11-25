"""Material gallery drawn inside a dedicated split Blender area."""
from dataclasses import dataclass
import math
from pathlib import Path
from time import monotonic

import blf
import bpy
from bpy.props import StringProperty
import gpu
from bpy_extras import view3d_utils

from . import appearance, catalog, layers, materials, state
from .appearance import BG, BUTTON, CARD, HOVER, LINE, MATTE, MUTED, PANEL, TEXT, mix_color, tr
from .paths import library_path
from .drag_overlay import DragOverlay
from .drawing import Painter
from .live_preview import CHANNELS, LivePreview
from .metadata_editor import MetadataEditor, NameEditor

HOVER_PREVIEW_DELAY = 0.65
HOVER_TIME = 0.14
POPUP_TIME = 0.16
GRAB_TIME = 0.13
DROP_TRANSITION = 0.16
DROP_PROBE_INTERVAL = 1 / 30
DROP_SCALE = 0.68
BUILDER_HEIGHT = 137
EDITOR_MARKER = "rtc_material_core_layout"
DEFAULT_CATEGORY = "Building"
VIEW_FLAGS = ("show_region_toolbar", "show_region_ui", "show_region_header", "show_region_tool_header")
HEADER_LOGO = Path(__file__).with_name("assets") / "material-core-logo.png"


def ease_out(elapsed, duration):
    return 1 - (1 - min(1.0, max(0.0, elapsed / duration))) ** 3


def region_for(area):
    return next((region for region in area.regions if region.type == "WINDOW"), None)


def layout_marker(area):
    if area.type == "IMAGE_EDITOR":
        image = area.spaces.active.image
        if image and image.get(EDITOR_MARKER):
            return image
    return None


def restore_saved_editors():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.as_pointer() not in BROWSERS and layout_marker(area) is not None:
                region = region_for(area)
                if region:
                    with bpy.context.temp_override(window=window, area=area, region=region):
                        bpy.ops.rtc.material_canvas("INVOKE_DEFAULT")
    return 0.4


def open_svn_folder(root, record):
    path = catalog.source_path(root, record["folder"])
    if not path.is_dir():
        raise ValueError("This folder is missing from the SVN working copy.")
    bpy.ops.wm.path_open(filepath=str(path))


def drop_mesh(context, x, y, browser):
    for area in context.window.screen.areas:
        if area.type != "VIEW_3D" or area.as_pointer() == browser:
            continue
        region = region_for(area)
        if region is None or not (region.x <= x < region.x + region.width and region.y <= y < region.y + region.height):
            continue
        coordinate = x - region.x, y - region.y
        space = area.spaces.active.region_3d
        if space is None:
            continue
        direction = view3d_utils.region_2d_to_vector_3d(region, space, coordinate)
        origin = view3d_utils.region_2d_to_origin_3d(region, space, coordinate, clamp=10000.0)
        hit, _, _, _, obj, _ = context.scene.ray_cast(context.evaluated_depsgraph_get(), origin, direction)
        if hit:
            original = obj.original
            if materials.can_assign_to(original) and original.visible_get(view_layer=context.view_layer,
                                                                          viewport=area.spaces.active):
                return original
    return None


BROWSERS = {}


@bpy.app.handlers.persistent
def close_browsers(*_args):
    for browser in list(BROWSERS.values()):
        browser.release(restore_editor=False)


@bpy.app.handlers.persistent
def prepare_layout_undo(*_args):
    for browser in BROWSERS.values():
        browser.undo_pending = True


@dataclass(frozen=True)
class Grab:
    key: str
    x: int
    y: int
    offset_x: int
    offset_y: int
    width: int
    height: int


class Browser:
    def __init__(self, context, owner_id=None):
        self.area_id = context.area.as_pointer()
        self.window = context.window
        self.owner_id = owner_id
        self.screen = context.screen
        self.manager = context.window_manager
        # Settings may already be preparing this library in the background.
        from .settings import maintenance
        config = state.preferences(context)
        indexing = maintenance and maintenance.root == library_path(config) and maintenance.indexing
        self.config = state.State(config, index_on_open=not indexing)
        self.painter = Painter()
        self.hits = []
        self.mouse = (-1, -1)
        self.hover_deadline = None
        self.hover_preview = None
        self.popup_started = 0.0
        self.card_focus = None
        self.card_fade = (None, 0.0)
        self.card_motion_started = 0.0
        self.motion_end = 0.0
        self.motion_timer = self.animation_frame
        self.search = self.tag = ""
        self.category = self.initial_category()
        self.collection = ""
        self.collection_picker = None
        self.metadata_editor = None
        self.name_editor = None
        self.collection_scroll = 0
        self.typing = False
        self.detail = self.map = None
        self.detail_opened_at = 0.0
        self.live = LivePreview()
        self.live_angles = (0.0, 0.0)
        self.live_zoom = 1.0
        self.live_plane = False
        self.live_layer = 0
        self.live_drag = None
        self.live_error = ""
        self.menu = None
        self.card_menu = None
        self.builder_open = False
        self.builder_height = BUILDER_HEIGHT
        self.layer_refs = []
        self.layer_press = None
        self.layer_drag = None
        self.scroll = self.limit = self.detail_scroll = self.detail_limit = self.menu_scroll = 0
        self.row_height = 160
        self.gallery_columns = 1
        self.zoom_anchor = None
        self.results_key = None
        self.results = []
        self.pressed = self.drag = None
        self.pointer = (0, 0)
        self.drag_regions = set()
        self.drag_frame = 0.0
        self.grab_started = 0.0
        self.drag_pending = False
        self.drop_hover = False
        self.drop_from = self.drop_changed_at = self.drop_probe_at = 0.0
        self.drop_probe_pending = False
        self.drag_overlay = None
        self.input = None
        self.cursor_owned = False
        self.undo_pending = False
        area = context.area
        self.original_editor = area.type
        area.type = "IMAGE_EDITOR"
        editor = area.spaces.active
        self.marker = layout_marker(area)
        self.original_flags = [int(getattr(editor, name)) for name in VIEW_FLAGS]
        previous_image = editor.image
        self.zoom = 1.0
        self.detached = owner_id is not None
        if self.marker is not None:
            self.original_editor = self.marker.get("return_editor", self.original_editor)
            self.original_flags = list(self.marker.get("visible_regions", self.original_flags))
            previous_image = self.marker.get("previous_image")
            self.zoom = float(self.marker.get("zoom", 1.0))
            self.detached = self.detached or bool(self.marker.get("detached"))
        if self.detached and self.owner_id is None and self.window.parent is not None:
            self.owner_id = self.window.parent.as_pointer()
        self.previous_image_name = previous_image.name if previous_image else ""
        self.bind_layout(area)
        self.last_tick = 0
        self.handler = bpy.types.SpaceImageEditor.draw_handler_add(self.paint, (), "WINDOW", "POST_PIXEL")
        self.overlay = bpy.types.SpaceView3D.draw_handler_add(self.paint_viewport_drag, (), "WINDOW", "POST_PIXEL")
        BROWSERS[self.area_id] = self
        self.ticker = self.tick
        bpy.app.timers.register(self.ticker, first_interval=0.1)
        area.tag_redraw()

    def bind_layout(self, area):
        area.type = "IMAGE_EDITOR"
        editor = area.spaces.active
        marker = layout_marker(area)
        if marker is None:
            marker = bpy.data.images.new(".Material Core Layout", 1, 1, alpha=True)
            marker[EDITOR_MARKER] = True
            marker.use_fake_user = True
            editor.image = marker
        marker["return_editor"] = self.original_editor
        marker["visible_regions"] = self.original_flags
        marker["zoom"] = self.zoom
        marker["detached"] = self.detached
        previous = bpy.data.images.get(self.previous_image_name)
        if previous is not None:
            marker["previous_image"] = previous
        for name in VIEW_FLAGS:
            setattr(editor, name, False)
        self.marker = marker

    def area(self):
        try:
            return next((area for area in self.screen.areas if area.as_pointer() == self.area_id), None)
        except ReferenceError:
            return None

    def visible(self):
        try:
            return self.window in self.manager.windows[:] and self.window.screen == self.screen
        except ReferenceError:
            return False

    def tick(self):
        area = self.area()
        if self.undo_pending:
            self.undo_pending = False
            self.clear_drag()
            self.clear_hover()
            self.input = None
            if area is not None and self.window in self.manager.windows[:]:
                self.bind_layout(area)
                area.tag_redraw()
        if area is None or layout_marker(area) != self.marker or self.window not in self.manager.windows[:]:
            self.release(stop_timer=False)
            return None
        if not self.visible():
            self.clear_drag()
            self.set_cursor(False)
            self.clear_hover()
            self.card_menu = None
            self.collection_picker = None
            self.live_drag = None
            self.card_focus = None
            self.card_fade = (None, 0.0)
            self.mouse = (-1, -1)
            return 0.1
        region = region_for(area)
        if self.input is None and region is not None:
            with bpy.context.temp_override(window=self.window, area=area, region=region):
                bpy.ops.rtc.material_canvas("INVOKE_DEFAULT")
            area.tag_redraw()
        if self.painter.pending:
            area.tag_redraw()
        now = monotonic()
        if self.hover_deadline is not None and now >= self.hover_deadline:
            self.hover_deadline = None
            self.hover_preview = self.hovered_card()
            if self.hover_preview:
                self.popup_started = now
                self.animate(POPUP_TIME, now)
                area.tag_redraw()
        if self.drag_pending:
            self.redraw_drag(now)
        interval = 0.15 if self.config.busy else 1.0
        if now - self.last_tick >= interval:
            self.last_tick = now
            try:
                if self.config.advance():
                    area.tag_redraw()
            except (OSError, ValueError) as error:
                self.config.status = str(error)
                area.tag_redraw()
        return 0.05 if self.drag else 0.1

    def animate(self, duration, now):
        self.motion_end = max(self.motion_end, now + duration)
        if not bpy.app.timers.is_registered(self.motion_timer):
            bpy.app.timers.register(self.motion_timer, first_interval=0.0)

    def animation_frame(self):
        area = self.area()
        if area is None or not self.visible():
            return None
        now = monotonic()
        if self.drag:
            self.probe_drop_hover(now)
            if self.drag_overlay:
                self.move_drag_overlay(self.drag_bounds(now))
            else:
                self.redraw_drag(now)
        else:
            area.tag_redraw()
        if self.drop_probe_pending or now < self.motion_end:
            return 1 / (60 if self.drag else 45)
        return None

    def set_cursor(self, inside):
        if inside != self.cursor_owned:
            try:
                if inside:
                    self.window.cursor_modal_set("DEFAULT")
                else:
                    self.window.cursor_modal_restore()
                self.cursor_owned = inside
            except ReferenceError:
                self.cursor_owned = False

    def release(self, stop_timer=True, restore_editor=True):
        BROWSERS.pop(self.area_id, None)
        self.clear_drag()
        self.set_cursor(False)
        if stop_timer and bpy.app.timers.is_registered(self.ticker):
            bpy.app.timers.unregister(self.ticker)
        if bpy.app.timers.is_registered(self.motion_timer):
            bpy.app.timers.unregister(self.motion_timer)
        self.config.close()
        self.painter.close()
        bpy.types.SpaceImageEditor.draw_handler_remove(self.handler, "WINDOW")
        bpy.types.SpaceView3D.draw_handler_remove(self.overlay, "WINDOW")
        area = self.area()
        if restore_editor and area is not None and layout_marker(area) == self.marker:
            editor = area.spaces.active
            for name, visible in zip(VIEW_FLAGS, self.marker.get("visible_regions", (1, 0, 1, 0))):
                setattr(editor, name, bool(visible))
            editor.image = self.marker.get("previous_image")
            area.type = self.marker.get("return_editor", "IMAGE_EDITOR")
            if self.marker.users == 1:
                self.marker.use_fake_user = False
            area.tag_redraw()

    def dismiss(self):
        window_id = self.window.as_pointer() if self.detached else None
        self.release()
        if window_id is None:
            return

        def close_window():
            window = next((item for item in self.manager.windows if item.as_pointer() == window_id), None)
            if window:
                with bpy.context.temp_override(window=window):
                    bpy.ops.wm.window_close()
            return None

        bpy.app.timers.register(close_window, first_interval=0.0)

    def hit(self, x, y, width, height, action):
        self.hits.append((x, y, width, height, action))
        return x <= self.mouse[0] < x + width and y <= self.mouse[1] < y + height

    def action_at(self, point):
        x, y = point
        return next((action for ix, iy, w, h, action in reversed(self.hits)
                     if ix <= x < ix + w and iy <= y < iy + h), None)

    def hovered_card(self):
        if (self.name_editor or self.metadata_editor or self.menu or self.card_menu or self.collection_picker
                or self.detail or self.typing or self.pressed or self.layer_press):
            return None
        action = self.action_at(self.mouse)
        if action and action[0] == "card":
            entry = self.config.entries.get(action[1])
            if entry and self.config.cover(entry):
                return action[1]
        return None

    def clear_hover(self):
        was_visible = self.hover_preview is not None
        self.hover_deadline = self.hover_preview = None
        return was_visible

    def card_level(self, key, now):
        if key == self.card_focus and key is not None:
            return ease_out(now - self.card_motion_started, HOVER_TIME)
        previous, strength = self.card_fade
        if key == previous and key is not None:
            return strength * (1 - ease_out(now - self.card_motion_started, HOVER_TIME))
        return 0.0

    def focus_card(self, key, now):
        if key != self.card_focus:
            self.card_fade = (self.card_focus, self.card_level(self.card_focus, now))
            self.card_focus = key
            self.card_motion_started = now
            self.animate(HOVER_TIME, now)

    def button(self, x, y, width, label, action, active=False, height=32, *, icon=None, primary=False):
        p = self.painter
        hover = self.hit(x, y, width, height, action)
        color = appearance.theme.hover if primary and hover else appearance.theme.accent if primary else (
            HOVER if hover else appearance.theme.dark if active else BUTTON)
        foreground = appearance.theme.ink if primary else appearance.theme.label if active else TEXT
        border = appearance.theme.accent if primary else appearance.theme.border if active or hover else LINE
        p.surface(x, y, width, height, 7, color, border)
        if icon in {"search", "back"}:
            p.icon(icon, x + 14, y + height / 2, foreground if active else MUTED, color)
        elif icon == "chevron":
            p.icon(icon, x + width - 13, y + height / 2, MUTED, color)
        elif icon:
            p.icon(icon, x + width / 2, y + height / 2, foreground, color)
        pad = 27 if icon in {"search", "back"} else 10
        reserve = 24 if icon == "chevron" else 9
        if label and width > pad + reserve:
            p.text(x + pad, y + (height - 11) / 2, label,
                   MUTED if action == ("search", None) and not active and not self.search else foreground,
                   width=width - pad - reserve)

    def folder_button(self, x, y, height, key):
        self.button(x, y, 32, "", ("folder", key), height=height, icon="folder")

    def drop_strength(self, now):
        amount = ease_out(now - self.drop_changed_at, DROP_TRANSITION)
        return self.drop_from + (float(self.drop_hover) - self.drop_from) * amount

    def set_drop_hover(self, valid, now):
        if valid != self.drop_hover:
            self.drop_from = self.drop_strength(now)
            self.drop_changed_at = now
            self.drop_hover = valid
            self.animate(DROP_TRANSITION, now)

    def probe_drop_hover(self, now):
        if self.drop_probe_pending and now >= self.drop_probe_at:
            self.drop_probe_pending = False
            self.drop_probe_at = now + DROP_PROBE_INTERVAL
            try:
                hit = self.drop_target(bpy.context, self.pointer)
            except (ReferenceError, RuntimeError, ValueError):
                hit = None
            self.set_drop_hover(hit is not None, now)

    def drag_bounds(self, now=None):
        now = monotonic() if now is None else now
        level = ease_out(now - self.grab_started, GRAB_TIME)
        scale = (1.0 + 0.045 * level) * (1.0 - (1.0 - DROP_SCALE) * self.drop_strength(now))
        return (round(self.pointer[0] - self.drag.offset_x * scale),
                round(self.pointer[1] - self.drag.offset_y * scale + 4 * level),
                round(self.drag.width * scale), round(self.drag.height * scale))

    def redraw_drag(self, now):
        self.drag_frame = now
        self.drag_pending = False
        targets = set()
        if self.drag and self.visible() and not self.drag_overlay:
            x, y, width, height = self.drag_bounds()
            for area in self.screen.areas:
                if area.type != "VIEW_3D" and area.as_pointer() != self.area_id:
                    continue
                region = region_for(area)
                if region and x < region.x + region.width and x + width > region.x \
                        and y < region.y + region.height and y + height > region.y:
                    targets.add(region)
        for region in self.drag_regions | targets:
            try:
                region.tag_redraw()
            except ReferenceError:
                pass
        self.drag_regions = targets

    def clear_drag(self):
        if self.drag_overlay:
            self.drag_overlay.close()
            self.drag_overlay = None
        self.pressed = self.drag = None
        self.layer_press = self.layer_drag = None
        self.drop_hover = self.drop_probe_pending = False
        self.drop_from = self.drop_changed_at = self.drop_probe_at = 0.0
        self.redraw_drag(monotonic())

    def paint_viewport_drag(self):
        if self.drag and not self.drag_overlay and bpy.context.window == self.window and bpy.context.screen == self.screen:
            self.paint_drag(bpy.context.region)

    def start_drag_overlay(self, bounds, title, preview):
        self.drag_overlay = DragOverlay.create(self.window, bounds, title, preview, appearance.theme.accent)
        if self.drag_overlay:
            self.move_drag_overlay(bounds)

    def move_drag_overlay(self, bounds):
        try:
            self.drag_overlay.move(self.window, bounds, self.pointer, allow_outside=bool(self.detached and self.drag))
        except OSError:
            self.drag_overlay.close()
            self.drag_overlay = None

    def paint_drag(self, region):
        grab = self.drag
        x, y, width, height = self.drag_bounds()
        x, y = x - region.x, y - region.y
        if x >= region.width or y >= region.height or x + width <= 0 or y + height <= 0:
            return
        entry = self.config.entries.get(grab.key)
        if entry:
            self.painter.round_rect(x + 3, y - 5, width + 2, height + 2, 12, MATTE)
            self.paint_card(entry, x, y, width, height, 1.0, drag=True)

    def paint_card(self, entry, x, y, width, height, level=0.0, *, drag=False):
        p = self.painter
        border = appearance.theme.accent if drag else mix_color(LINE, appearance.theme.accent, level * 0.67)
        p.surface(x, y, width, height, 10, mix_color(CARD, HOVER, level * 0.60),
                  border)
        lifted = round(level * 2)
        p.round_rect(x + 5, y + 33, width - 10, height - 38, 6, MATTE)
        if cover := self.config.cover(entry):
            p.image(cover, x + 7, y + 34 + lifted, width - 14, height - 43)
        badges = ["4L"] if entry["kind"] == "4L-mat" else []
        if entry["kind"] == "Decal" or any(catalog.has_alpha(group) for group in entry["groups"].values()):
            badges.append("DL")
        for index, badge in enumerate(badges):
            color = appearance.theme.dark if badge == "4L" else (0.24, 0.18, 0.29, 1)
            label = appearance.theme.label if badge == "4L" else (0.89, 0.77, 0.95, 1)
            p.round_rect(x + 9, y + height - 27 - index * 20, 25, 18, 4, color)
            p.text(x + 15, y + height - 22 - index * 20, badge, label, size=10)
        name = self.config.metadata.get(entry["id"], {}).get("name") or entry["name"]
        p.text(x + 10, y + 12, name, mix_color((0.80, 0.85, 0.86, 1), TEXT, level), width=width - 20)

    def paint(self):
        context = bpy.context
        area = context.area
        if area is None or area.as_pointer() != self.area_id:
            return
        width, height = context.region.width, context.region.height
        self.builder_height = BUILDER_HEIGHT + (36 if width < 420 else 0)
        self.hits = []
        self.painter.begin_frame()
        p = self.painter
        p.rect(0, 0, width, height, BG)
        p.rect(0, height - 59, width, 59, PANEL)
        p.rect(0, height - 59, width, 1, LINE)
        palette_width = 94 if width >= 420 else 51
        logo_width = min(188, width - 89 - palette_width)
        if logo_width > 0:
            p.image(HEADER_LOGO, 17, height - 48, logo_width, 37)
        self.button(width - 54 - palette_width, height - 46, palette_width,
                    tr("4L palette") if width >= 420 else "4L", ("palette_toggle", None), self.builder_open)
        self.button(width - 47, height - 46, 30, "", ("close", None), icon="close")
        if self.detail in self.config.entries:
            self.draw_detail(width, height, self.config.entries[self.detail])
        else:
            self.draw_gallery(width, height)
        if self.builder_open:
            self.draw_builder(width)
        if self.card_menu:
            self.draw_card_menu(width, height)
        if self.collection_picker:
            self.draw_collection_picker(width, height)
        p.rect(0, 0, width, 32, PANEL)
        p.rect(0, 31, width, 1, LINE)
        if self.layer_drag is not None:
            status = tr("Drop on a slot to reorder · Drop in the catalog to remove")
        elif self.drag:
            status = tr("Drop on a palette slot or a mesh") if self.builder_open else tr("Drop on a mesh in another 3D View")
        else:
            status = self.config.status
        action = self.action_at(self.mouse)
        if action and action[0] == "folder" and not self.drag:
            status = tr("Open SVN folder")
        elif action and action[0] == "favorite":
            status = tr("Remove from Favorites") if action[1] in self.config.favorites else tr("Add to Favorites")
        if self.config.busy:
            p.round_rect(17, 13, 6, 6, 3, appearance.theme.accent)
        p.text(31 if self.config.busy else 17, 11, tr(status), MUTED, size=11, width=width - 45)
        if self.drag and not self.drag_overlay:
            self.paint_drag(context.region)
        elif self.layer_drag is not None and not self.drag_overlay:
            self.paint_layer_drag()
        elif self.hover_preview and self.hovered_card() == self.hover_preview:
            self.paint_hover_preview(width, height)
        if self.metadata_editor or self.name_editor:
            self.hits.clear()
            gpu.state.blend_set("ALPHA")
            try:
                p.rect(0, 0, width, height, (0.0, 0.0, 0.0, 0.62))
            finally:
                gpu.state.blend_set("NONE")
            if self.name_editor:
                self.draw_name_editor(width, height)
            else:
                self.draw_metadata_editor(width, height)

    def metadata_field(self, draft, key, x, y, width, placeholder=""):
        field = draft.fields[key]
        p = self.painter
        active = draft.focus == key
        self.hit(x, y, width, 32, ("meta_field", key))
        p.surface(x, y, width, 32, 6, BG, appearance.theme.accent if active else LINE)
        blf.size(0, 13)
        measure = lambda value: blf.dimensions(0, value)[0]
        start = 0
        if active:
            start = field.cursor
            while start > 0 and measure(field.text[start - 1:field.cursor]) <= width - 20:
                start -= 1
        end = max(start, field.cursor) if active else start
        while end < len(field.text) and measure(field.text[start:end + 1]) < width - 19:
            end += 1
        visible = field.text[start:end]
        draft.field_views[key] = (x + 10, start, visible)
        if active:
            a, b = field.selection
            left = measure(field.text[start:max(start, min(a, end))])
            right = measure(field.text[start:max(start, min(b, end))])
            if left < right:
                p.rect(x + 10 + left, y + 7, right - left, 18, appearance.theme.dark)
            caret = x + 10 + measure(field.text[start:field.cursor])
            p.rect(caret, y + 7, 1, 18, appearance.theme.accent)
        p.text(x + 10, y + 10, visible or (placeholder if not field.text else ""), TEXT if visible else MUTED,
               width=width - 18)

    def draw_metadata_editor(self, width, height):
        draft = self.metadata_editor
        p = self.painter
        panel_w = min(584, width - 24)
        panel_h = min(height - 24, 410 if draft.has_layers else 340)
        if panel_w < 175 or panel_h < 130:
            return
        x, y = round((width - panel_w) / 2), round((height - panel_h) / 2)
        top, left, inner = y + panel_h, x + 18, panel_w - 36
        p.surface(x, y, panel_w, panel_h, 12, PANEL, mix_color(LINE, appearance.theme.accent, 0.35))
        p.rect(x + 1, top - 53, panel_w - 2, 1, LINE)
        title = tr("Add tag") if draft.tags_only else tr("Edit tags")
        p.text(left, top - 24, title, size=17, width=inner - 36)
        p.text(left, top - 43, draft.initial.get("name") or draft.entry["name"], MUTED, size=11, width=inner - 36)
        self.button(left + inner - 29, top - 38, 29, "", ("meta_cancel", None), height=25, icon="close")
        minimum = 313 if draft.has_layers else 245
        if panel_h < minimum or panel_w < 234:
            p.text(left, y + 43, tr("Enlarge this area to edit tags"), MUTED, width=inner)
            return
        line = top - 72
        p.text(left, line, tr("Tags"), MUTED, size=11)
        self.metadata_field(draft, "tag", left, line - 38, inner - 66, tr("Type a tag or paste a comma-separated list"))
        self.button(left + inner - 60, line - 38, 60, tr("Add"), ("meta_add", None))
        suggestions = draft.suggestions()
        if suggestions:
            tile = (inner + 5) / len(suggestions)
            for index, tag in enumerate(suggestions):
                self.button(left + tile * index, line - 66, tile - 5, tag, ("meta_suggest", tag), height=22)
        else:
            p.text(left, line - 58, tr("Enter to add"), MUTED, size=11, width=inner)

        tags_bottom = y + (136 if draft.has_layers else 68)
        tags_top = line - 72
        self.draw_tag_chips(draft, left, tags_bottom, inner, max(26, tags_top - tags_bottom))
        if draft.has_layers:
            layers_y = y + 86
            p.text(left, layers_y + 37, tr("Layer order"), MUTED, size=11)
            self.metadata_field(draft, "layers", left, layers_y, inner, tr("Automatic"))
            keys = ", ".join(draft.entry["groups"])
            p.text(left, layers_y - 14, tr("1–4 keys, comma-separated · {keys}", keys=keys), MUTED, size=10, width=inner)

        p.rect(x + 1, y + 56, panel_w - 2, 1, LINE)
        storage = tr("Local 4L preset") if draft.preset else tr("Shared Catalog")
        p.text(left, y + 23, storage, MUTED, size=10, width=max(0, inner - 162))
        if draft.error:
            p.text(left, y + 44, tr(draft.error), (1.0, 0.52, 0.47, 1), size=10, width=inner)
        self.button(left + inner - 155, y + 12, 68, tr("Cancel"), ("meta_cancel", None))
        self.button(left + inner - 79, y + 12, 79, tr("Save"), ("meta_save", None), primary=True)

    def draw_name_editor(self, width, height):
        draft = self.name_editor
        panel_w, panel_h = min(430, width - 24), 160
        if panel_w < 195 or height < panel_h + 16:
            return
        p = self.painter
        x, y = round((width - panel_w) / 2), round((height - panel_h) / 2)
        left, inner = x + 16, panel_w - 32
        p.surface(x, y, panel_w, panel_h, 12, PANEL, mix_color(LINE, appearance.theme.accent, 0.35))
        p.text(left, y + 133, draft.title, size=16, width=inner)
        self.metadata_field(draft, "name", left, y + 83, inner, tr("Enter a name"))
        if draft.error:
            p.text(left, y + 63, tr(draft.error), (1.0, 0.52, 0.47, 1), size=11, width=inner)
        self.button(left + inner - 163, y + 15, 72, tr("Cancel"), ("name_cancel", None))
        self.button(left + inner - 83, y + 15, 83, draft.action, ("name_save", None), primary=True)

    def focus_field(self, draft, key, extend=False):
        field = draft.fields[key]
        x, offset, visible = draft.field_views[key]
        blf.size(0, 13)
        target = self.mouse[0] - x
        position = min(range(len(visible) + 1),
                       key=lambda index: abs(blf.dimensions(0, visible[:index])[0] - target))
        field.select(offset + position, extend and draft.focus == key)
        draft.focus = key

    def save_name_draft(self):
        draft = self.name_editor
        name = draft.fields["name"].text.strip()
        try:
            if draft.collection is None:
                self.config.create_collection(name)
                self.config.status = tr("Created collection: {name}", name=name)
            else:
                self.config.save_preset(name, draft.refs, draft.collection)
                title = dict(self.collection_labels(include_ignore=False))[draft.collection]
                self.collection_picker = None
                self.scroll = 0
                self.config.status = tr("Added to {title}: {name}", title=title, name=name)
        except (TypeError, KeyError, OSError, ValueError) as error:
            draft.error = str(error)
            return
        self.name_editor = None

    def name_event(self, context, event):
        if event.type == "ESC":
            self.name_editor = None
        elif event.type in {"RET", "NUMPAD_ENTER"}:
            self.save_name_draft()
        elif event.type == "LEFTMOUSE":
            action, key = self.action_at(self.mouse) or (None, None)
            if action == "name_cancel":
                self.name_editor = None
            elif action == "name_save":
                self.save_name_draft()
            elif action == "meta_field":
                self.focus_field(self.name_editor, key, event.shift)
        else:
            self.name_editor.fields["name"].key(event, context.window_manager)

    def draw_tag_chips(self, draft, x, y, width, height):
        p = self.painter
        p.surface(x, y, width, height, 6, BG)
        if not draft.tags:
            p.text(x + 8, y + height - 18, tr("No tags yet"), MUTED, size=11)
            return
        blf.size(0, 11)
        rows = [[]]
        offset = 0
        for tag in draft.tags:
            chip_w = min(width - 24, blf.dimensions(0, tag)[0] + (20 if tag in draft.locked else 35))
            if rows[-1] and offset + chip_w > width - 20:
                rows.append([])
                offset = 0
            rows[-1].append((offset, chip_w, tag))
            offset += chip_w + 5
        visible = max(1, (height - 8) // 26)
        draft.scroll_limit = max(0, len(rows) - visible)
        draft.scroll = min(draft.scroll, draft.scroll_limit)
        for index, row in enumerate(rows[draft.scroll:draft.scroll + visible]):
            chip_y = y + height - 24 - index * 26
            for offset, chip_w, tag in row:
                chip_x = x + 6 + offset
                removable = tag not in draft.locked
                hovered = removable and self.hit(chip_x, chip_y, chip_w, 20, ("meta_remove", tag))
                p.surface(chip_x, chip_y, chip_w, 20, 6, HOVER if hovered else appearance.theme.dark)
                p.text(chip_x + 7, chip_y + 5, tag, appearance.theme.label, size=11, width=chip_w - (25 if removable else 14))
                if removable:
                    p.icon("close", chip_x + chip_w - 10, chip_y + 10, MUTED)
        if draft.scroll_limit:
            track = max(1, height - 12)
            knob = max(7, track * visible / len(rows))
            pos = (track - knob) * (1 - draft.scroll / draft.scroll_limit)
            p.round_rect(x + width - 7, y + 6 + pos, 3, knob, 2, MUTED)

    def save_metadata_draft(self):
        draft = self.metadata_editor
        try:
            self.config.save_metadata(draft.key, draft.initial, draft.fields_to_save())
        except (TypeError, KeyError, OSError, ValueError) as error:
            draft.error = str(error)
            return
        self.config.status = tr("Tags saved") if draft.tags_only else tr("Material metadata saved")
        self.metadata_editor = None

    def metadata_event(self, context, event, inside, previous_mouse):
        draft = self.metadata_editor
        if event.type == "WINDOW_DEACTIVATE":
            self.set_cursor(False)
            return False
        if event.type in {"MOUSEMOVE", "INBETWEEN_MOUSEMOVE"}:
            self.set_cursor(inside)
            return self.action_at(previous_mouse) != self.action_at(self.mouse)
        if event.value != "PRESS":
            return False
        if self.name_editor:
            if inside or event.type == "ESC":
                self.name_event(context, event)
                return True
            return False
        if event.type == "ESC":
            self.metadata_editor = None
            return True
        if not inside:
            return False
        kind = event.type
        if kind == "LEFTMOUSE":
            action, value = self.action_at(self.mouse) or (None, None)
            if action == "meta_cancel":
                self.metadata_editor = None
            elif action == "meta_save":
                self.save_metadata_draft()
            elif action == "meta_field":
                self.focus_field(draft, value, event.shift)
            elif action in {"meta_suggest", "meta_add"}:
                draft.add_tags(value)
            elif action == "meta_remove":
                draft.tags.remove(value)
        elif kind in {"WHEELUPMOUSE", "WHEELDOWNMOUSE"}:
            direction = 1 if kind == "WHEELDOWNMOUSE" else -1
            draft.scroll = max(0, min(draft.scroll_limit, draft.scroll + direction))
        elif kind in {"RET", "NUMPAD_ENTER"} and event.ctrl:
            self.save_metadata_draft()
        elif kind in {"RET", "NUMPAD_ENTER"} and draft.focus == "tag":
            draft.add_tags()
        elif kind in {"TAB", "RET", "NUMPAD_ENTER"}:
            order = ["tag"]
            if draft.has_layers:
                order.append("layers")
            offset = -1 if event.shift else 1
            draft.focus = order[(order.index(draft.focus) + offset) % len(order)]
        else:
            draft.fields[draft.focus].key(event, context.window_manager)
        return True

    @property
    def content_bottom(self):
        return 32 + self.builder_height if self.builder_open else 32

    def draft_name(self):
        names = [self.config.entries[source]["groups"][group]["name"]
                 for source, group in self.layer_refs if source in self.config.entries
                 and group in self.config.entries[source]["groups"]]
        return ("4L — " + " + ".join(names))[:95]

    def draft_record(self):
        return layers.compose(self.config.entries, self.layer_refs, layers.DRAFT_ID, self.draft_name())

    def builder_entry(self, index):
        source, group = self.layer_refs[index]
        entry = self.config.entries.get(source)
        if not entry or group not in entry["groups"]:
            return tr("Missing layer"), None
        texture = entry["groups"][group]["maps"]["Diffuse"][0]
        preview = self.config.map_previews.get(texture, {}).get("path")
        label = entry["groups"][group]["name"] if entry["kind"] == "4L-mat" else (
            self.config.metadata.get(source, {}).get("name") or entry["name"])
        return label, preview if entry["kind"] == "4L-mat" and preview else self.config.cover(entry)

    def add_to_builder(self, key, slot=None):
        entry = self.config.entries[key]
        refs = layers.references(entry, self.config.metadata.get(key, {}))
        if entry["kind"] == "4L-mat":
            if slot is not None and self.layer_refs:
                raise ValueError(tr("Use Load layers into 4L palette to replace the current mix."))
            self.layer_refs = refs
            for source, group in refs:
                component = self.config.entries.get(source)
                if component and component["kind"] == "4L-mat" and group in component["groups"]:
                    self.config.preview(component["groups"][group]["maps"]["Diffuse"][0])
        elif len(self.layer_refs) < layers.LIMIT:
            self.layer_refs.insert(len(self.layer_refs) if slot is None else slot, refs[0])
        else:
            raise ValueError(tr("All four slots are filled. Remove a layer first."))
        self.builder_open = True
        self.config.status = tr("Loaded {name}", name=entry["name"]) if entry["kind"] == "4L-mat" else tr("Added {name}", name=entry["name"])

    def draw_card_menu(self, width, height):
        key, (pointer_x, pointer_y) = self.card_menu
        entry = self.config.entries.get(key)
        if not entry:
            return
        menu_w, menu_h = min(257, width - 24), 257
        x = min(max(12, pointer_x - 10), width - menu_w - 12)
        y = min(max(37, pointer_y - menu_h + 9), height - menu_h - 16)
        p = self.painter
        p.round_rect(x + 3, y - 4, menu_w + 1, menu_h + 1, 12, MATTE)
        p.surface(x, y, menu_w, menu_h, 10, PANEL, mix_color(LINE, appearance.theme.accent, 0.22))
        header = y + menu_h - 38
        title = self.config.metadata.get(key, {}).get("name") or entry["name"]
        self.button(x + 8, header, menu_w - 16, title, ("popup_details", key), height=29, icon="chevron")
        p.rect(x + 9, header - 5, menu_w - 18, 1, LINE)
        rows = [
            (tr("Apply to selected"), "apply"),
            (tr("Add to collection"), "collections"),
            (tr("Open first layer on SVN") if entry.get("layer_refs") else tr("Open on SVN"), "folder"),
            (tr("Add tag"), "add_tag"),
            (tr("Load layers into 4L palette") if entry["kind"] == "4L-mat" else tr("Add to 4L palette"), "builder_add"),
            (tr("Remove from Ignore List") if key in self.config.ignored else tr("To Ignore List"), "ignore"),
        ]
        for index, (label, action) in enumerate(rows, 1):
            self.button(x + 8, header - 7 - 33 * index, menu_w - 16, label, ("popup_" + action, key),
                        height=29, primary=action == "apply")

    def collection_labels(self, include_ignore=True):
        return [(key, tr(name) if key in {state.FAVORITES, state.IGNORE_LIST} else name)
                for key, name in self.config.collection_options(include_ignore)]

    def draw_collection_picker(self, width, height):
        key, (pointer_x, pointer_y) = self.collection_picker
        palette = key == layers.DRAFT_ID
        entry = self.config.entries.get(key)
        if (not palette and not entry) or height < 120:
            return
        choices = self.collection_labels(include_ignore=False)
        visible = min(len(choices), max(1, (height - 128) // 33), 10)
        self.collection_scroll = min(self.collection_scroll, len(choices) - visible)
        menu_w, menu_h = min(296, width - 24), 79 + 33 * visible
        x = min(max(12, pointer_x - 10), width - menu_w - 12)
        y = min(max(37, pointer_y - menu_h + 9), height - menu_h - 16)
        p = self.painter
        p.surface(x, y, menu_w, menu_h, 10, PANEL, mix_color(LINE, appearance.theme.accent, 0.25))
        title = self.draft_name() if palette else self.config.metadata.get(key, {}).get("name") or entry["name"]
        p.text(x + 12, y + menu_h - 23, tr("Add to collection"), TEXT, size=15, width=menu_w - 24)
        p.text(x + 12, y + menu_h - 42, title, MUTED, size=11, width=menu_w - 24)
        for index, (collection, label) in enumerate(choices[self.collection_scroll:self.collection_scroll + visible]):
            selected = key in self.config.collection_members(collection)
            label = tr("Remove from {title}", title=label) if selected else label
            self.button(x + 8, y + menu_h - 80 - 33 * index, menu_w - 16, label,
                        ("collection_member", collection), active=selected, height=28)
        self.button(x + 8, y + 6, menu_w - 16, tr("+ Collection"), ("collection_create", None), height=26)

    def draw_builder(self, width):
        p = self.painter
        x, y = 12, 37
        wide = width >= 420
        extra = self.builder_height - BUILDER_HEIGHT
        p.surface(x, y, width - 24, self.builder_height - 9, 10, PANEL, mix_color(LINE, appearance.theme.accent, 0.25))
        p.text(x + 12, y + 105 + extra, tr("4L palette"), size=15, width=110)
        p.text(x + 113, y + 106 + extra, f"{len(self.layer_refs)}/4", appearance.theme.label, size=11)
        self.button(width - 48, y + 94 + extra, 27, "", ("builder_close", None), height=26, icon="close")
        action_width = 143 if wide else max(60, (width - 50) // 2)
        start = x + 9
        slots_space = width - 2 * start - (action_width + 31 if wide else 18)
        slot_width = max(27, slots_space // 4)
        slot_y, slot_height = y + 10 + extra, 77
        for index in range(layers.LIMIT):
            slot_x = start + index * (slot_width + 6)
            hover = self.hit(slot_x, slot_y, slot_width, slot_height, ("layer_slot", index))
            active = index < len(self.layer_refs)
            fill = CARD if active else BG
            border = appearance.theme.accent if hover and self.layer_drag is not None else LINE
            p.surface(slot_x, slot_y, slot_width, slot_height, 7, fill, border)
            if active and index != self.layer_drag:
                name, preview = self.builder_entry(index)
                p.image(preview, slot_x + 6, slot_y + 19, slot_width - 12, slot_height - 25)
                p.text(slot_x + 6, slot_y + 7, name, size=10, width=slot_width - 12)
            p.round_rect(slot_x + 4, slot_y + slot_height - 21, 15, 16, 4, appearance.theme.dark)
            p.text(slot_x + 8, slot_y + slot_height - 17, str(index + 1), appearance.theme.label, size=10)
            if active:
                self.button(slot_x + slot_width - 24, slot_y + slot_height - 23, 20, "",
                            ("layer_remove", index), height=19, icon="close")
        right = width - x - 9 - action_width
        self.button(right if wide else start, y + 52 if wide else y + 8, action_width,
                    tr("Apply to selected") if wide or width >= 330 else tr("Apply"),
                    ("builder_apply", None), height=34, primary=bool(self.layer_refs))
        self.button(right, y + 11 if wide else y + 8, action_width,
                    tr("Add to collection") if wide or width >= 330 else tr("Collection"),
                    ("builder_save", None), height=34)

    def paint_layer_drag(self):
        name, preview = self.builder_entry(self.layer_drag)
        x, y = self.mouse
        p = self.painter
        p.surface(x - 38, y - 42, 76, 79, 8, CARD, appearance.theme.accent)
        p.image(preview, x - 29, y - 25, 58, 56)
        p.text(x - 31, y - 35, name, size=10, width=62)

    def layer_slot_at(self, x, y):
        return next((action[1] for lx, ly, lw, lh, action in self.hits
                     if action[0] == "layer_slot" and lx <= x < lx + lw and ly <= y < ly + lh), None)

    def layer_drag_bounds(self):
        return self.pointer[0] - 38, self.pointer[1] - 42, 76, 79

    def paint_hover_preview(self, width, height):
        margin, padding, gap = 12, 8, 18
        edge = min(320, width - 2 * (margin + padding), height - 2 * margin - 40)
        if edge < 64:
            return
        box_width, box_height = edge + 2 * padding, edge + 40
        mx, my = self.mouse
        action = ("card", self.hover_preview)
        cx, cy, card_width, card_height, _ = next(hit for hit in self.hits if hit[-1] == action)
        x, y = cx + card_width + gap, my - box_height // 2
        if x + box_width > width - margin:
            x = cx - gap - box_width
        if x < margin:
            x = mx - box_width // 2
            above = cy + card_height + gap
            y = above if above + box_height <= height - margin else cy - gap - box_height
        x = min(width - margin - box_width, max(margin, x))
        y = min(height - margin - box_height, max(margin, y))
        entry = self.config.entries[self.hover_preview]
        name = self.config.metadata.get(self.hover_preview, {}).get("name") or entry["name"]
        p = self.painter
        level = ease_out(monotonic() - self.popup_started, POPUP_TIME)
        inset = round((1 - level) * 8)
        lift = round((1 - level) * 7)
        x, y = x + inset, max(margin, y + inset - lift)
        box_width, box_height = box_width - 2 * inset, box_height - 2 * inset
        edge -= 2 * inset
        p.round_rect(x + 3, y - 5, box_width + 3, box_height + 3, 14, MATTE)
        p.surface(x, y, box_width, box_height, 12, CARD, mix_color(LINE, appearance.theme.accent, 0.30))
        p.round_rect(x + padding, y + 32, edge, edge, 6, MATTE)
        p.image(self.config.cover(entry), x + padding + 2, y + 34, edge - 4, edge - 4)
        p.text(x + padding + 2, y + 12, name, size=14, width=edge - 4)

    def draw_gallery(self, width, height):
        p = self.painter
        y = height - 109
        self.button(16, y, max(60, width - 32), self.search + ("  |" if self.typing else "") if self.search or self.typing else tr("Search names or tags"), ("search", None), self.typing, 36, icon="search")
        y -= 42
        collection_width = min(148, max(98, width // 3))
        filter_width = max(38, (width - collection_width - 46) // 2)
        category_name = tr("Buildings") if self.category == DEFAULT_CATEGORY else self.category
        self.button(16, y, filter_width, category_name or tr("All folders"), ("menu", "category"),
                    bool(self.category), icon="chevron")
        self.button(23 + filter_width, y, filter_width, self.tag or tr("All tags"), ("menu", "tag"),
                    bool(self.tag), icon="chevron")
        collections = dict(self.collection_labels())
        if self.collection and self.collection not in collections:
            self.collection = ""
        collection_name = collections.get(self.collection, tr("All collections"))
        self.button(30 + filter_width * 2, y, collection_width, collection_name,
                    ("menu", "collection"), bool(self.collection), icon="chevron")
        result_key = (self.search, self.category, self.tag, self.collection, self.config.stamps)
        if result_key != self.results_key:
            self.results_key = result_key
            self.results = list(self.config.matches(self.search, self.category, self.tag, self.collection))
        results = self.results
        columns = max(1, (width - 22) // round(142 * self.zoom))
        step_x = (width - 32) // columns
        edge = max(30, min(step_x - 8, round(236 * self.zoom)))
        grid_top = y - 11
        row_height = self.row_height = edge + 33
        self.gallery_columns = columns
        if self.zoom_anchor is not None:
            self.scroll = (self.zoom_anchor // columns) * row_height
            self.zoom_anchor = None
        viewport_height = max(0, grid_top - self.content_bottom)
        rows = math.ceil(len(results) / columns)
        visible_rows = viewport_height // row_height
        self.limit = max(0, rows - max(1, visible_rows)) * row_height
        self.scroll = min(self.scroll, self.limit)
        now = monotonic()
        start_row = self.scroll // row_height
        end_row = start_row + visible_rows
        for index in range(start_row * columns, min(len(results), end_row * columns)):
            row, column = divmod(index, columns)
            entry = results[index]
            key = entry["id"]
            x = 16 + column * step_x
            card_y = grid_top - (row + 1) * row_height + self.scroll + 5
            top = card_y + row_height - 7
            if top > grid_top or card_y < self.content_bottom:
                continue
            card_height = row_height - 7
            hovered = self.hit(x, card_y, edge, card_height, ("card", key))
            if self.drag and self.drag.key == key:
                p.surface(x, card_y, edge, card_height, 10, PANEL, mix_color(LINE, appearance.theme.accent, 0.25))
            else:
                self.paint_card(entry, x, card_y, edge, card_height, self.card_level(key, now))
            if hovered and not self.drag:
                favorite = key in self.config.favorites
                self.button(x + edge - 73, top - 28, 32, "", ("favorite", key), active=favorite,
                            height=24, icon="heart_filled" if favorite else "heart")
                self.folder_button(x + edge - 36, top - 28, 24, key)
        if not results and self.config.entries:
            p.text(16, grid_top - 23, tr("No matching materials"), MUTED)
        elif viewport_height < row_height and results:
            p.text(16, grid_top - 23, tr("Enlarge this area to display cards"), MUTED)
        if self.limit and viewport_height > 0:
            height_fraction = viewport_height / (viewport_height + self.limit)
            track = max(0, viewport_height - 8)
            thumb = min(track, max(23, round(track * height_fraction)))
            p.round_rect(width - 8, self.content_bottom + 1, 3, track, 2, PANEL)
            p.round_rect(width - 8, self.content_bottom + 1 + round((track - thumb) * (1 - self.scroll / self.limit)),
                         3, thumb, 2, mix_color(LINE, appearance.theme.accent, 0.35))
        if self.menu:
            if self.menu == "collection":
                choices = [("", tr("All collections")), ("@create", tr("+ Collection")), *self.collection_labels()]
            else:
                values = (sorted({entry["category"] for entry in self.config.entries.values()})
                          if self.menu == "category" else self.config.tag_vocabulary)
                choices = [("", tr("All folders") if self.menu == "category" else tr("All tags"))] + [(value, tr("Buildings") if self.menu == "category" and value == DEFAULT_CATEGORY else value)
                                           for value in values]
            available = min(len(choices), max(1, (grid_top - self.content_bottom - 3) // 29))
            self.menu_scroll = min(self.menu_scroll, len(choices) - available)
            menu_width = min(width - 32, 230)
            anchor = {"category": 16, "tag": 23 + filter_width, "collection": 30 + filter_width * 2}
            menu_x = min(anchor[self.menu], width - menu_width - 16)
            p.surface(menu_x, grid_top - available * 29 - 4, menu_width, available * 29 + 4, 8, CARD)
            for index, (choice, label) in enumerate(choices[self.menu_scroll:self.menu_scroll + available]):
                self.button(menu_x + 3, grid_top - 29 * (index + 1), menu_width - 6, label, ("choice", choice), height=26)

    def draw_detail(self, width, height, entry):
        p = self.painter
        key = entry["id"]
        data = self.config.metadata.get(key, {})
        right = width - 16
        toolbar = height - 101
        self.button(16, toolbar, 86 if appearance.language == "RU" else 72, tr("Back"), ("back", None), icon="back")
        apply_width = 94 if appearance.language == "RU" else 68
        self.button(right - apply_width, toolbar, apply_width, tr("Apply"), ("apply", key), primary=True)
        self.button(right - apply_width - 80, toolbar, 73, tr("Edit tags"), ("edit", key))
        self.folder_button(right - apply_width - 120, toolbar, 32, key)

        title = data.get("name") or entry["name"]
        p.text(17, toolbar - 32, title, size=25, width=width - 34)
        p.text(18, toolbar - 54, f'{tr(entry["kind"])}   ·   {tr("Buildings") if entry["category"] == DEFAULT_CATEGORY else entry["category"]}   ·   {entry.get("size") or "—"}',
               appearance.theme.label, size=12, width=width - 36)
        p.text(18, toolbar - 74, tr("Tags") + "  " + (", ".join(data.get("tags", [])) or "—"),
               MUTED, size=11, width=width - 36)
        self.draw_author(entry, right, toolbar - 101)

        floor = self.content_bottom + 9
        top = toolbar - 111
        available = top - floor
        if available < 80:
            return
        tile_h = min(92, max(42, available // 4))
        frame_y = floor + tile_h + 33
        frame_h = top - frame_y
        p.surface(16, frame_y, width - 32, frame_h, 11, MATTE)
        selection = self.map
        cover = self.config.cover(entry)
        preview = self.config.map_previews.get(selection, {})
        path = ((self.config.gallery_covers.get(key) or cover) if selection is None
                else preview.get("path"))
        if selection == "@live":
            self.draw_live(entry, (21, frame_y + 5, width - 42, frame_h - 10))
        elif path and frame_h > 15:
            p.image(path, 21, frame_y + 5, width - 42, frame_h - 10)
        elif frame_h > 28:
            message = preview.get("error") or (tr("Loading texture…") if selection else tr("No cover available"))
            p.text(27, frame_y + frame_h / 2, message, MUTED, width=width - 54)

        groups = entry["groups"].values()
        maps = [(f'{group["name"]} · {tr(channel)}' if entry["kind"] == "4L-mat" else tr(channel), relative)
                for group in groups for channel, paths in group["maps"].items()
                if channel != "Preview" for relative in paths]
        label = {None: tr("Cover"), "@live": tr("Interactive 3D")}.get(selection)
        if label is None:
            channel = next((name for name, path in maps if path == selection), tr("Texture"))
            label = f'{channel}  ·  {Path(selection).name}  ·  {preview.get("size") or "—"}'
        p.text(18, frame_y - 19, label, MUTED, size=11, width=width - 36)
        tiles = [("3D", "@live", ("live", key), None), (tr("Cover"), None, ("cover", None), cover)]
        for name, relative in maps:
            thumb = self.config.map_thumbnails.get(relative, {})
            full = self.config.map_previews.get(relative, {})
            tiles.append((name, relative, ("map", relative), thumb.get("path") or full.get("path")))

        strip_width = width - 90
        count = max(1, strip_width // 79)
        self.detail_limit = max(0, len(tiles) - count)
        self.detail_scroll = min(self.detail_scroll, self.detail_limit)
        visible = tiles[self.detail_scroll:self.detail_scroll + count]
        self.config.preview_strip([value for _, value, (action, _), _ in visible if action == "map"])
        if self.detail_scroll:
            self.button(16, floor, 23, "", ("strip_scroll", -1), height=tile_h, icon="previous")
        if self.detail_scroll < self.detail_limit:
            self.button(right - 23, floor, 23, "", ("strip_scroll", 1), height=tile_h, icon="next")
        tile_w = min(128, strip_width / count)
        for index, (name, value, action, thumb) in enumerate(visible):
            tile_x = 45 + tile_w * index
            w = tile_w - 6
            hover = self.hit(tile_x, floor, w, tile_h, action)
            p.surface(tile_x, floor, w, tile_h, 7, HOVER if hover else PANEL,
                      appearance.theme.accent if value == selection else LINE)
            if thumb:
                p.image(thumb, tile_x + 4, floor + 20, w - 8, tile_h - 24)
            elif value == "@live":
                p.text(tile_x + w / 2 - 9, floor + tile_h / 2 + 2, "↻", appearance.theme.accent, size=21)
            p.text(tile_x + 6, floor + 6, name, appearance.theme.label if value == selection else TEXT, size=11, width=w - 12)

    def draw_author(self, entry, right, y):
        key = entry["id"]
        author = self.config.authors.get(key)
        if entry.get("layer_refs"):
            label = tr("Shared 4L preset") if key in self.config.shared_presets else tr("Local 4L preset")
        elif author:
            label = f'{tr("First SVN upload")}  {author["author"] or tr("Unknown")} · r{author["revision"]}'
        elif self.config.author_errors.get(key):
            label = tr("First SVN upload · author unavailable")
        else:
            label = tr("First SVN upload · use Read in settings")
        self.painter.text(18, y + 6, label, MUTED, size=11, width=right - 18)

    def live_group(self, entry):
        keys = list(entry["groups"])
        if entry["kind"] == "4L-mat":
            try:
                keys = layers.layer_keys(entry, self.config.metadata.get(entry["id"], {}))
            except ValueError:
                keys = layers.layer_keys(entry, {})
        return entry["groups"][keys[self.live_layer % len(keys)]], len(keys)

    def load_live(self, entry):
        group, _ = self.live_group(entry)
        for channel in reversed(CHANNELS):
            if paths := group["maps"].get(channel):
                self.config.preview(paths[0])

    def reset_live(self):
        self.live_angles = (0.0, 0.0)
        self.live_zoom = 1.0
        self.live_drag = None

    def draw_live(self, entry, bounds):
        x, y, width, height = bounds
        if min(width, height) < 20:
            return
        group, count = self.live_group(entry)
        textures = {}
        failed = ""
        for channel in CHANNELS:
            if paths := group["maps"].get(channel):
                preview = self.config.map_previews.get(paths[0], {})
                if texture := self.painter.image_texture(preview.get("path")):
                    textures[channel] = texture
                if channel == "Diffuse":
                    failed = preview.get("error", "")
        if not self.live_error:
            try:
                self.live.draw((x + width / 2, y + height / 2), min(width, height) / 2, textures,
                               self.live_angles, self.live_zoom, self.live_plane,
                               entry["kind"] == "Decal" or catalog.has_alpha(group))
            except (RuntimeError, ValueError) as error:
                self.live_error = str(error)
        self.hit(x, y, width, height, ("orbit", None))
        if height > 44:
            self.button(x + 5, y + height - 30, 65, tr("Plane") if self.live_plane else tr("Sphere"),
                        ("live_shape", None), height=25)
            self.button(x + 76, y + height - 30, 55, tr("Reset"), ("live_reset", None), height=25)
            if count > 1:
                self.button(x + 137, y + height - 30, 91, tr("Layer {index}/{count}", index=self.live_layer + 1, count=count),
                            ("live_layer", None), height=25)
            message = self.live_error or failed or (tr("Loading texture maps…") if not textures else
                                                    tr("Drag to orbit  ·  Scroll to zoom"))
            self.painter.text(x + 6, y + 7, message, MUTED, size=11, width=width - 12)

    def apply(self, context, key, obj):
        if obj is None:
            self.config.status = tr("Choose a mesh in Object Mode.")
            return
        result = bpy.ops.rtc.apply_catalog_material("EXEC_DEFAULT", True, key=key,
                                                    target=obj.name, browser_id=str(self.area_id))
        if "FINISHED" in result:
            self.config.status = tr("Applied to {name}", name=obj.name)

    def target_window(self):
        return next((window for window in self.manager.windows if window.as_pointer() == self.owner_id), self.window)

    def request_undo(self, redo):
        def run():
            if BROWSERS.get(self.area_id) is not self:
                return None
            window = self.target_window()
            area = next((area for area in window.screen.areas if area.type == "VIEW_3D"), None)
            if area is None and window == self.window:
                area = self.area()
            with bpy.context.temp_override(window=window, area=area, region=region_for(area) if area else None):
                operation = bpy.ops.ed.redo if redo else bpy.ops.ed.undo
                if operation.poll():
                    operation()
            return None

        # Undo changes Blender data and may cancel modal handlers. Run it after this event returns.
        bpy.app.timers.register(run, first_interval=0.0)

    def apply_selected(self, context, key):
        target = self.target_window()
        with context.temp_override(window=target):
            layer = context.view_layer
            obj = layer.objects.active
            self.apply(context, key, obj if obj and obj.select_get(view_layer=layer) else None)

    def drop_target(self, context, pointer):
        target = self.target_window()
        x, y = pointer
        if target != self.window:
            x += self.window.x - target.x
            y += self.window.y - target.y
        with context.temp_override(window=target):
            return drop_mesh(context, x, y, self.area_id)

    def apply_drop(self, context, key, x, y):
        obj = self.drop_target(context, (x, y))
        with context.temp_override(window=self.target_window()):
            self.apply(context, key, obj)

    def initial_category(self):
        return DEFAULT_CATEGORY if (not self.config.entries or any(
            entry["category"] == DEFAULT_CATEGORY for entry in self.config.entries.values())) else ""

    def click(self, context, action):
        name, value = action
        if self.collection_picker:
            if name == "collection_create":
                self.name_editor = NameEditor(tr("New collection"), tr("Create"))
                return
            key, _ = self.collection_picker
            if name == "collection_member" and key == layers.DRAFT_ID:
                title = dict(self.collection_labels(include_ignore=False))[value]
                self.name_editor = NameEditor(tr("Add 4L preset to {title}", title=title), tr("Add"), name=self.draft_name(),
                                              collection=value, refs=tuple(self.layer_refs))
                self.name_editor.fields["name"].anchor = 0
                return
            self.collection_picker = None
            if name == "collection_member":
                label = dict(self.collection_labels())[value]
                was_member = key in self.config.collection_members(value)
                self.config.toggle_collection_material(value, key)
                self.config.status = tr("Removed from {title}", title=label) if was_member else tr("Added to {title}", title=label)
            return
        if self.card_menu:
            self.card_menu = None
            if not name.startswith("popup_"):
                return
            name = name.removeprefix("popup_")
        self.typing = name == "search"
        if self.menu and name not in ("menu", "choice"):
            self.menu = None
            return
        if name == "menu":
            self.menu = None if self.menu == value else value
            self.menu_scroll = 0
        elif name == "choice":
            menu = self.menu
            self.menu = None
            if menu == "collection" and value == "@create":
                self.name_editor = NameEditor(tr("New collection"), tr("Create"))
                return
            setattr(self, menu, value)
            if menu == "collection" and value:
                self.category = ""
            self.scroll = 0
        elif name == "collections":
            self.collection_picker = (value, self.mouse)
            self.collection_scroll = 0
        elif name == "favorite":
            self.config.toggle_favorite(value)
        elif name == "ignore":
            hidden = value not in self.config.ignored
            self.config.set_ignored(value, hidden)
            self.config.status = tr("Moved to Ignore List") if hidden else tr("Removed from Ignore List")
        elif name == "folder":
            open_svn_folder(self.config.root, self.config.entries[value])
        elif name in {"card", "details"}:
            self.detail, self.map, self.detail_scroll = value, None, 0
            entry = self.config.entries[value]
            self.config.gallery_cover(entry)
            self.detail_opened_at = monotonic()
        elif name == "back":
            self.detail, self.map = None, None
        elif name == "cover":
            self.map = None
        elif name == "map":
            self.map = value
            self.config.preview(value)
        elif name == "strip_scroll":
            self.detail_scroll = min(self.detail_limit, max(0, self.detail_scroll + value))
        elif name == "live":
            self.map = "@live"
            self.live_plane = self.config.entries[value]["kind"] == "Decal"
            self.live_layer = 0
            self.reset_live()
            self.load_live(self.config.entries[value])
        elif name == "live_shape":
            self.live_plane = not self.live_plane
            self.reset_live()
        elif name == "live_reset":
            self.reset_live()
        elif name == "live_layer":
            entry = self.config.entries[self.detail]
            self.live_layer = (self.live_layer + 1) % self.live_group(entry)[1]
            self.load_live(entry)
        elif name == "apply":
            self.apply_selected(context, value)
        elif name in {"edit", "add_tag"}:
            self.metadata_editor = MetadataEditor(self.config, value, tags_only=name == "add_tag")
            self.clear_hover()
        elif name == "builder_add":
            self.add_to_builder(value)
        elif name == "builder_close":
            self.builder_open = False
        elif name == "palette_toggle":
            self.builder_open = not self.builder_open
        elif name == "layer_remove":
            self.layer_refs.pop(value)
        elif name == "builder_apply":
            self.draft_record()
            self.apply_selected(context, layers.DRAFT_ID)
        elif name == "builder_save":
            self.draft_record()
            self.collection_picker = (layers.DRAFT_ID, self.mouse)
            self.collection_scroll = 0

    def modal(self, context, event):
        if self.area_id not in BROWSERS:
            return {"CANCELLED"}
        area = self.area()
        if area is None or area.type != "IMAGE_EDITOR" or not self.visible():
            return {"CANCELLED"}
        if event.type == "TIMER":
            return {"PASS_THROUGH"}
        region = region_for(area)
        if region is None:
            return {"PASS_THROUGH"}
        previous_mouse = self.mouse
        self.pointer = event.mouse_x, event.mouse_y
        self.mouse = self.pointer[0] - region.x, self.pointer[1] - region.y
        x, y = self.mouse
        inside = 0 <= x < region.width and 0 <= y < region.height
        if self.metadata_editor or self.name_editor:
            if self.metadata_event(context, event, inside, previous_mouse):
                area.tag_redraw()
            return {"RUNNING_MODAL"} if inside else {"PASS_THROUGH"}
        if inside and event.type == "Z" and (event.ctrl or event.oskey) and not self.typing:
            if event.value == "PRESS":
                self.request_undo(event.shift)
            return {"RUNNING_MODAL"}
        if self.live_drag:
            if event.type in {"MOUSEMOVE", "INBETWEEN_MOUSEMOVE"}:
                yaw, pitch = self.live_angles
                self.live_angles = ((yaw - (x - previous_mouse[0]) * 0.009) % math.tau,
                                    max(-1.48, min(1.48, pitch + (y - previous_mouse[1]) * 0.009)))
                area.tag_redraw()
                return {"RUNNING_MODAL"}
            if event.type in {"ESC", "WINDOW_DEACTIVATE"} or (event.type == self.live_drag and event.value == "RELEASE"):
                self.live_drag = None
                return {"PASS_THROUGH"} if event.type == "WINDOW_DEACTIVATE" else {"RUNNING_MODAL"}
        if (inside and self.map == "@live" and event.type in {"LEFTMOUSE", "MIDDLEMOUSE"}
                and event.value == "PRESS" and self.action_at(self.mouse) == ("orbit", None)):
            self.live_drag = event.type
            return {"RUNNING_MODAL"}
        if event.type in {"MOUSEMOVE", "INBETWEEN_MOUSEMOVE"}:
            self.set_cursor(3 <= x < region.width - 3 and 3 <= y < region.height - 3)
            if self.layer_press:
                index, start = self.layer_press
                if self.layer_drag is None and math.dist(start, self.mouse) > 5:
                    self.layer_drag = index
                    name, preview = self.builder_entry(index)
                    self.start_drag_overlay(self.layer_drag_bounds(), name, preview)
                if self.layer_drag is not None:
                    if self.drag_overlay:
                        self.move_drag_overlay(self.layer_drag_bounds())
                    if not self.drag_overlay or self.action_at(previous_mouse) != self.action_at(self.mouse):
                        area.tag_redraw()
                return {"RUNNING_MODAL"}
            hover_hidden = False
            if self.mouse != previous_mouse:
                hover_hidden = self.clear_hover()
                if inside and self.hovered_card():
                    self.hover_deadline = monotonic() + HOVER_PREVIEW_DELAY
            if self.pressed and not self.drag and math.dist((self.pressed.x, self.pressed.y), self.pointer) > 9:
                self.drag = self.pressed
                self.drag_frame = 0.0
                self.grab_started = monotonic()
                self.focus_card(None, self.grab_started)
                self.animate(GRAB_TIME, self.grab_started)
                entry = self.config.entries[self.drag.key]
                title = self.config.metadata.get(self.drag.key, {}).get("name") or entry["name"]
                self.start_drag_overlay(self.drag_bounds(), title, self.config.cover(entry))
                area.tag_redraw()
            if self.drag:
                now = monotonic()
                if inside:
                    self.drop_probe_pending = False
                    self.set_drop_hover(False, now)
                elif self.mouse != previous_mouse:
                    self.drop_probe_pending = True
                    self.probe_drop_hover(now)
                    if self.drop_probe_pending:
                        self.animate(self.drop_probe_at - now, now)
                if self.drag_overlay:
                    if now >= self.motion_end:
                        self.move_drag_overlay(self.drag_bounds(now))
                    if self.builder_open and self.action_at(previous_mouse) != self.action_at(self.mouse):
                        area.tag_redraw()
                elif now - self.drag_frame >= 1 / 60:
                    self.redraw_drag(now)
                else:
                    self.drag_pending = True
            else:
                unobstructed = not (self.detail or self.menu or self.card_menu or self.collection_picker)
                target = self.action_at(self.mouse) if inside and unobstructed else None
                self.focus_card(target[1] if target and target[0] in {"card", "favorite", "folder"} else None,
                                monotonic())
                if hover_hidden or self.action_at(previous_mouse) != self.action_at(self.mouse):
                    area.tag_redraw()
            return {"RUNNING_MODAL"} if inside or self.drag else {"PASS_THROUGH"}
        if event.type == "WINDOW_DEACTIVATE":
            self.set_cursor(False)
            self.focus_card(None, monotonic())
            if self.drag or self.layer_drag is not None:
                self.clear_drag()
                area.tag_redraw()
            if self.clear_hover():
                area.tag_redraw()
            return {"PASS_THROUGH"}
        if event.value == "PRESS":
            if event.type not in {"LEFTMOUSE", "ESC"}:
                self.focus_card(None, monotonic())
            if self.clear_hover():
                area.tag_redraw()
                if event.type == "ESC":
                    return {"RUNNING_MODAL"}
        if event.type == "ESC" and event.value == "PRESS" and (self.pressed or self.layer_press):
            self.clear_drag()
            area.tag_redraw()
            return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "RELEASE" and self.layer_press:
            if self.layer_drag is not None and inside:
                slot = self.layer_slot_at(x, y)
                if slot is not None:
                    self.layer_refs.insert(slot, self.layer_refs.pop(self.layer_drag))
                elif self.content_bottom <= y < region.height - 154:
                    self.layer_refs.pop(self.layer_drag)
                    self.config.status = tr("Layer removed")
            self.clear_drag()
            area.tag_redraw()
            return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "RELEASE" and self.pressed:
            key = self.pressed.key
            try:
                if self.drag and not inside:
                    self.apply_drop(context, key, event.mouse_x, event.mouse_y)
                elif self.drag and self.builder_open and (slot := self.layer_slot_at(x, y)) is not None:
                    self.add_to_builder(key, slot)
                elif not self.drag and inside:
                    self.click(context, ("card", key))
            except (OSError, ValueError, RuntimeError) as error:
                self.config.status = str(error)
            self.clear_drag()
            area.tag_redraw()
            return {"RUNNING_MODAL"}
        if not inside:
            return {"PASS_THROUGH"}
        if event.value == "PRESS":
            if event.type == "ESC":
                if self.typing:
                    self.typing = False
                elif self.collection_picker:
                    self.collection_picker = None
                elif self.card_menu:
                    self.card_menu = None
                elif self.menu:
                    self.menu = None
                elif self.detail:
                    self.detail = self.map = None
                else:
                    self.dismiss()
                    return {"CANCELLED"}
            elif event.type in {"WHEELUPMOUSE", "WHEELDOWNMOUSE"}:
                self.card_menu = None
                direction = -1 if event.type == "WHEELUPMOUSE" else 1
                if self.collection_picker:
                    self.collection_scroll = max(0, self.collection_scroll + direction * 3)
                elif event.ctrl and not self.detail and not self.menu:
                    self.zoom_anchor = (self.scroll // self.row_height) * self.gallery_columns
                    self.zoom = min(1.9, max(0.6, round(self.zoom * (1.12 if direction < 0 else 1 / 1.12), 3)))
                    self.marker["zoom"] = self.zoom
                elif self.menu:
                    self.menu_scroll = max(0, self.menu_scroll + direction * 3)
                elif self.detail:
                    if self.map == "@live" and self.action_at(self.mouse) == ("orbit", None):
                        self.live_zoom = max(0.55, min(2.7, self.live_zoom * (1.12 if direction < 0 else 1 / 1.12)))
                    else:
                        self.detail_scroll = max(0, min(self.detail_limit, self.detail_scroll + direction))
                else:
                    self.scroll = max(0, min(self.limit, self.scroll + direction * self.row_height))
            elif event.type == "RIGHTMOUSE" and not (self.pressed or self.layer_press):
                if self.collection_picker:
                    self.collection_picker = None
                elif self.card_menu:
                    self.card_menu = None
                elif not self.detail and not self.menu:
                    action = self.action_at(self.mouse)
                    if action and action[0] in {"card", "favorite", "folder"}:
                        self.typing = False
                        self.card_menu = (action[1], self.mouse)
            elif event.type == "LEFTMOUSE":
                action = self.action_at(self.mouse)
                if action == ("close", None):
                    self.dismiss()
                    return {"FINISHED"}
                try:
                    if action and action[0] == "card" and not (self.menu or self.card_menu or self.collection_picker):
                        cx, cy, width, height, _ = next(hit for hit in self.hits if hit[-1] == action)
                        self.pressed = Grab(action[1], *self.pointer, x - cx, y - cy, width, height)
                        self.typing = False
                    elif action and action[0] == "layer_slot" and not (self.card_menu or self.collection_picker):
                        if action[1] < len(self.layer_refs):
                            self.layer_press = (action[1], self.mouse)
                    elif action:
                        self.click(context, action)
                    else:
                        self.typing = False
                        self.menu = None
                        self.card_menu = None
                        self.collection_picker = None
                except (OSError, ValueError, RuntimeError, KeyError) as error:
                    self.config.status = str(error)
            elif self.typing:
                if event.type in {"RET", "NUMPAD_ENTER"}:
                    self.typing = False
                elif event.type == "BACK_SPACE":
                    self.search = self.search[:-1]
                elif event.type == "V" and event.ctrl:
                    self.search += context.window_manager.clipboard.replace("\n", " ")[:250]
                elif event.type == "A" and event.ctrl:
                    self.search = ""
                elif event.unicode and event.unicode.isprintable() and not event.ctrl:
                    self.search += event.unicode
                self.scroll = 0
        area.tag_redraw()
        return {"RUNNING_MODAL"}


class RTC_OT_material_canvas(bpy.types.Operator):
    bl_idname = "rtc.material_canvas"
    bl_label = "Material Core"
    owner_id: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, _event):
        area = context.area
        if area is None:
            return {"CANCELLED"}
        browser = BROWSERS.get(area.as_pointer())
        if browser is None:
            if area.type not in {"VIEW_3D", "IMAGE_EDITOR"}:
                return {"CANCELLED"}
            root = library_path(state.preferences(context))
            if root is None or not root.is_dir():
                self.report({"WARNING"}, tr("Choose the texture library folder in Material Core preferences."))
                return {"CANCELLED"}
            browser = Browser(context, int(self.owner_id) if self.owner_id.isdigit() else None)
        if browser.input is not None:
            return {"CANCELLED"}
        self.browser = browser
        browser.input = self
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if self.browser.input is not self:
            return {"CANCELLED"}
        result = self.browser.modal(context, event)
        if "FINISHED" in result or "CANCELLED" in result:
            self.cancel(context)
        return result

    def cancel(self, _context):
        if self.browser.input is self:
            self.browser.input = None
            self.browser.clear_drag()
            self.browser.clear_hover()
