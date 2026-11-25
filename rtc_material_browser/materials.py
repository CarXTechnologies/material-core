"""Build materials compatible with the RenderCore exporter."""
import hashlib
import json
from pathlib import Path

import bpy

from .catalog import source_path
from .layers import LAYER_CHANNELS, LIMIT, layer_keys

STANDARD = {
    frozenset(("Diffuse",)): "Diffuse",
    frozenset(("Diffuse", "Roughness")): "Diffuse + Roughness",
    frozenset(("Diffuse", "Roughness", "Normal")): "Diffuse + Roughness + Normal",
    frozenset(("Diffuse", "Alpha")): "Diffuse + Alpha-blend",
    frozenset(("Diffuse", "Roughness", "Alpha")): "Diffuse + Roughness + Alpha-blend",
    frozenset(("Diffuse", "Roughness", "Normal", "Alpha")): "Diffuse + Roughness + Normal + Alpha-blend",
    frozenset(("Diffuse", "Roughness", "Normal", "Metalic")): "Diffuse + Roughness + Normal + Metalness",
}
LOCAL_MAPS = {"Diffuse", "Normal", "Roughness", "Alpha", "aoMap"}
BUNDLED_SHADER = Path(__file__).with_name("assets") / "RenderCore_Shader.blend"


def default_shader():
    if BUNDLED_SHADER.is_file():
        return str(BUNDLED_SHADER)
    paths = bpy.utils.script_paths(subdir="addons")
    for folder in paths:
        candidate = Path(folder) / "CarXBridge" / "RenderCore_Shader.blend"
        if candidate.is_file():
            return str(candidate)
    return ""


def plan(record, metadata):
    kind = record["kind"]
    groups = record["groups"]
    layers = layer_keys(record, metadata) if kind == "4L-mat" else list(groups)
    if not layers:
        raise ValueError("No usable texture layers.")
    if kind == "4L-mat":
        # RenderCore always evaluates the four vertex-color blend slots.
        layers = [*layers, *([layers[0]] * (LIMIT - len(layers)))]
    images = {}
    for number, key in enumerate(layers, 1):
        for channel, candidates in groups[key]["maps"].items():
            if channel == "Preview":
                continue
            if kind == "4L-mat" and channel not in LAYER_CHANNELS:
                continue
            destination = f"{channel} ({number:02})" if channel in LOCAL_MAPS else channel
            images[destination] = (candidates[0], "sRGB" if channel in {"Diffuse", "Emission"} else "Non-Color")
    if kind == "Standart":
        channels = frozenset(groups[layers[0]]["maps"]) - {"Preview", "aoMap"}
        mode = STANDARD.get(channels)
        if mode is None:
            compatible = channels & {"Diffuse", "Roughness", "Normal", "Alpha"}
            if "Normal" in compatible:
                compatible |= {"Roughness"}
            mode = STANDARD.get(compatible, "Diffuse")
    elif kind == "Decal":
        if "Alpha (01)" not in images:
            images["Alpha (01)"] = images["Diffuse (01)"]
        mode = "Decal_Alpha-blend"
    else:
        mode = "4 Layers"
    return mode, images


def rendercore_group(shader_file):
    bundled = shader_file == BUNDLED_SHADER.resolve()
    for group in bpy.data.node_groups:
        if bundled and group.get("rtc_bundled_rendercore"):
            return group
        if group.name == "RenderCore" and group.library:
            if Path(bpy.path.abspath(group.library.filepath)).resolve() == shader_file:
                return group
    with bpy.data.libraries.load(str(shader_file), link=not bundled) as (source, target):
        if "RenderCore" not in source.node_groups:
            raise ValueError("The shader library has no RenderCore node group.")
        target.node_groups = ["RenderCore"]
    if not target.node_groups or target.node_groups[0] is None:
        raise ValueError("Blender could not link the RenderCore node group.")
    group = target.node_groups[0]
    if bundled:
        group["rtc_bundled_rendercore"] = True
    return group


def matching_image(path, color_space):
    for existing in bpy.data.images:
        if existing.source == "FILE" and existing.filepath and existing.colorspace_settings.name == color_space:
            if Path(bpy.path.abspath(existing.filepath, library=existing.library)).resolve() == path:
                return existing
    return None


def build(root, shader_path, record, metadata):
    mode, inputs = plan(record, metadata)
    shader_file = Path(shader_path).resolve()
    if not shader_file.is_file():
        raise ValueError("Set the CarXBridge RenderCore_Shader.blend path in preferences.")
    files = {socket: (source_path(root, path), space) for socket, (path, space) in inputs.items()}
    for path, _ in files.values():
        if not path.is_file():
            raise ValueError(f"Texture missing: {path.name}. Refresh the catalog after SVN update.")
    name = metadata.get("name") or record["name"]
    identity = hashlib.sha256(json.dumps(
        [str(shader_file), name, mode, {sock: [str(path), space] for sock, (path, space) in files.items()}],
        sort_keys=True,
    ).encode()).hexdigest()
    material = bpy.data.materials.new(name)
    created = []
    try:
        tree = material.node_tree
        if tree is None:
            material.use_nodes = True
            tree = material.node_tree
        tree.nodes.clear()
        shader = tree.nodes.new("ShaderNodeGroup")
        shader.node_tree = rendercore_group(shader_file)
        shader.location = (0, 200)
        shader.inputs["ShaderType"].default_value = mode
        shader.inputs["CollisionType"].default_value = "Disable"
        if mode == "4 Layers":
            for slot in range(1, LIMIT + 1):
                socket = f"Normal ({slot:02})"
                if socket not in files:
                    shader.inputs[socket].default_value = (0.5, 0.5, 1.0)
        output = tree.nodes.new("ShaderNodeOutputMaterial")
        output.location = (330, 200)
        tree.links.new(shader.outputs["Return"], output.inputs["Surface"])
        for index, (socket, (path, color_space)) in enumerate(files.items()):
            target = shader.inputs.get(socket)
            if target is None or target.is_inactive or not target.enabled:
                if not socket.startswith("Diffuse"):
                    continue
                raise ValueError(f"RenderCore {mode} does not expose the {socket} input.")
            image = matching_image(path, color_space)
            if image is None:
                image = bpy.data.images.load(str(path), check_existing=False)
                created.append(image)
                image.colorspace_settings.name = color_space
                image["rtc_catalog_texture"] = True
            node = tree.nodes.new("ShaderNodeTexImage")
            node.image = image
            node.label = socket
            node.location = (-330 - (index // 4) * 250, 250 - (index % 4) * 265)
            channel = "Alpha" if mode.startswith("Decal") and socket == "Alpha (01)" and color_space == "sRGB" else "Color"
            tree.links.new(node.outputs[channel], target)
        material["rtc_catalog_id"] = record["id"]
        material["rtc_catalog_recipe"] = identity
        return material
    except Exception:
        bpy.data.materials.remove(material)
        for image in created:
            if image.users == 0:
                bpy.data.images.remove(image)
        raise


def can_assign_to(obj):
    return (obj is not None and obj.type == "MESH" and obj.mode == "OBJECT" and obj.is_editable
            and (bool(obj.material_slots) or obj.data.is_editable))


def assign(obj, material):
    if obj is None or obj.type != "MESH" or obj.mode != "OBJECT" or not obj.is_editable:
        raise ValueError("Choose an editable mesh in Object Mode.")
    if obj.material_slots:
        index = obj.active_material_index
        slot = obj.material_slots[index]
        if slot.link == "DATA" and (obj.data.users > 1 or not obj.data.is_editable):
            slot.link = "OBJECT"
        slot.material = material
    else:
        if not obj.data.is_editable:
            raise ValueError("Mesh data is read-only.")
        if obj.data.users > 1:
            obj.data = obj.data.copy()
        obj.data.materials.append(material)


def remove_unused_catalog_material(material):
    if material is None or material.users != 0 or material.library or not material.get("rtc_catalog_id"):
        return
    tree = material.node_tree
    images = {node.image for node in tree.nodes if node.type == "TEX_IMAGE" and node.image} if tree else set()
    bpy.data.materials.remove(material, do_unlink=False)
    for image in images:
        if image.users == 0 and not image.library and image.get("rtc_catalog_texture"):
            bpy.data.images.remove(image, do_unlink=False)
