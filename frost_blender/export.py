# The scene, as Frost reads it: the evaluated meshes with their materials
# written as a glTF, and everything Frost's importer leaves alone -- the
# camera, the lights, the world, the render settings and the material
# extensions -- in a setup file beside it.
#
# Blender is Z up; glTF and Frost are Y up. Every position and direction
# goes through `to_frost`: (x, y, z) becomes (x, z, -y). Blender's own glTF
# exporter does the same, which is why a file it writes and a file this
# writes agree.

import json
import math
import os
import shutil
import struct

import bpy
from mathutils import Vector

GLTF_FLOAT = 5126
GLTF_UINT = 5125
ARRAY_BUFFER = 34962
ELEMENT_ARRAY_BUFFER = 34963


def to_frost(v):
    return (float(v[0]), float(v[2]), -float(v[1]))


class GltfWriter:
    """Accumulates one glTF with one binary buffer."""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.binary = bytearray()
        self.accessors = []
        self.buffer_views = []
        self.meshes = []
        self.nodes = []
        self.materials = []
        self.material_index = {}
        self.images = []
        self.textures = []
        self.image_index = {}
        self.extensions_used = set()
        self.warnings = []

    def accessor(self, data, count, component_type, kind, target, bounds=None):
        while len(self.binary) % 4:
            self.binary += b"\0"
        view = {"buffer": 0, "byteOffset": len(self.binary), "byteLength": len(data), "target": target}
        self.binary += data
        self.buffer_views.append(view)
        accessor = {"bufferView": len(self.buffer_views) - 1, "componentType": component_type,
                    "count": count, "type": kind}
        if bounds is not None:
            accessor["min"], accessor["max"] = bounds
        self.accessors.append(accessor)
        return len(self.accessors) - 1

    def image(self, image):
        """Writes an image beside the glTF and returns its texture index."""
        key = image.name
        if key in self.image_index:
            return self.image_index[key]
        folder = os.path.join(self.out_dir, "textures")
        os.makedirs(folder, exist_ok=True)
        source = bpy.path.abspath(image.filepath_from_user()) if image.filepath else ""
        stem = bpy.path.clean_name(os.path.splitext(image.name)[0]) or "image"
        if source and os.path.isfile(source) and not image.is_dirty:
            name = stem + os.path.splitext(source)[1].lower()
            shutil.copyfile(source, os.path.join(folder, name))
        else:
            # Packed, painted or generated: written out as a PNG.
            name = stem + ".png"
            copy = image.copy()
            try:
                copy.filepath_raw = os.path.join(folder, name)
                copy.file_format = 'PNG'
                copy.save()
            finally:
                bpy.data.images.remove(copy)
        self.images.append({"uri": "textures/" + name})
        self.textures.append({"source": len(self.images) - 1, "sampler": 0})
        self.image_index[key] = len(self.textures) - 1
        return self.image_index[key]

    def write(self, name):
        path = os.path.join(self.out_dir, name + ".gltf")
        with open(os.path.join(self.out_dir, name + ".bin"), "wb") as f:
            f.write(self.binary)
        document = {
            "asset": {"version": "2.0", "generator": "Frost for Blender"},
            "buffers": [{"uri": name + ".bin", "byteLength": len(self.binary)}],
            "bufferViews": self.buffer_views,
            "accessors": self.accessors,
            "meshes": self.meshes,
            "nodes": self.nodes,
            "scenes": [{"nodes": list(range(len(self.nodes)))}],
            "scene": 0,
            "materials": self.materials,
            "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}],
        }
        if self.images:
            document["images"] = self.images
            document["textures"] = self.textures
        if self.extensions_used:
            document["extensionsUsed"] = sorted(self.extensions_used)
        with open(path, "w") as f:
            json.dump(document, f)
        return path


# ---- materials -------------------------------------------------------------

def principled_of(material):
    if material is None or not material.use_nodes or material.node_tree is None:
        return None
    for node in material.node_tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            return node
    return None


def socket(node, *names):
    for name in names:
        if name in node.inputs:
            return node.inputs[name]
    return None


def value_of(sock, fallback):
    if sock is None:
        return fallback
    value = sock.default_value
    try:
        return tuple(value)
    except TypeError:
        return value


def trace_image(sock):
    """Follows a Principled input back to the image behind it, through the
    nodes Blender's own glTF importer puts in the way -- a Normal Map, the
    Separate Color that splits a metal-roughness map, a Mix or a Math set
    to multiply that carries the file's factor -- and returns (image,
    factor): the constant multiplied in along the way, a colour or a
    number, or None when there was none. (None, None) when no image."""
    if sock is None or not sock.is_linked:
        return None, None
    node = sock.links[0].from_node
    factor = None
    for _ in range(6):
        if node.type == 'TEX_IMAGE':
            return (node.image, factor) if node.image is not None else (None, None)
        if node.type in {'NORMAL_MAP', 'SEPARATE_COLOR', 'SEPRGB'}:
            next_socket = socket(node, "Color", "Image")
        elif node.type in {'MIX', 'MIX_RGB'} and getattr(node, "blend_type", 'MULTIPLY') == 'MULTIPLY':
            candidates = [s for s in node.inputs if s.enabled and s.name in {"A", "B", "Color1", "Color2"}]
            linked = [s for s in candidates if s.is_linked]
            plain = [s for s in candidates if not s.is_linked]
            if not linked:
                return None, None
            if plain:
                factor = tuple(float(c) for c in plain[0].default_value)[:3]
            next_socket = linked[0]
        elif node.type == 'MATH' and node.operation == 'MULTIPLY':
            inputs = list(node.inputs)[:2]
            linked = [s for s in inputs if s.is_linked]
            plain = [s for s in inputs if not s.is_linked]
            if not linked:
                return None, None
            if plain:
                factor = float(plain[0].default_value)
            next_socket = linked[0]
        else:
            return None, None
        if next_socket is None or not next_socket.is_linked:
            return None, None
        node = next_socket.links[0].from_node
    return None, None


def image_behind(sock):
    return trace_image(sock)[0]


def material_json(writer, material, overrides):
    """One glTF material, and what Frost's importer does not read -- the
    emission's strength, the glass -- into `overrides` by name."""
    entry = {"name": material.name if material else "default", "doubleSided": True,
             "pbrMetallicRoughness": {"baseColorFactor": [0.8, 0.8, 0.8, 1.0], "metallicFactor": 0.0,
                                      "roughnessFactor": 0.5}}
    if material is None:
        return entry
    entry["doubleSided"] = not material.use_backface_culling
    node = principled_of(material)
    pbr = entry["pbrMetallicRoughness"]
    if node is None:
        colour = tuple(material.diffuse_color)
        pbr["baseColorFactor"] = [colour[0], colour[1], colour[2], 1.0]
        pbr["metallicFactor"] = float(material.metallic)
        pbr["roughnessFactor"] = float(material.roughness)
        return entry

    base = socket(node, "Base Color")
    image, factor = trace_image(base)
    if image is not None:
        pbr["baseColorTexture"] = {"index": writer.image(image)}
        tint = factor if isinstance(factor, tuple) else (1.0, 1.0, 1.0)
        pbr["baseColorFactor"] = [float(tint[0]), float(tint[1]), float(tint[2]), 1.0]
    else:
        colour = value_of(base, (0.8, 0.8, 0.8, 1.0))
        pbr["baseColorFactor"] = [float(colour[0]), float(colour[1]), float(colour[2]), 1.0]
    metal_image, metal_factor = trace_image(socket(node, "Metallic"))
    rough_image, rough_factor = trace_image(socket(node, "Roughness"))
    if rough_image is not None and (metal_image is None or metal_image == rough_image):
        # glTF's packing, roughness in green and metalness in blue, which is
        # what the importer split and what the exporter joins again.
        pbr["metallicRoughnessTexture"] = {"index": writer.image(rough_image)}
        pbr["roughnessFactor"] = float(rough_factor) if isinstance(rough_factor, float) else 1.0
        if metal_image is not None:
            pbr["metallicFactor"] = float(metal_factor) if isinstance(metal_factor, float) else 1.0
        else:
            pbr["metallicFactor"] = float(value_of(socket(node, "Metallic"), 0.0))
    else:
        pbr["metallicFactor"] = float(value_of(socket(node, "Metallic"), 0.0))
        pbr["roughnessFactor"] = float(value_of(socket(node, "Roughness"), 0.5))

    normal_image = image_behind(socket(node, "Normal"))
    if normal_image is not None:
        entry["normalTexture"] = {"index": writer.image(normal_image)}

    emission = value_of(socket(node, "Emission Color", "Emission"), (0.0, 0.0, 0.0, 1.0))
    strength = float(value_of(socket(node, "Emission Strength"), 0.0))
    emission_image = image_behind(socket(node, "Emission Color", "Emission"))
    if emission_image is not None:
        entry["emissiveTexture"] = {"index": writer.image(emission_image)}
        emission = (1.0, 1.0, 1.0, 1.0)
        strength = max(strength, 1.0)
    if strength > 0.0 and max(emission[:3]) > 0.0:
        entry["emissiveFactor"] = [min(float(c), 1.0) for c in emission[:3]]
        if strength != 1.0:
            entry.setdefault("extensions", {})["KHR_materials_emissive_strength"] = {"emissiveStrength": strength}
            writer.extensions_used.add("KHR_materials_emissive_strength")
        overrides.setdefault(material.name, {}).update(
            {"emission": [float(c) for c in emission[:3]], "emission_strength": strength})

    transmission = float(value_of(socket(node, "Transmission Weight", "Transmission"), 0.0))
    if transmission > 0.0:
        ior = float(value_of(socket(node, "IOR"), 1.45))
        entry.setdefault("extensions", {})["KHR_materials_transmission"] = {"transmissionFactor": transmission}
        entry["extensions"]["KHR_materials_ior"] = {"ior": ior}
        writer.extensions_used.update({"KHR_materials_transmission", "KHR_materials_ior"})
        overrides.setdefault(material.name, {}).update({"transmission": transmission, "ior": ior})
    return entry


def material_slot(writer, material, overrides):
    key = material.name if material else ""
    if key not in writer.material_index:
        writer.materials.append(material_json(writer, material, overrides))
        writer.material_index[key] = len(writer.materials) - 1
    return writer.material_index[key]


# ---- meshes ----------------------------------------------------------------

def add_mesh_instance(writer, obj, matrix, overrides, name):
    """The evaluated mesh of one instance, in world space, one primitive per
    material. Every triangle corner is its own vertex, which keeps the
    corner normals and needs no welding."""
    try:
        mesh = obj.to_mesh()
    except RuntimeError:
        return
    if mesh is None or len(mesh.polygons) == 0:
        obj.to_mesh_clear()
        return
    try:
        mesh.calc_loop_triangles()
        corner_normals = mesh.corner_normals
        uv_layer = mesh.uv_layers.active
        uvs = uv_layer.data if uv_layer is not None else None
        normal_matrix = matrix.to_3x3().inverted_safe().transposed()
        vertices = mesh.vertices

        by_material = {}
        for triangle in mesh.loop_triangles:
            by_material.setdefault(triangle.material_index, []).append(triangle)

        primitives = []
        for material_index, triangles in by_material.items():
            positions, normals, texcoords = [], [], []
            low = [1e30, 1e30, 1e30]
            high = [-1e30, -1e30, -1e30]
            for triangle in triangles:
                for loop_index, vertex_index in zip(triangle.loops, triangle.vertices):
                    p = to_frost(matrix @ vertices[vertex_index].co)
                    n = to_frost((normal_matrix @ corner_normals[loop_index].vector).normalized())
                    positions.extend(p)
                    normals.extend(n)
                    for axis in range(3):
                        low[axis] = min(low[axis], p[axis])
                        high[axis] = max(high[axis], p[axis])
                    if uvs is not None:
                        uv = uvs[loop_index].uv
                        texcoords.extend((float(uv.x), 1.0 - float(uv.y)))
                    else:
                        texcoords.extend((0.0, 0.0))
            count = len(positions) // 3
            if count == 0:
                continue
            primitive = {
                "attributes": {
                    "POSITION": writer.accessor(struct.pack("<%df" % len(positions), *positions), count,
                                                GLTF_FLOAT, "VEC3", ARRAY_BUFFER, (low, high)),
                    "NORMAL": writer.accessor(struct.pack("<%df" % len(normals), *normals), count,
                                              GLTF_FLOAT, "VEC3", ARRAY_BUFFER),
                    "TEXCOORD_0": writer.accessor(struct.pack("<%df" % len(texcoords), *texcoords), count,
                                                  GLTF_FLOAT, "VEC2", ARRAY_BUFFER),
                },
                "indices": writer.accessor(struct.pack("<%dI" % count, *range(count)), count,
                                           GLTF_UINT, "SCALAR", ELEMENT_ARRAY_BUFFER),
                "mode": 4,
            }
            slots = obj.material_slots
            material = slots[material_index].material if material_index < len(slots) else None
            primitive["material"] = material_slot(writer, material, overrides)
            primitives.append(primitive)
        if primitives:
            writer.meshes.append({"name": name, "primitives": primitives})
            writer.nodes.append({"name": name, "mesh": len(writer.meshes) - 1})
    finally:
        obj.to_mesh_clear()


def add_area_light(writer, light, matrix, overrides, name):
    """An area light as the emissive quad it is: Frost samples emissive
    geometry directly, and a Lambertian emitter of power P over area A has
    the radiance P / (pi A)."""
    size_x = float(light.size)
    size_y = float(light.size_y) if light.shape in {'RECTANGLE', 'ELLIPSE'} else size_x
    area = size_x * size_y
    if light.shape in {'DISK', 'ELLIPSE'}:
        area *= math.pi / 4.0
    if area <= 1e-9:
        return
    radiance = float(light.energy) / (math.pi * area)
    colour = [float(c) for c in light.color]
    material_name = "__frost_area_" + name
    writer.materials.append({
        "name": material_name, "doubleSided": True,
        "pbrMetallicRoughness": {"baseColorFactor": [0.0, 0.0, 0.0, 1.0], "metallicFactor": 0.0, "roughnessFactor": 1.0},
        "emissiveFactor": [min(c, 1.0) for c in colour],
        "extensions": {"KHR_materials_emissive_strength": {"emissiveStrength": radiance}},
    })
    writer.extensions_used.add("KHR_materials_emissive_strength")
    writer.material_index[material_name] = len(writer.materials) - 1
    overrides[material_name] = {"emission": colour, "emission_strength": radiance}
    hx, hy = size_x * 0.5, size_y * 0.5
    corners = [Vector((-hx, -hy, 0.0)), Vector((hx, -hy, 0.0)), Vector((hx, hy, 0.0)), Vector((-hx, hy, 0.0))]
    normal_matrix = matrix.to_3x3().inverted_safe().transposed()
    n = to_frost((normal_matrix @ Vector((0.0, 0.0, -1.0))).normalized())
    positions, normals = [], []
    low = [1e30] * 3
    high = [-1e30] * 3
    for corner in corners:
        p = to_frost(matrix @ corner)
        positions.extend(p)
        normals.extend(n)
        for axis in range(3):
            low[axis] = min(low[axis], p[axis])
            high[axis] = max(high[axis], p[axis])
    indices = [0, 2, 1, 0, 3, 2]
    primitive = {
        "attributes": {
            "POSITION": writer.accessor(struct.pack("<12f", *positions), 4, GLTF_FLOAT, "VEC3", ARRAY_BUFFER, (low, high)),
            "NORMAL": writer.accessor(struct.pack("<12f", *normals), 4, GLTF_FLOAT, "VEC3", ARRAY_BUFFER),
            "TEXCOORD_0": writer.accessor(struct.pack("<8f", 0, 0, 1, 0, 1, 1, 0, 1), 4, GLTF_FLOAT, "VEC2", ARRAY_BUFFER),
        },
        "indices": writer.accessor(struct.pack("<6I", *indices), 6, GLTF_UINT, "SCALAR", ELEMENT_ARRAY_BUFFER),
        "mode": 4,
        "material": writer.material_index[material_name],
    }
    writer.meshes.append({"name": name, "primitives": [primitive]})
    writer.nodes.append({"name": name, "mesh": len(writer.meshes) - 1})


# ---- the setup: camera, lights, world, render -------------------------------

def camera_json(depsgraph, scene, width, height, warnings):
    camera_object = scene.camera
    if camera_object is None:
        warnings.append("The scene has no camera; Frost framed it on everything.")
        return None
    evaluated = camera_object.evaluated_get(depsgraph)
    matrix = evaluated.matrix_world
    rotation = matrix.to_3x3()
    forward = (rotation @ Vector((0.0, 0.0, -1.0))).normalized()
    up = (rotation @ Vector((0.0, 1.0, 0.0))).normalized()
    camera = evaluated.data
    if camera.type != 'PERSP':
        warnings.append("Frost renders perspective cameras; this one is %s." % camera.type.lower())
    aspect = width / max(height, 1)
    if camera.sensor_fit == 'VERTICAL':
        sensor_height = camera.sensor_height
    elif camera.sensor_fit == 'HORIZONTAL':
        sensor_height = camera.sensor_width / aspect
    else:
        sensor_height = camera.sensor_width / aspect if width >= height else camera.sensor_width
    focus = camera.dof.focus_distance
    if camera.dof.focus_object is not None:
        focus = (camera.dof.focus_object.evaluated_get(depsgraph).matrix_world.translation - matrix.translation).length
    return {
        "position": to_frost(matrix.translation),
        "forward": to_frost(forward),
        "up": to_frost(up),
        "lens_mm": float(camera.lens),
        "sensor_width": float(sensor_height * aspect),
        "sensor_height": float(sensor_height),
        "clip_start": float(camera.clip_start),
        "clip_end": float(camera.clip_end),
        "dof": {"enabled": bool(camera.dof.use_dof), "focus_distance": float(max(focus, 0.01)),
                "fstop": float(camera.dof.aperture_fstop)},
    }


def world_json(scene, warnings):
    world = scene.world
    colour, strength = (0.05, 0.05, 0.05), 1.0
    if world is not None:
        if world.use_nodes and world.node_tree is not None:
            for node in world.node_tree.nodes:
                if node.type == 'BACKGROUND':
                    colour_socket = socket(node, "Color")
                    if colour_socket is not None and colour_socket.is_linked:
                        warnings.append("The world's colour comes from a node Frost does not read; a plain grey stands in.")
                    else:
                        colour = tuple(value_of(colour_socket, (0.05, 0.05, 0.05, 1.0))[:3])
                    strength = float(value_of(socket(node, "Strength"), 1.0))
                    break
        else:
            colour = tuple(world.color)
    return {"color": [float(c) for c in colour], "strength": strength}


def lights_json(depsgraph, writer, overrides, warnings):
    sun = None
    lights = []
    for instance in depsgraph.object_instances:
        obj = instance.object
        if obj.type != 'LIGHT':
            continue
        light = obj.data
        matrix = instance.matrix_world
        rotation = matrix.to_3x3()
        direction = to_frost((rotation @ Vector((0.0, 0.0, -1.0))).normalized())
        colour = [float(c) for c in light.color]
        if light.type == 'SUN':
            if sun is not None:
                warnings.append("Frost has one sun; the first sun lamp is it.")
                continue
            sun = {"direction": direction, "color": colour, "strength": float(light.energy),
                   "angle": float(light.angle)}
        elif light.type in {'POINT', 'SPOT'}:
            entry = {"type": light.type.lower(), "name": obj.name, "position": to_frost(matrix.translation),
                     "direction": direction, "color": colour,
                     "intensity": float(light.energy) / (4.0 * math.pi),   # watts to watts per steradian
                     "radius": float(light.shadow_soft_size)}
            if light.type == 'SPOT':
                entry["spot_angle"] = float(light.spot_size)
                entry["spot_blend"] = float(light.spot_blend)
            lights.append(entry)
        elif light.type == 'AREA':
            add_area_light(writer, light, matrix, overrides, obj.name)
    return sun, lights


def export_scene(depsgraph, scene, out_dir, width, height, settings):
    """Writes scene.gltf, scene.bin and setup.json into out_dir. Returns
    (gltf path, setup path, warnings)."""
    os.makedirs(out_dir, exist_ok=True)
    writer = GltfWriter(out_dir)
    overrides = {}
    warnings = []
    for instance in depsgraph.object_instances:
        obj = instance.object
        if obj.type not in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}:
            continue
        if not instance.show_self and not instance.is_instance:
            continue
        name = obj.name if not instance.is_instance else "%s.%d" % (obj.name, instance.random_id)
        add_mesh_instance(writer, obj, instance.matrix_world.copy(), overrides, name)
    sun, lights = lights_json(depsgraph, writer, overrides, warnings)
    gltf = writer.write("scene")
    warnings.extend(writer.warnings)

    setup = {
        "camera": camera_json(depsgraph, scene, width, height, warnings),
        "world": world_json(scene, warnings),
        "lights": lights,
        "materials": overrides,
        "render": {
            "width": width, "height": height,
            "samples": settings.samples, "bounces": settings.bounces,
            "denoise": settings.denoise,
            "noise": settings.noise_threshold if settings.adaptive else 0.0,
            "filter_glossy": settings.filter_glossy,
            "exposure": settings.exposure,
        },
    }
    if sun is not None:
        setup["sun"] = sun
    if setup["camera"] is None:
        del setup["camera"]
    setup_path = os.path.join(out_dir, "setup.json")
    with open(setup_path, "w") as f:
        json.dump(setup, f, indent=1)
    return gltf, setup_path, warnings
