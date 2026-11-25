"""Small studio-light renderer for cached catalog thumbnails."""
from functools import lru_cache

import numpy as np
from PIL import Image

SIZE = 320
TEXTURE_EDGE = 512

@lru_cache(maxsize=2)
def sphere_geometry(size):
    coord = (np.arange(size, dtype=np.float32) + 0.5 - size / 2) / (size * 0.451)
    x, y = np.meshgrid(coord, -coord)
    radius_squared = x*x + y*y
    z = np.sqrt(np.maximum(0.0, 1.0 - radius_squared))
    phi = np.arctan2(x, z)
    latitude = np.arcsin(np.clip(y, -1, 1))
    u = np.clip(0.5 + phi / np.pi, 0.0, 1.0)
    v = np.clip(0.5 - latitude / np.pi, 0.0, 1.0)
    surface = np.stack((x, y, z), axis=-1)
    tangent = np.stack((np.cos(phi), np.zeros_like(x), -np.sin(phi)), axis=-1)
    bitangent = np.stack((-np.sin(phi) * np.sin(latitude), np.cos(latitude),
                          -np.cos(phi) * np.sin(latitude)), axis=-1)
    edge = np.clip((1.0 - radius_squared) * (size / 2.5), 0.0, 1.0)[..., None]
    sectors = (x > 0).astype(np.int8) + 2 * (y < 0)
    backdrop = np.clip(25 - 13 * radius_squared + 7 * y, 12, 32)[..., None]
    return u, v, surface, tangent, bitangent, edge, sectors, backdrop

_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def sample(image, uv):
    pixels = np.asarray(image)
    if uv is None:
        return pixels[..., :3].astype(np.float32) / 255
    height, width = pixels.shape[:2]
    u, v = uv
    x, y = u * (width - 1), v * (height - 1)
    x0, y0 = x.astype(np.intp), y.astype(np.intp)
    x1, y1 = np.minimum(x0 + 1, width - 1), np.minimum(y0 + 1, height - 1)
    dx, dy = (x - x0)[..., None].astype(np.float32), (y - y0)[..., None].astype(np.float32)
    top = pixels[y0, x0, :3] * (1 - dx) + pixels[y0, x1, :3] * dx
    bottom = pixels[y1, x0, :3] * (1 - dx) + pixels[y1, x1, :3] * dx
    return (top * (1 - dy) + bottom * dy) / 255


def linearize(srgb):
    return np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)


def to_display(radiance):
    mapped = -np.expm1(-np.maximum(radiance, 0.0))
    srgb = np.where(mapped <= 0.0031308, mapped * 12.92, 1.055 * mapped ** (1 / 2.4) - 0.055)
    return np.clip(srgb * 255 + 0.5, 0, 255).astype(np.uint8)


def dot(a, b):
    return np.sum(a * b, axis=-1)


def unit(vector):
    return vector / np.sqrt(np.maximum(dot(vector, vector), 1e-8))[..., None]


def smooth_between(value, lower, upper):
    mix = np.clip((value - lower) / (upper - lower), 0.0, 1.0)
    return mix * mix * (3 - 2 * mix)


def studio_reflection(yaw, elevation, roughness):
    blur = 0.085 + 1.0 * roughness ** 2
    result = np.zeros_like(roughness)
    # Broad upper light and two strips for grazing-angle reflections.
    for center_x, center_y, half_x, half_y, power in [
        (0.0, 0.82, 0.92, 0.41, 15.5),
        (-2.10, 0.05, 0.21, 0.76, 10.0),
        (2.28, 0.12, 0.19, 0.65, 8.5),
    ]:
        x = np.abs((yaw - center_x + np.pi) % (2 * np.pi) - np.pi)
        y = np.abs(elevation - center_y)
        soft_x = 1 - smooth_between(x, np.maximum(0.0, half_x - 0.65 * blur), half_x + blur)
        soft_y = 1 - smooth_between(y, np.maximum(0.0, half_y - 0.65 * blur), half_y + blur)
        normalization = 1 / (1 + 4.7 * roughness ** 2)
        result += power * normalization * soft_x * soft_y
    return result


def shade(color, normal_map, roughness, metallic, geometry, tangent, bitangent):
    normal = geometry
    if normal_map is not None:
        xyz = normal_map * 2 - 1
        normal = unit(tangent * xyz[..., 0:1] + bitangent * xyz[..., 1:2] + geometry * xyz[..., 2:3])

    normal_view = np.clip(normal[..., 2], 0.03, 1.0)
    reflected = 2 * normal[..., 2:3] * normal - np.array([0, 0, 1], dtype=np.float32)
    yaw = np.arctan2(reflected[..., 0], reflected[..., 2])
    elevation = np.arcsin(np.clip(reflected[..., 1], -1.0, 1.0))
    studio = studio_reflection(yaw, elevation, roughness)
    gloss_response = np.minimum(1, ((1 - roughness) * 2) ** 1.5)
    studio *= np.clip(geometry[..., 2] * 4, 0.0, 1.0) * gloss_response * (1 - 0.35 * roughness)

    dielectric = 0.04
    f0 = dielectric * (1.0 - metallic[..., None]) + color * metallic[..., None]
    fresnel = f0 + (1 - f0) * (1 - normal_view[..., None]) ** 5
    # The fill keeps the color readable between reflections. The directional keys reveal the normal map.
    key = np.maximum(dot(normal, np.array([-0.42, 0.59, 0.69], dtype=np.float32)), 0.0)
    fill = np.maximum(dot(normal, np.array([0.77, 0.14, 0.62], dtype=np.float32)), 0.0)
    diffuse = color * ((0.25 + 0.72 * key + 0.29 * fill) * (1 - metallic))[..., None]
    return diffuse + fresnel * (studio[..., None] + 0.13)


def render_layer(maps, uv, geometry, tangent, bitangent):
    color = linearize(sample(maps["Diffuse"], uv))
    normal = sample(maps["Normal"], uv) if "Normal" in maps else None
    shape = color.shape[:-1]
    roughness = dot(sample(maps["Roughness"], uv), _LUMA) if "Roughness" in maps else np.full(shape, 0.66, dtype=np.float32)
    roughness = np.clip(roughness, 0.045, 1.0)
    metallic = dot(sample(maps["Metalic"], uv), _LUMA) if "Metalic" in maps else np.zeros(shape, dtype=np.float32)
    return to_display(shade(color, normal, roughness, metallic, geometry, tangent, bitangent))


def sphere(layers, size=SIZE):
    u, v, surface, tangent, bitangent, edge, sectors, backdrop = sphere_geometry(size)
    if len(layers) == 1:
        pixels = render_layer(layers[0], (u, v), surface, tangent, bitangent)
    else:
        pixels = np.empty((size, size, 3), dtype=np.uint8)
        sectors = sectors % len(layers)
        for index, layer in enumerate(layers):
            mask = sectors == index
            pixels[mask] = render_layer(layer, (u[mask], v[mask]), surface[mask],
                                        tangent[mask], bitangent[mask])
    return Image.fromarray((edge * pixels + (1 - edge) * backdrop).astype(np.uint8))


def decal(maps, edge=SIZE):
    diffuse = maps["Diffuse"]
    alpha = maps["Alpha"].convert("L") if "Alpha" in maps else diffuse.getchannel("A")
    width, height = diffuse.size
    factor = min((edge - round(28 * edge / SIZE)) / width, (edge - round(28 * edge / SIZE)) / height)
    size = max(1, round(width * factor)), max(1, round(height * factor))
    alpha = alpha.resize(size, Image.Resampling.BILINEAR)
    maps = {channel: image.resize(size, Image.Resampling.BILINEAR) for channel, image in maps.items() if channel != "Alpha"}
    pixels = render_layer(maps, None, np.array([-0.10, 0.09, 0.991], dtype=np.float32),
                          np.array([0.995, 0., 0.100], dtype=np.float32),
                          np.array([0.009, 0.996, -0.090], dtype=np.float32))
    check = (((np.indices((edge, edge)) // max(1, edge // 16)).sum(axis=0) % 2) * 13 + 40).astype(np.uint8)
    backdrop = Image.fromarray(check).convert("RGB")
    offset = ((edge - size[0]) // 2, (edge - size[1]) // 2)
    backdrop.paste(Image.fromarray(pixels), offset, alpha)
    return backdrop
