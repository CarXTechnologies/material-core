"""GPU drawing and bounded texture caching for the catalogue."""
from collections import OrderedDict
import math

import blf
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader
from PIL import Image

from .appearance import BUTTON, LINE, TEXT
from .paths import readable_path


class Painter:
    def __init__(self):
        self.images = OrderedDict()
        self.batches = OrderedDict()
        self.image_bytes = 0
        self.uploads = 0
        self.pending = False
        self.solid = None
        self.texture = None

    def begin_frame(self):
        self.uploads = 0
        self.pending = False

    def quad(self, shader, x, y, width, height, textured=False):
        key = textured, x, y, width, height
        return self.batch(key, shader, lambda: self.quad_vertices(x, y, width, height, textured))

    @staticmethod
    def quad_vertices(x, y, width, height, textured):
        attrs = {"pos": ((x, y), (x + width, y), (x + width, y + height), (x, y + height))}
        if textured:
            attrs["texCoord"] = ((0, 0), (1, 0), (1, 1), (0, 1))
        return attrs

    def batch(self, key, shader, vertices):
        batch = self.batches.get(key)
        if batch is None:
            batch = batch_for_shader(shader, "TRI_FAN", vertices())
            self.batches[key] = batch
            if len(self.batches) > 1024:
                self.batches.popitem(last=False)
        self.batches.move_to_end(key)
        return batch

    def fill(self, color):
        if self.solid is None:
            self.solid = gpu.shader.from_builtin("UNIFORM_COLOR")
        self.solid.bind()
        self.solid.uniform_float("color", color)

    def rect(self, x, y, width, height, color):
        if width <= 0 or height <= 0:
            return
        self.fill(color)
        self.quad(self.solid, x, y, width, height).draw(self.solid)

    def round_rect(self, x, y, width, height, radius, color):
        if width <= 0 or height <= 0:
            return
        radius = min(radius, width / 2, height / 2)
        key = "round", x, y, width, height, radius

        def vertices():
            corners = ((x + width - radius, y + radius), (x + width - radius, y + height - radius),
                       (x + radius, y + height - radius), (x + radius, y + radius))
            return {"pos": [(cx + radius * math.cos(angle), cy + radius * math.sin(angle))
                            for index, (cx, cy) in enumerate(corners)
                            for angle in (math.pi * ((index - 1) / 2 + step / 10) for step in range(6))]}

        self.fill(color)
        self.batch(key, self.solid, vertices).draw(self.solid)

    def surface(self, x, y, width, height, radius, fill, border=LINE):
        self.round_rect(x, y, width, height, radius, border)
        self.round_rect(x + 1, y + 1, width - 2, height - 2, max(0, radius - 1), fill)

    def stroke(self, x1, y1, x2, y2, color, weight=1.65):
        dx, dy = x2 - x1, y2 - y1
        half = weight / (2 * math.hypot(dx, dy))
        nx, ny = -dy * half, dx * half
        key = "stroke", x1, y1, x2, y2, weight
        self.fill(color)
        self.batch(key, self.solid, lambda: {"pos": (
            (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x2 - nx, y2 - ny), (x2 + nx, y2 + ny)
        )}).draw(self.solid)

    def icon(self, icon, x, y, color, background=BUTTON):
        if icon == "search":
            self.surface(x - 7, y - 4, 11, 11, 5.5, background, color)
            self.stroke(x + 2, y - 3, x + 7, y - 8, color)
        elif icon == "chevron":
            self.stroke(x - 4, y + 2, x, y - 2, color)
            self.stroke(x, y - 2, x + 4, y + 2, color)
        elif icon in {"previous", "next"}:
            tip = x + (-3 if icon == "previous" else 3)
            tail = 2 * x - tip
            self.stroke(tail, y - 5, tip, y, color)
            self.stroke(tip, y, tail, y + 5, color)
        elif icon == "close":
            self.stroke(x - 4, y - 4, x + 4, y + 4, color)
            self.stroke(x - 4, y + 4, x + 4, y - 4, color)
        elif icon == "folder":
            self.round_rect(x - 8, y, 8, 6, 2, color)
            self.round_rect(x - 8, y - 5, 16, 9, 2, color)
        elif icon in {"heart", "heart_filled"}:
            label = "♥" if icon == "heart_filled" else "♡"
            blf.size(0, 16)
            self.text(x - blf.dimensions(0, label)[0] / 2, y - 5, label, color, size=16)
        elif icon == "back":
            self.stroke(x + 5, y, x - 5, y, color)
            self.stroke(x - 5, y, x - 1, y + 4, color)
            self.stroke(x - 5, y, x - 1, y - 4, color)

    def text(self, x, y, label, color=TEXT, size=13, width=None):
        label = str(label)
        blf.size(0, size)
        if width is not None and blf.dimensions(0, label)[0] > width:
            left, right = 0, len(label)
            while left < right:
                mid = (left + right + 1) // 2
                if blf.dimensions(0, label[:mid] + "…")[0] <= width:
                    left = mid
                else:
                    right = mid - 1
            label = label[:left] + "…"
        blf.color(0, *color)
        blf.position(0, x, y, 0)
        blf.draw(0, label)

    def image_texture(self, path):
        if not path:
            return
        if path not in self.images:
            if self.uploads >= 6:
                self.pending = True
                return
            self.uploads += 1
            try:
                with Image.open(readable_path(path)) as image:
                    rgba = image.convert("RGBA").transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                    size = rgba.size
                    pixels = np.asarray(rgba, dtype=np.float32).ravel()
                    pixels *= 1 / 255
                buffer = gpu.types.Buffer("FLOAT", pixels.size, pixels)
                texture = gpu.types.GPUTexture(size, format="RGBA8", data=buffer)
                self.images[path] = (texture, size)
                self.image_bytes += size[0] * size[1] * 4
            except (OSError, ValueError, RuntimeError):
                self.images[path] = None
            while len(self.images) > 768 or self.image_bytes > 96 * 1024 * 1024:
                _, old = self.images.popitem(last=False)
                if old is not None:
                    self.image_bytes -= old[1][0] * old[1][1] * 4
        if path not in self.images:
            return
        self.images.move_to_end(path)
        return self.images[path]

    def image(self, path, x, y, width, height):
        cached = self.image_texture(path)
        if cached is None:
            return
        texture, (source_width, source_height) = cached
        scale = min(width / source_width, height / source_height)
        iw, ih = round(source_width * scale), round(source_height * scale)
        x += (width - iw) / 2
        y += (height - ih) / 2
        if self.texture is None:
            self.texture = gpu.shader.from_builtin("IMAGE")
        self.texture.bind()
        self.texture.uniform_sampler("image", texture)
        gpu.state.blend_set("ALPHA")
        self.quad(self.texture, x, y, iw, ih, textured=True).draw(self.texture)
        gpu.state.blend_set("NONE")

    def close(self):
        self.images.clear()
        self.batches.clear()
        self.solid = self.texture = None


