# The scene, as Frost reads it: the evaluated meshes with their materials
# written as a glTF, and everything Frost's importer leaves alone -- the
# camera, the lights, the world, the render settings and the material
# extensions -- in a setup file beside it.
#
# Blender is Z up; glTF and Frost are Y up. Every position and direction
# goes through `to_frost`: (x, y, z) becomes (x, z, -y). Blender's own glTF
# exporter does the same, which is why a file it writes and a file this
# writes agree.

import hashlib
import json
import math
import os
import shutil
import struct

import bpy
import numpy as np
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
        self.mesh_index = {}
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

    def add_node(self, name, mesh_number, matrix):
        """A node showing a mesh, with Blender's world matrix turned into
        glTF's axes; glTF wants the sixteen numbers column by column."""
        world = np.array(matrix, dtype=np.float64)
        turned = AXIS_CHANGE @ world @ AXIS_CHANGE_INVERSE
        self.nodes.append({"name": name, "mesh": mesh_number, "matrix": turned.T.reshape(-1).tolist()})

    def image(self, image):
        """Writes an image beside the glTF and returns its texture index. The
        file is linked from a cache, not copied: a project's textures are
        gigabytes, and the viewport exports the scene again on every change."""
        key = image.name
        if key in self.image_index:
            return self.image_index[key]
        folder = os.path.join(self.out_dir, "textures")
        os.makedirs(folder, exist_ok=True)
        cached = cached_image(image)
        if cached is None:
            self.warnings.append("The image %s could not be written out; its material renders without it." % image.name)
            return None
        name = os.path.basename(cached)
        target = os.path.join(folder, name)
        if not os.path.exists(target):
            try:
                os.symlink(cached, target)
            except OSError:
                shutil.copyfile(cached, target)
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


def cache_folder():
    folder = os.path.join(os.path.expanduser("~"), "Library", "Caches", "Frost for Blender", "textures")
    os.makedirs(folder, exist_ok=True)
    return folder


def cached_image(image):
    """The image as a file in the cache, made once: a file-backed image is
    linked as it is, and a packed, painted or generated one is written as a
    PNG named by its content's stamp. Returns the path, or None."""
    source = bpy.path.abspath(image.filepath_from_user()) if image.filepath else ""
    stem = bpy.path.clean_name(os.path.splitext(image.name)[0]) or "image"
    if source and os.path.isfile(source) and not image.is_dirty and not image.packed_file:
        return source
    try:
        stamp = "%s-%dx%d-%s" % (image.name, image.size[0], image.size[1],
                                 image.packed_file.size if image.packed_file else "painted")
    except Exception:
        stamp = image.name
    digest = hashlib.sha1(stamp.encode("utf-8", "replace")).hexdigest()[:12]
    path = os.path.join(cache_folder(), "%s-%s.png" % (stem, digest))
    if os.path.isfile(path) and not image.is_dirty:
        return path
    copy = image.copy()
    try:
        copy.filepath_raw = path
        copy.file_format = 'PNG'
        copy.save()
    except Exception:
        path = None
    finally:
        bpy.data.images.remove(copy)
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
    index = writer.image(image) if image is not None else None
    if index is not None:
        pbr["baseColorTexture"] = {"index": index}
        tint = factor if isinstance(factor, tuple) else (1.0, 1.0, 1.0)
        pbr["baseColorFactor"] = [float(tint[0]), float(tint[1]), float(tint[2]), 1.0]
    else:
        colour = value_of(base, (0.8, 0.8, 0.8, 1.0))
        pbr["baseColorFactor"] = [float(colour[0]), float(colour[1]), float(colour[2]), 1.0]
    metal_image, metal_factor = trace_image(socket(node, "Metallic"))
    rough_image, rough_factor = trace_image(socket(node, "Roughness"))
    rough_index = writer.image(rough_image) if rough_image is not None else None
    if rough_index is not None and (metal_image is None or metal_image == rough_image):
        # glTF's packing, roughness in green and metalness in blue, which is
        # what the importer split and what the exporter joins again.
        pbr["metallicRoughnessTexture"] = {"index": rough_index}
        pbr["roughnessFactor"] = float(rough_factor) if isinstance(rough_factor, float) else 1.0
        if metal_image is not None:
            pbr["metallicFactor"] = float(metal_factor) if isinstance(metal_factor, float) else 1.0
        else:
            pbr["metallicFactor"] = float(value_of(socket(node, "Metallic"), 0.0))
    else:
        pbr["metallicFactor"] = float(value_of(socket(node, "Metallic"), 0.0))
        pbr["roughnessFactor"] = float(value_of(socket(node, "Roughness"), 0.5))

    normal_image = image_behind(socket(node, "Normal"))
    normal_index = writer.image(normal_image) if normal_image is not None else None
    if normal_index is not None:
        entry["normalTexture"] = {"index": normal_index}

    emission = value_of(socket(node, "Emission Color", "Emission"), (0.0, 0.0, 0.0, 1.0))
    strength = float(value_of(socket(node, "Emission Strength"), 0.0))
    emission_image = image_behind(socket(node, "Emission Color", "Emission"))
    emission_index = writer.image(emission_image) if emission_image is not None else None
    if emission_index is not None:
        entry["emissiveTexture"] = {"index": emission_index}
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

# Blender's z-up into glTF's y-up, as a change of basis: a node's matrix is
# C M C^-1, a position is C p.
AXIS_CHANGE = np.array([[1.0, 0.0, 0.0, 0.0],
                        [0.0, 0.0, 1.0, 0.0],
                        [0.0, -1.0, 0.0, 0.0],
                        [0.0, 0.0, 0.0, 1.0]])
AXIS_CHANGE_INVERSE = np.linalg.inv(AXIS_CHANGE)


def mesh_key(obj):
    """What decides whether two instances can share one exported mesh: the
    same mesh data and no modifiers of its own. An object with modifiers is
    its own evaluated mesh, and gets its own."""
    original = obj.original if hasattr(obj, "original") else obj
    if original.modifiers or obj.type != 'MESH':
        return None
    data = original.data
    return data.name_full if data is not None else None


def add_mesh_instance(writer, obj, matrix, overrides, name):
    """One node for this instance, carrying its matrix, and the mesh it
    shows: written once per mesh data (a thousand copies of a stone are one
    mesh and a thousand nodes, which is what Frost's importer keeps them
    as, and a thousandth of the file), in the mesh's own space, one
    primitive per material, corners welded. Everything comes out of
    Blender in bulk (`foreach_get`) and is turned with numpy: a million
    triangles take a third of a second, where a loop over every corner in
    Python took minutes with Blender frozen for the whole of it."""
    key = mesh_key(obj)
    if key is not None and key in writer.mesh_index:
        writer.add_node(name, writer.mesh_index[key], matrix)
        return
    mesh_number = add_mesh_data(writer, obj, overrides, name)
    if mesh_number is None:
        return
    if key is not None:
        writer.mesh_index[key] = mesh_number
    writer.add_node(name, mesh_number, matrix)


def add_mesh_data(writer, obj, overrides, name):
    """The evaluated mesh, in its own space. Returns the glTF mesh index."""
    try:
        mesh = obj.to_mesh()
    except RuntimeError:
        return None
    if mesh is None or len(mesh.polygons) == 0:
        obj.to_mesh_clear()
        return None
    try:
        mesh.calc_loop_triangles()
        triangle_count = len(mesh.loop_triangles)
        if triangle_count == 0:
            return None
        vertex_count = len(mesh.vertices)
        loop_count = len(mesh.loops)
        co = np.empty(vertex_count * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", co)
        triangle_vertices = np.empty(triangle_count * 3, dtype=np.int32)
        mesh.loop_triangles.foreach_get("vertices", triangle_vertices)
        triangle_loops = np.empty(triangle_count * 3, dtype=np.int32)
        mesh.loop_triangles.foreach_get("loops", triangle_loops)
        material_of = np.empty(triangle_count, dtype=np.int32)
        mesh.loop_triangles.foreach_get("material_index", material_of)
        normals = np.empty(loop_count * 3, dtype=np.float32)
        mesh.corner_normals.foreach_get("vector", normals)
        uv_layer = mesh.uv_layers.active
        if uv_layer is not None:
            uvs = np.empty(loop_count * 2, dtype=np.float32)
            try:
                uv_layer.uv.foreach_get("vector", uvs)
            except AttributeError:
                uv_layer.data.foreach_get("uv", uvs)
            uvs = uvs.reshape(loop_count, 2)
            uvs[:, 1] = 1.0 - uvs[:, 1]   # glTF's picture origin is the top left
        else:
            uvs = np.zeros((loop_count, 2), dtype=np.float32)

        # In the mesh's own space, into Frost's axes: (x, y, z) -> (x, z, -y).
        positions = co.reshape(vertex_count, 3).astype(np.float64)
        world_normals = normals.reshape(loop_count, 3).astype(np.float64)
        positions = positions[:, [0, 2, 1]] * np.array([1.0, 1.0, -1.0])
        world_normals = world_normals[:, [0, 2, 1]] * np.array([1.0, 1.0, -1.0])

        corner_vertices = triangle_vertices.reshape(triangle_count, 3)
        corner_loops = triangle_loops.reshape(triangle_count, 3)
        primitives = []
        for material_index in np.unique(material_of):
            chosen = material_of == material_index
            v = corner_vertices[chosen].reshape(-1)
            l = corner_loops[chosen].reshape(-1)
            if v.size == 0:
                continue
            # Corners welded: a smooth surface's corners share their vertex,
            # normal and uv with their neighbours, and a vertex for each of
            # them was three times the file and three times the import. The
            # key is the vertex with its normal and uv rounded to what a
            # half-float would keep.
            keys = np.empty((v.size, 6), dtype=np.int64)
            keys[:, 0] = v
            keys[:, 1:4] = np.rint(world_normals[l] * 4096.0)
            keys[:, 4:6] = np.rint(uvs[l] * 65536.0)
            unique_keys, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
            count = int(first.size)
            p = positions[v[first]].astype("<f4")
            n = world_normals[l[first]].astype("<f4")
            t = uvs[l[first]].astype("<f4")
            low = p.min(axis=0).tolist()
            high = p.max(axis=0).tolist()
            primitive = {
                "attributes": {
                    "POSITION": writer.accessor(p.tobytes(), count, GLTF_FLOAT, "VEC3", ARRAY_BUFFER, (low, high)),
                    "NORMAL": writer.accessor(n.tobytes(), count, GLTF_FLOAT, "VEC3", ARRAY_BUFFER),
                    "TEXCOORD_0": writer.accessor(t.tobytes(), count, GLTF_FLOAT, "VEC2", ARRAY_BUFFER),
                },
                "indices": writer.accessor(inverse.reshape(-1).astype("<u4").tobytes(), int(inverse.size),
                                           GLTF_UINT, "SCALAR", ELEMENT_ARRAY_BUFFER),
                "mode": 4,
            }
            slots = obj.material_slots
            material = slots[material_index].material if material_index < len(slots) else None
            primitive["material"] = material_slot(writer, material, overrides)
            primitives.append(primitive)
        if not primitives:
            return None
        writer.meshes.append({"name": name, "primitives": primitives})
        return len(writer.meshes) - 1
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
