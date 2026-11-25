"""A Windows drag card composed independently of Blender's 3D viewport."""
import ctypes
from ctypes import wintypes as win
from functools import lru_cache
import os
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps


class BitmapHeader(ctypes.Structure):
    _fields_ = [("size", win.DWORD), ("width", win.LONG), ("height", win.LONG),
                ("planes", win.WORD), ("bits", win.WORD), ("compression", win.DWORD),
                ("image_size", win.DWORD), ("x_density", win.LONG), ("y_density", win.LONG),
                ("colors", win.DWORD), ("important_colors", win.DWORD)]


class Blend(ctypes.Structure):
    _fields_ = [("operation", win.BYTE), ("flags", win.BYTE),
                ("opacity", win.BYTE), ("format", win.BYTE)]


def function(library, name, result, *args):
    call = getattr(library, name)
    call.restype = result
    call.argtypes = args
    return call


class Windows:
    def __init__(self):
        user = ctypes.WinDLL("user32", use_last_error=True)
        gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        pointer = ctypes.POINTER
        void = ctypes.c_void_p
        self.foreground = function(user, "GetForegroundWindow", win.HWND)
        self.owner_pid = function(user, "GetWindowThreadProcessId", win.DWORD, win.HWND, pointer(win.DWORD))
        self.client = function(user, "GetClientRect", win.BOOL, win.HWND, pointer(win.RECT))
        self.client_to_screen = function(user, "ClientToScreen", win.BOOL, win.HWND, pointer(win.POINT))
        self.create = function(user, "CreateWindowExW", win.HWND, win.DWORD, win.LPCWSTR, win.LPCWSTR,
                               win.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               win.HWND, win.HMENU, win.HINSTANCE, void)
        self.destroy = function(user, "DestroyWindow", win.BOOL, win.HWND)
        self.get_dc = function(user, "GetDC", win.HDC, win.HWND)
        self.release_dc = function(user, "ReleaseDC", ctypes.c_int, win.HWND, win.HDC)
        self.move = function(user, "SetWindowPos", win.BOOL, win.HWND, win.HWND,
                             ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, win.UINT)
        self.show = function(user, "ShowWindow", win.BOOL, win.HWND, ctypes.c_int)
        self.update = function(user, "UpdateLayeredWindow", win.BOOL, win.HWND, win.HDC,
                               pointer(win.POINT), pointer(win.SIZE), win.HDC, pointer(win.POINT),
                               win.DWORD, pointer(Blend), win.DWORD)
        self.make_dc = function(gdi, "CreateCompatibleDC", win.HDC, win.HDC)
        self.delete_dc = function(gdi, "DeleteDC", win.BOOL, win.HDC)
        self.select = function(gdi, "SelectObject", win.HGDIOBJ, win.HDC, win.HGDIOBJ)
        self.delete = function(gdi, "DeleteObject", win.BOOL, win.HGDIOBJ)
        self.bitmap = function(gdi, "CreateDIBSection", win.HBITMAP, win.HDC, pointer(BitmapHeader),
                               win.UINT, pointer(void), win.HANDLE, win.DWORD)


@lru_cache(maxsize=1)
def get_api():
    return Windows()


def frame(width, height, title, preview, accent):
    image = Image.new("RGBA", (width + 8, height + 10))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 5, width + 5, height + 8), 10, fill=(0, 0, 0, 115))
    outline = tuple(round(channel * 255) for channel in accent)
    draw.rounded_rectangle((0, 0, width, height), 9, fill=(29, 39, 42, 255), outline=outline, width=1)
    if preview:
        try:
            with Image.open(preview) as source:
                thumb = ImageOps.contain(source.convert("RGBA"), (width - 12, height - 32))
            image.alpha_composite(thumb, ((width - thumb.width) // 2, 6))
        except (OSError, ValueError):
            pass
    try:
        path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts" / "segoeui.ttf"
        font = ImageFont.truetype(str(path), max(10, min(14, width // 9)))
    except OSError:
        font = ImageFont.load_default()
    label = title
    while label and draw.textlength(label, font=font) > width - 15:
        label = label[:-2].rstrip("…") + "…" if len(label) > 2 else ""
    draw.text((7, height - 23), label, font=font, fill=(234, 245, 241, 255))
    return image


class DragOverlay:
    @classmethod
    def create(cls, window, bounds, title, preview, accent):
        api = get_api()
        owner = api.foreground()
        pid = win.DWORD()
        if not owner or not api.owner_pid(owner, ctypes.byref(pid)) or pid.value != os.getpid():
            return None
        overlay = cls(api, owner)
        try:
            overlay.initialize(window, bounds, title, preview, accent)
        except (OSError, ValueError, TypeError):
            overlay.close()
            return None
        return overlay

    def __init__(self, api, owner):
        self.api = api
        self.owner = owner
        self.handle = None
        self.visible = False
        self.image = None
        self.card_size = None

    def position(self, window, bounds):
        api = self.api
        rect, point = win.RECT(), win.POINT()
        if not api.client(self.owner, ctypes.byref(rect)) or not api.client_to_screen(self.owner, ctypes.byref(point)):
            raise OSError("Unable to locate Blender client window")
        scale_x = rect.right / max(1, window.width)
        scale_y = rect.bottom / max(1, window.height)
        x, y, width, height = bounds
        point.x += round(x * scale_x)
        point.y += round((window.height - y - height) * scale_y)
        return point, max(48, round(width * scale_x)), max(40, round(height * scale_y))

    def initialize(self, window, bounds, title, preview, accent):
        api = self.api
        point, width, height = self.position(window, bounds)
        image = self.image = frame(width, height, title, preview, accent)
        self.handle = api.create(0x080800A0, "STATIC", None, 0x80000000,
                                 point.x, point.y, image.width, image.height,
                                 self.owner, None, None, None)
        if not self.handle:
            raise OSError("Could not create the drag preview window")
        self.upload(point, image)
        self.card_size = width, height

    def upload(self, point, image):
        api = self.api
        dc = api.get_dc(None)
        memory = api.make_dc(dc)
        bitmap = old = None
        try:
            pixels = ctypes.c_void_p()
            header = BitmapHeader(ctypes.sizeof(BitmapHeader), image.width, -image.height, 1, 32)
            bitmap = api.bitmap(memory, ctypes.byref(header), 0, ctypes.byref(pixels), None, 0)
            if not bitmap or not pixels.value:
                raise OSError("Could not allocate the drag preview bitmap")
            old = api.select(memory, bitmap)
            red, green, blue, alpha = image.split()
            premultiplied = Image.merge("RGBA", (ImageChops.multiply(red, alpha), ImageChops.multiply(green, alpha),
                                                 ImageChops.multiply(blue, alpha), alpha))
            data = premultiplied.tobytes("raw", "BGRA")
            ctypes.memmove(pixels, data, len(data))
            size = win.SIZE(image.width, image.height)
            source = win.POINT()
            blend = Blend(0, 0, 255, 1)
            if not api.update(self.handle, dc, ctypes.byref(point), ctypes.byref(size), memory,
                              ctypes.byref(source), 0, ctypes.byref(blend), 2):
                raise OSError("Could not paint the drag preview window")
        finally:
            if old:
                api.select(memory, old)
            if bitmap:
                api.delete(bitmap)
            if memory:
                api.delete_dc(memory)
            if dc:
                api.release_dc(None, dc)

    def move(self, window, bounds, pointer, *, allow_outside=False):
        x, y = pointer
        inside = 0 <= x < window.width and 0 <= y < window.height
        if self.api.foreground() != self.owner or not (inside or allow_outside):
            if self.visible:
                self.api.show(self.handle, 0)
                self.visible = False
            return
        point, width, height = self.position(window, bounds)
        if self.card_size != (width, height):
            image = self.image.resize((width + 8, height + 10), Image.Resampling.BILINEAR)
            self.upload(point, image)
            self.card_size = width, height
        self.visible = bool(self.api.move(self.handle, None, point.x, point.y, 0, 0, 0x51))

    def close(self):
        if self.handle:
            self.api.destroy(self.handle)
            self.handle = None
            self.visible = False
        self.image = None
