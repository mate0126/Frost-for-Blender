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
from mathutils import Matrix, Vector

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

    def image_file(self, path):
        """A picture already on disk -- a bake -- linked beside the glTF."""
        if not path or not os.path.isfile(path):
            return None
        key = "file:" + path
        if key in self.image_index:
            return self.image_index[key]
        folder = os.path.join(self.out_dir, "textures")
        os.makedirs(folder, exist_ok=True)
        name = os.path.basename(path)
        target = os.path.join(folder, name)
        if not os.path.exists(target):
            try:
                os.symlink(path, target)
            except OSError:
                shutil.copyfile(path, target)
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


# The bake's UV map on a mesh, and the custom property on an object that
# names its baked pictures (bake.py makes both; the export reads them).
BAKE_UV = "FrostBake"
BAKE_KEY = "frost_bake"


def cache_folder():
    folder = os.path.join(os.path.expanduser("~"), "Library", "Caches", "Frost for Blender", "textures")
    os.makedirs(folder, exist_ok=True)
    return folder


def cached_image(image, hdr=False):
    """The image as a file in the cache, made once: a file-backed image is
    linked as it is, and a packed, painted or generated one is written as a
    PNG named by its content's stamp -- or as an EXR for a sky, whose sun is
    thousands of times its ground and does not fit in a PNG. Returns the
    path, or None."""
    global last_image_error
    source = bpy.path.abspath(image.filepath_from_user()) if image.filepath else ""
    stem = bpy.path.clean_name(os.path.splitext(image.name)[0]) or "image"
    # A sky is handed over as it is when it is a Radiance .hdr, which Frost
    # reads; any other source -- an OpenEXR of whatever compression, a packed
    # or painted picture -- is written out once by Blender as a plain OpenEXR,
    # which Frost reads too. Not as an .hdr: Blender's Radiance writer goes
    # through the view transform and a sun of six hundred came out as fifteen.
    keep = source and os.path.isfile(source) and not image.is_dirty and not image.packed_file
    if keep and (not hdr or source.lower().endswith(".hdr")):
        return source
    try:
        stamp = "%s-%dx%d-%s" % (image.name, image.size[0], image.size[1],
                                 image.packed_file.size if image.packed_file else
                                 ("%d" % os.path.getmtime(source) if keep else "painted"))
    except Exception:
        stamp = image.name
    digest = hashlib.sha1(stamp.encode("utf-8", "replace")).hexdigest()[:12]
    path = os.path.join(cache_folder(), "%s-%s.%s" % (stem, digest, "exr" if hdr else "png"))
    if os.path.isfile(path) and not image.is_dirty:
        return path
    if hdr:
        # Through a fresh float image, tagged linear, filled in bulk: a copy
        # of a file-backed picture would not save under another name.
        fresh = None
        try:
            w, h = int(image.size[0]), int(image.size[1])
            pixels = np.empty(w * h * 4, dtype=np.float32)
            image.pixels.foreach_get(pixels)
            fresh = bpy.data.images.new("frost-sky", w, h, float_buffer=True, alpha=True)
            try:
                fresh.colorspace_settings.name = 'Linear Rec.709'
            except TypeError:
                pass
            fresh.pixels.foreach_set(pixels)
            fresh.filepath_raw = path
            fresh.file_format = 'OPEN_EXR'
            fresh.save()
        except Exception as error:
            last_image_error = str(error)
            path = None
        finally:
            if fresh is not None:
                bpy.data.images.remove(fresh)
        return path
    copy = image.copy()
    try:
        copy.filepath_raw = path
        copy.file_format = 'PNG'
        copy.save()
    except Exception as error:
        last_image_error = str(error)
        path = None
    finally:
        bpy.data.images.remove(copy)
    return path


last_image_error = ""


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
            # A picture mapped by position rather than by the mesh's UVs is
            # one Frost cannot read as it is: only a bake hands it over.
            if node.image is None or not uv_mapped(node):
                return None, None
            return node.image, factor
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


def uv_mapped(node):
    """Whether an Image Texture reads by the mesh's UVs, which is the only
    mapping Frost has: an unlinked Vector, a UV Map node, or Texture
    Coordinate's UV output, through a Mapping node or not. Generated, Object
    and the rest project the picture by position, which only a bake hands
    over."""
    vector = socket(node, "Vector")
    for _ in range(3):
        if vector is None or not vector.is_linked:
            return True
        link = vector.links[0]
        source = link.from_node
        if source.type == 'UVMAP':
            return True
        if source.type == 'TEX_COORD':
            return link.from_socket.name == 'UV'
        if source.type == 'MAPPING':
            vector = socket(source, "Vector")
            continue
        return False
    return False


# ---- the surface, whatever shader makes it ----------------------------------

PLAIN_SURFACE = {"base": (0.8, 0.8, 0.8), "roughness": 0.5, "metallic": 0.0, "normal": None,
                 "emission": (0.0, 0.0, 0.0), "strength": 0.0, "transmission": 0.0, "ior": 1.45}


def is_socket(value):
    return hasattr(value, "is_linked")


def number_of(value, fallback):
    """A number from a socket or a plain value; a linked socket counts as
    its default, which is what the graph started from."""
    if is_socket(value):
        value = value.default_value
    try:
        return float(value)
    except TypeError:
        return fallback


def colour_of(value, fallback):
    if is_socket(value):
        value = value.default_value
    try:
        return tuple(float(c) for c in value)[:3]
    except TypeError:
        return fallback


def output_shader(material):
    """The node wired into the active Material Output's Surface, or None."""
    if material is None or not getattr(material, "use_nodes", True) or material.node_tree is None:
        return None
    output = None
    for node in material.node_tree.nodes:
        if node.type == 'OUTPUT_MATERIAL' and (output is None or node.is_active_output):
            output = node
    surface = socket(output, "Surface") if output is not None else None
    if surface is None or not surface.is_linked:
        return None
    return surface.links[0].from_node


def shader_surface(node, depth=0):
    """A shader node as what Frost's material is: sockets to follow (a
    picture may be behind them) or plain values. The Principled BSDF as it
    is; a Diffuse, Glossy or Metallic, Emission, Glass, Refraction,
    Translucent or Transparent shader as the Principled it is nearest to; a
    Mix or Add of shaders blended by the factor, with the sockets of the
    heavier side; a group by what its output is wired to inside. Anything
    else is grey -- which is what every material without a Principled was."""
    surface = dict(PLAIN_SURFACE)
    if node is None or depth > 5:
        return surface
    kind = node.type
    if kind == 'REROUTE':
        inp = node.inputs[0] if node.inputs else None
        return shader_surface(inp.links[0].from_node if inp is not None and inp.is_linked else None, depth + 1)
    if kind == 'BSDF_PRINCIPLED':
        surface.update({"base": socket(node, "Base Color"), "roughness": socket(node, "Roughness"),
                        "metallic": socket(node, "Metallic"), "normal": socket(node, "Normal"),
                        "emission": socket(node, "Emission Color", "Emission"),
                        "strength": socket(node, "Emission Strength"),
                        "transmission": socket(node, "Transmission Weight", "Transmission"),
                        "ior": socket(node, "IOR")})
    elif kind == 'BSDF_DIFFUSE':
        surface.update({"base": socket(node, "Color"), "roughness": 0.9, "normal": socket(node, "Normal")})
    elif kind in ('BSDF_GLOSSY', 'BSDF_METALLIC'):
        surface.update({"base": socket(node, "Color", "Base Color"), "metallic": 1.0,
                        "roughness": socket(node, "Roughness"), "normal": socket(node, "Normal")})
    elif kind == 'EMISSION':
        surface.update({"base": (0.0, 0.0, 0.0), "emission": socket(node, "Color"),
                        "strength": socket(node, "Strength")})
    elif kind in ('BSDF_GLASS', 'BSDF_REFRACTION'):
        surface.update({"base": socket(node, "Color"), "transmission": 1.0, "ior": socket(node, "IOR"),
                        "roughness": socket(node, "Roughness"), "normal": socket(node, "Normal")})
    elif kind == 'BSDF_TRANSPARENT':
        surface.update({"base": socket(node, "Color"), "transmission": 1.0, "ior": 1.0, "roughness": 0.0})
    elif kind in ('BSDF_TRANSLUCENT', 'SUBSURFACE_SCATTERING', 'BSDF_VELVET', 'BSDF_SHEEN', 'BSDF_TOON',
                  'BSDF_HAIR', 'BSDF_HAIR_PRINCIPLED'):
        surface.update({"base": socket(node, "Color"), "roughness": 0.9, "normal": socket(node, "Normal")})
    elif kind in ('MIX_SHADER', 'ADD_SHADER'):
        shaders = [s for s in node.inputs if s.type == 'SHADER']
        parts = [shader_surface(s.links[0].from_node if s.is_linked else None, depth + 1) for s in shaders[:2]]
        while len(parts) < 2:
            parts.append(dict(PLAIN_SURFACE))
        if kind == 'ADD_SHADER':
            fac = 0.5
        else:
            fac_socket = node.inputs[0] if node.inputs and node.inputs[0].type != 'SHADER' else None
            fac = 0.5 if fac_socket is None or fac_socket.is_linked else float(fac_socket.default_value)
        return blend_surfaces(parts[0], parts[1], max(0.0, min(1.0, fac)))
    elif kind == 'GROUP' and node.node_tree is not None:
        for inner in node.node_tree.nodes:
            if inner.type == 'GROUP_OUTPUT' and inner.is_active_output:
                for inp in inner.inputs:
                    if inp.type == 'SHADER' and inp.is_linked:
                        return shader_surface(inp.links[0].from_node, depth + 1)
    return surface


def blend_surfaces(a, b, fac):
    """Two surfaces as one: numbers blended by the factor, colours blended
    when both are plain, the sockets of the heavier side otherwise, and the
    emission of whichever glows."""
    heavy = b if fac >= 0.5 else a
    out = dict(heavy)
    for key, fallback in (("roughness", 0.5), ("metallic", 0.0), ("transmission", 0.0), ("ior", 1.45)):
        out[key] = number_of(a[key], fallback) * (1.0 - fac) + number_of(b[key], fallback) * fac
    if not is_socket(a["base"]) and not is_socket(b["base"]):
        ca, cb = colour_of(a["base"], (0.8, 0.8, 0.8)), colour_of(b["base"], (0.8, 0.8, 0.8))
        out["base"] = tuple(ca[i] * (1.0 - fac) + cb[i] * fac for i in range(3))
    glow_a = number_of(a["strength"], 0.0) * (1.0 - fac)
    glow_b = number_of(b["strength"], 0.0) * fac
    if glow_a > 0.0 or glow_b > 0.0:
        glowing = a if glow_a >= glow_b else b
        out["emission"] = glowing["emission"]
        out["strength"] = glow_a + glow_b
    return out


def surface_of(material):
    return shader_surface(output_shader(material))


def readable(value):
    """Whether Frost can take this as it is: a plain value, an unlinked
    socket, or a socket with a picture behind it that Frost can follow."""
    return not is_socket(value) or not value.is_linked or trace_image(value)[0] is not None


def needs_bake(material):
    """Whether the material's look reaches Frost only by baking: something
    wired into its surface that is neither a value nor a picture Frost can
    follow -- a procedural graph, a picture mapped by position."""
    if material is None or output_shader(material) is None:
        return False
    surface = surface_of(material)
    return not all(readable(surface[key]) for key in ("base", "roughness", "metallic", "normal", "emission"))


def bake_folder():
    base = os.path.join(os.path.expanduser("~"), "Library", "Caches", "Frost for Blender", "bakes")
    stem = bpy.path.clean_name(os.path.splitext(os.path.basename(bpy.data.filepath))[0]) or "untitled"
    folder = os.path.join(base, stem)
    os.makedirs(folder, exist_ok=True)
    return folder


def world_folder():
    folder = os.path.join(os.path.expanduser("~"), "Library", "Caches", "Frost for Blender", "worlds")
    os.makedirs(folder, exist_ok=True)
    return folder


def manifest_read(folder):
    """What has been baked into this folder before, by name or stamp. A
    bake lives on the object as a custom property, which is in the .blend
    only once it is saved; the manifest is so that opening a file that was
    never saved -- or saved before the bake -- does not bake it all again."""
    try:
        with open(os.path.join(folder, "baked.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def manifest_write(folder, key, record):
    data = manifest_read(folder)
    data[key] = record
    try:
        with open(os.path.join(folder, "baked.json"), "w") as f:
            json.dump(data, f, indent=1)
    except Exception:
        pass


def record_files_exist(record):
    """Every picture a bake names is still in the cache."""
    for entry in record.get("materials", {}).values():
        for key in ("color", "roughness", "normal", "emit"):
            path = entry.get(key)
            if path and not os.path.isfile(path):
                return False
    return True


def bake_record(obj):
    """The object's bake, as a plain dict, or None: from the object, or
    from the cache's manifest when this session has not baked it."""
    original = getattr(obj, "original", obj)
    record = original.get(BAKE_KEY)
    if record is not None:
        try:
            record = record.to_dict()
        except AttributeError:
            record = dict(record)
        return record if record_files_exist(record) else None
    record = manifest_read(bake_folder()).get(original.name)
    if record is None or not record_files_exist(record):
        return None
    return record


def graph_stamp(tree, extra=""):
    """A fingerprint of a node tree -- the nodes, what is typed into them,
    the pictures behind them, the wires -- so a bake knows the graph it was
    made from and a changed one is baked again."""
    parts = [extra]
    if tree is not None:
        for node in tree.nodes:
            parts.append(node.type + "|" + node.name)
            for sock in node.inputs:
                if sock.is_linked:
                    continue
                value = getattr(sock, "default_value", None)
                if value is None:
                    continue
                try:
                    parts.append(",".join("%.5g" % float(x) for x in value))
                except TypeError:
                    try:
                        parts.append("%.5g" % float(value))
                    except (TypeError, ValueError):
                        parts.append(str(value))
            image = getattr(node, "image", None)
            if image is not None:
                parts.append("%s %s %s" % (image.name, tuple(image.size),
                                           image.packed_file.size if image.packed_file else image.filepath))
            for attr in ("sky_type", "sun_elevation", "sun_rotation", "sun_intensity", "sun_size", "sun_disc",
                         "altitude", "air_density", "aerosol_density", "dust_density", "ozone_density", "operation",
                         "blend_type", "data_type", "noise_dimensions", "feature", "distance", "interpolation",
                         "uv_map", "attribute_name", "projection", "extension", "invert", "use_clamp",
                         "noise_type", "normalize", "wave_type", "bands_direction", "wave_profile", "gradient_type",
                         "coloring", "musgrave_type"):
                if hasattr(node, attr):
                    parts.append("%s=%s" % (attr, getattr(node, attr)))
            ramp = getattr(node, "color_ramp", None)
            if ramp is not None:
                parts.append(";".join("%.4f:%s" % (e.position, ",".join("%.4f" % c for c in e.color))
                                      for e in ramp.elements))
        for link in tree.links:
            parts.append("%s.%s>%s.%s" % (link.from_node.name, link.from_socket.identifier,
                                          link.to_node.name, link.to_socket.identifier))
    return hashlib.sha1("\n".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


def world_source(world):
    """The world's Background node and the node feeding its Color, either
    None when there is no such thing."""
    if world is None or not getattr(world, "use_nodes", True) or world.node_tree is None:
        return None, None
    background = None
    for node in world.node_tree.nodes:
        if node.type == 'BACKGROUND':
            background = node
            break
    if background is None:
        return None, None
    colour_socket = socket(background, "Color")
    source = colour_socket.links[0].from_node if colour_socket is not None and colour_socket.is_linked else None
    return background, source


# Bumped when the world baker itself changes, so a picture baked by an
# older one is made again rather than trusted.
WORLD_BAKE_VERSION = "3"


def world_stamp(scene):
    world = scene.world
    return graph_stamp(world.node_tree if world is not None else None,
                       (world.name if world is not None else "") + "|v" + WORLD_BAKE_VERSION)


def world_record(scene):
    """The scene's world bake, as a plain dict, when it is of the world as
    it stands and its picture is still there; else None."""
    original = getattr(scene, "original", scene)
    record = original.get(WORLD_KEY)
    if record is None:
        return None
    try:
        record = record.to_dict()
    except AttributeError:
        record = dict(record)
    if record.get("stamp") != world_stamp(scene) or not os.path.isfile(record.get("image", "")):
        return None
    return record


def world_record_cached(scene):
    """The world's bake from the scene, or from the cache's manifest when
    this session has not baked it."""
    record = world_record(scene)
    if record is not None:
        return record
    record = manifest_read(world_folder()).get(world_stamp(scene))
    if record is None or not os.path.isfile(record.get("image", "")):
        return None
    return record


def material_json(writer, material, overrides):
    """One glTF material, and what Frost's importer does not read -- the
    emission's strength, the glass -- into `overrides` by name."""
    entry = {"name": material.name if material else "default", "doubleSided": True,
             "pbrMetallicRoughness": {"baseColorFactor": [0.8, 0.8, 0.8, 1.0], "metallicFactor": 0.0,
                                      "roughnessFactor": 0.5}}
    if material is None:
        return entry
    entry["doubleSided"] = not material.use_backface_culling
    pbr = entry["pbrMetallicRoughness"]
    if output_shader(material) is None:
        colour = tuple(material.diffuse_color)
        pbr["baseColorFactor"] = [colour[0], colour[1], colour[2], 1.0]
        pbr["metallicFactor"] = float(material.metallic)
        pbr["roughnessFactor"] = float(material.roughness)
        return entry
    surface = surface_of(material)

    base = surface["base"]
    image, factor = trace_image(base) if is_socket(base) else (None, None)
    index = writer.image(image) if image is not None else None
    if index is not None:
        pbr["baseColorTexture"] = {"index": index}
        tint = factor if isinstance(factor, tuple) else (1.0, 1.0, 1.0)
        pbr["baseColorFactor"] = [float(tint[0]), float(tint[1]), float(tint[2]), 1.0]
    else:
        colour = colour_of(base, (0.8, 0.8, 0.8))
        pbr["baseColorFactor"] = [colour[0], colour[1], colour[2], 1.0]
    metal, rough = surface["metallic"], surface["roughness"]
    metal_image, metal_factor = trace_image(metal) if is_socket(metal) else (None, None)
    rough_image, rough_factor = trace_image(rough) if is_socket(rough) else (None, None)
    rough_index = writer.image(rough_image) if rough_image is not None else None
    if rough_index is not None and (metal_image is None or metal_image == rough_image):
        # glTF's packing, roughness in green and metalness in blue, which is
        # what the importer split and what the exporter joins again.
        pbr["metallicRoughnessTexture"] = {"index": rough_index}
        pbr["roughnessFactor"] = float(rough_factor) if isinstance(rough_factor, float) else 1.0
        if metal_image is not None:
            pbr["metallicFactor"] = float(metal_factor) if isinstance(metal_factor, float) else 1.0
        else:
            pbr["metallicFactor"] = number_of(metal, 0.0)
    else:
        pbr["metallicFactor"] = number_of(metal, 0.0)
        pbr["roughnessFactor"] = number_of(rough, 0.5)

    normal = surface["normal"]
    normal_image = image_behind(normal) if is_socket(normal) else None
    normal_index = writer.image(normal_image) if normal_image is not None else None
    if normal_index is not None:
        entry["normalTexture"] = {"index": normal_index}

    emission = colour_of(surface["emission"], (0.0, 0.0, 0.0))
    strength = number_of(surface["strength"], 0.0)
    emission_image = image_behind(surface["emission"]) if is_socket(surface["emission"]) else None
    emission_index = writer.image(emission_image) if emission_image is not None else None
    if emission_index is not None:
        entry["emissiveTexture"] = {"index": emission_index}
        emission = (1.0, 1.0, 1.0)
        strength = max(strength, 1.0)
    if strength > 0.0 and max(emission) > 0.0:
        entry["emissiveFactor"] = [min(float(c), 1.0) for c in emission]
        if strength != 1.0:
            entry.setdefault("extensions", {})["KHR_materials_emissive_strength"] = {"emissiveStrength": strength}
            writer.extensions_used.add("KHR_materials_emissive_strength")
        overrides.setdefault(material.name, {}).update(
            {"emission": [float(c) for c in emission], "emission_strength": strength})

    transmission = number_of(surface["transmission"], 0.0)
    if transmission > 0.0:
        ior = number_of(surface["ior"], 1.45)
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


def baked_material_slot(writer, material, entry, owner, overrides):
    """A material as its bake: the pictures Bake Materials for Frost made
    for it on this object, in the material's place, named for the object
    since a bake is of the material on that object."""
    key = "%s@%s" % (material.name if material else "default", owner)
    if key in writer.material_index:
        return writer.material_index[key]
    result = {"name": key, "doubleSided": not material.use_backface_culling if material else True,
              "pbrMetallicRoughness": {"baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                                       "metallicFactor": float(entry.get("metallic", 0.0)),
                                       "roughnessFactor": 1.0}}
    pbr = result["pbrMetallicRoughness"]
    index = writer.image_file(entry.get("color"))
    if index is not None:
        pbr["baseColorTexture"] = {"index": index}
    index = writer.image_file(entry.get("roughness"))
    if index is not None:
        # The bake packs roughness in green and the metallic value in blue.
        pbr["metallicRoughnessTexture"] = {"index": index}
        pbr["metallicFactor"] = 1.0
    else:
        # A plain roughness was not baked; the material's own value stands.
        pbr["roughnessFactor"] = float(entry.get("roughness_value", 0.5))
    index = writer.image_file(entry.get("normal"))
    if index is not None:
        result["normalTexture"] = {"index": index}
    strength = float(entry.get("emit_strength", 0.0))
    index = writer.image_file(entry.get("emit")) if strength > 0.0 else None
    if index is not None:
        result["emissiveTexture"] = {"index": index}
        result["emissiveFactor"] = [1.0, 1.0, 1.0]
        if strength != 1.0:
            result.setdefault("extensions", {})["KHR_materials_emissive_strength"] = {"emissiveStrength": strength}
            writer.extensions_used.add("KHR_materials_emissive_strength")
        overrides.setdefault(key, {}).update({"emission": [1.0, 1.0, 1.0], "emission_strength": strength})
    transmission = float(entry.get("transmission", 0.0))
    if transmission > 0.0:
        ior = float(entry.get("ior", 1.45))
        result.setdefault("extensions", {})["KHR_materials_transmission"] = {"transmissionFactor": transmission}
        result["extensions"]["KHR_materials_ior"] = {"ior": ior}
        writer.extensions_used.update({"KHR_materials_transmission", "KHR_materials_ior"})
        overrides.setdefault(key, {}).update({"transmission": transmission, "ior": ior})
    writer.materials.append(result)
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
    # A bake is of the material on that object, so a baked object's mesh is
    # its own even when the data is shared.
    if original.get(BAKE_KEY) is not None:
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


def weld(keys):
    """np.unique over the rows of an integer array, fast: each row hashed to
    64 bits, the hashes made unique, and the answer checked against the rows
    themselves -- two different rows sharing a hash, a one in ten million
    chance at three million rows, falls back to the exact sort of the rows,
    which is what np.unique(axis=0) does and is forty times slower (a
    million-triangle mesh spent 1.3 of its 2 seconds in it). Returns
    (first, inverse) as np.unique would."""
    h = np.zeros(keys.shape[0], dtype=np.uint64)
    with np.errstate(over='ignore'):
        for column in range(keys.shape[1]):
            h = h * np.uint64(0x9E3779B97F4A7C15) + keys[:, column].astype(np.uint64)
            h ^= h >> np.uint64(29)
    _, first, inverse = np.unique(h, return_index=True, return_inverse=True)
    inverse = inverse.reshape(-1)
    if np.array_equal(keys[first][inverse], keys):
        return first, inverse
    _, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    return first, inverse.reshape(-1)


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
        # A baked object reads its pictures by the bake's own UV map.
        bake = bake_record(obj)
        uv_layer = mesh.uv_layers.get(BAKE_UV) if bake is not None else None
        if uv_layer is None:
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
            first, inverse = weld(keys)
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
            baked = bake["materials"].get(str(material_index)) if bake is not None else None
            if baked is not None and baked.get("color"):
                primitive["material"] = baked_material_slot(writer, material, baked, obj.name, overrides)
            else:
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


# Blender's Sky Texture against Frio's atmosphere, measured: a grey plane
# under the demo's sky rendered in Cycles and through Frost with the sun
# mapped over, the plane's brightness with and without the sun disc, and
# these are the ratios (tools/bench/nishita_calibrate.py): Cycles' plane
# 6.25 with the sun and 1.76 without against Frost's 0.296 and 0.071.
# Against the tables Frost makes for the node's own air (tools/bench/
# nishita_calibrate.py): the fallback when the world has not been baked.
SUN_FROM_NISHITA = 10.0
SKY_FROM_NISHITA = 8.47
# The world's bake, recorded on the scene: an equirectangular picture of
# the sky Cycles rendered from the world's own nodes, and a Sky Texture's
# sun measured off its disc.
WORLD_KEY = "frost_world_bake"


def mapping_turn(node):
    """The turn about up of a Mapping node feeding an Environment Texture's
    Vector, in radians; nought when there is none."""
    vector = socket(node, "Vector")
    if vector is None or not vector.is_linked:
        return 0.0
    mapping = vector.links[0].from_node
    if mapping.type != 'MAPPING':
        return 0.0
    rotation = socket(mapping, "Rotation")
    try:
        return float(rotation.default_value[2]) if rotation is not None and not rotation.is_linked else 0.0
    except (TypeError, IndexError):
        return 0.0


def atmosphere_json(node, strength):
    """A Sky Texture node as Frio's atmosphere: the sun where the node puts
    it, the air by the node's aerosol, the ground's height. Nishita's sun
    direction is geographical_to_direction(elevation, rotation + pi/2)."""
    if hasattr(node, "sun_elevation"):
        elevation, rotation = float(node.sun_elevation), float(node.sun_rotation)
        # The Sky Texture's rotation is measured the other way round from a
        # bearing: the sun's azimuth is pi/2 - rotation. Sent as
        # rotation + pi/2 it came out 83 degrees off on Blender's own sky
        # demo, which is why Frost drew that scene with the glow in the
        # wrong half of the sky and the shadows pointing the wrong way.
        # Found by sweeping a narrow Cycles view round the horizon and
        # asking which way was bright.
        azimuth = math.pi / 2.0 - rotation
        to_sun = Vector((math.cos(elevation) * math.cos(azimuth),
                         math.cos(elevation) * math.sin(azimuth),
                         math.sin(elevation)))
        sun_strength = float(node.sun_intensity) if getattr(node, "sun_disc", True) else 0.0
        angle = float(getattr(node, "sun_size", 0.0095))
        altitude = float(getattr(node, "altitude", 0.0))
        air = float(getattr(node, "air_density", 1.0))
        aerosol = float(getattr(node, "aerosol_density", getattr(node, "dust_density", 1.0)))
        ozone = float(getattr(node, "ozone_density", 1.0))
    else:
        # The older models: a direction and a turbidity.
        to_sun = Vector(node.sun_direction).normalized()
        sun_strength = 1.0
        angle = 0.0095
        altitude = 0.0
        air = 1.0
        aerosol = float(getattr(node, "turbidity", 2.2)) / 2.2
        ozone = 1.0
    preset = "dusty" if aerosol >= 5.0 else ("hazy" if aerosol >= 2.0 else "clear")
    # The node's own three numbers -- the air, the aerosol and the ozone as
    # multiples of a clear day's -- go over as they are: Frost makes the
    # tables for exactly that air. The preset is the fallback for a Frost
    # from before it read them.
    return {
        "preset": preset,
        "air": air,
        "aerosol": aerosol,
        "ozone": ozone,
        "altitude": altitude,
        "strength": strength * SKY_FROM_NISHITA,
        "sun": {"direction": to_frost(-to_sun), "strength": strength * sun_strength * SUN_FROM_NISHITA,
                "angle": angle},
    }


def world_json(scene, warnings):
    """The world as (world, atmosphere, sun): a colour at a strength or a
    picture of the sky in the world block -- an Environment Texture as it
    is, or any other world as the picture Cycles baked of it, a Sky Texture
    included, with the Sky Texture's sun measured off its disc as a sun
    light; and, when a Sky Texture has not been baked, Blender's sky as an
    atmosphere block near it."""
    world = scene.world
    colour, strength = (0.05, 0.05, 0.05), 1.0
    result = None
    atmosphere = None
    sun = None
    if world is not None:
        background, source = world_source(world)
        if background is not None:
            strength = float(value_of(socket(background, "Strength"), 1.0))
            colour_socket = socket(background, "Color")
            record = world_record_cached(scene) if source is not None and source.type != 'TEX_ENVIRONMENT' else None
            if record is not None:
                # The world as Cycles drew it: the same sky, exactly, and
                # the same light off it. The bake includes the Background's
                # strength.
                result = {"image": record["image"], "strength": 1.0, "rotation": 0.0}
                if any(node.type == 'LIGHT_PATH' for node in world.node_tree.nodes):
                    warnings.append("This world shows one thing to the camera and another to everything else "
                                    "(a Light Path node). Frost has one sky: it is the one the world lights "
                                    "with, so the backdrop may differ from Cycles.")
                if record.get("sun"):
                    sun = record["sun"]
            elif source is not None and source.type == 'TEX_SKY':
                atmosphere = atmosphere_json(source, strength)
                warnings.append("The Sky Texture is rendered as an atmosphere near it; press F12 with Frost, or "
                                "Bake for Frost, to render the very same sky.")
            elif source is not None and source.type == 'TEX_ENVIRONMENT' and source.image is not None:
                path = cached_image(source.image, hdr=True)
                if path:
                    result = {"image": path, "strength": strength, "rotation": mapping_turn(source)}
                else:
                    warnings.append("The world's picture could not be written out (%s); a plain grey stands in."
                                    % (last_image_error or "no reason given"))
            elif source is not None:
                warnings.append("The world's colour comes from a %s node Frost does not read, and it has not been "
                                "baked; a plain grey stands in. Press F12 with Frost, or Bake for Frost."
                                % source.type.replace('_', ' ').lower())
            else:
                colour = tuple(value_of(colour_socket, (0.05, 0.05, 0.05, 1.0))[:3])
        else:
            colour = tuple(world.color)
    if result is None:
        result = {"color": [float(c) for c in colour], "strength": strength}
    return result, atmosphere, sun


# ---- volumes ---------------------------------------------------------------

def volume_node_of(obj):
    """The volume shader of a mesh whose every material is a volume and no
    surface -- a box of fog -- or None. Such a mesh is air, not a wall."""
    if obj.type != 'MESH' or not obj.material_slots:
        return None
    found = None
    for slot in obj.material_slots:
        material = slot.material
        if material is None or not getattr(material, "use_nodes", True) or material.node_tree is None:
            return None
        output = None
        for node in material.node_tree.nodes:
            if node.type == 'OUTPUT_MATERIAL' and (output is None or node.is_active_output):
                output = node
        if output is None:
            return None
        surface, volume = socket(output, "Surface"), socket(output, "Volume")
        if surface is None or volume is None or surface.is_linked or not volume.is_linked:
            return None
        node = volume.links[0].from_node
        if node.type not in {'PRINCIPLED_VOLUME', 'VOLUME_SCATTER', 'VOLUME_ABSORPTION'}:
            return None
        found = node
    return found


def volume_json(obj, matrix, node, name):
    """The box the mesh's bounds make, as a Frio domain: Frio's box is a unit
    cube about the node, so the bounds' centre and size are folded into the
    transform, which is then taken apart in Frost's axes."""
    corners = [Vector(c) for c in obj.bound_box]
    lo = Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))
    hi = Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners)))
    centre = (lo + hi) * 0.5
    size = Vector((max(hi.x - lo.x, 1e-3), max(hi.y - lo.y, 1e-3), max(hi.z - lo.z, 1e-3)))
    world = matrix @ Matrix.Translation(centre) @ Matrix.Diagonal(size).to_4x4()
    location, rotation, scale = world.decompose()
    turn = Matrix.Rotation(-math.pi / 2.0, 4, 'X').to_quaternion()
    q = turn @ rotation @ turn.inverted()
    colour = value_of(socket(node, "Color"), (1.0, 1.0, 1.0, 1.0))
    return {
        "name": name,
        "translation": to_frost(location),
        "rotation": [float(q.x), float(q.y), float(q.z), float(q.w)],
        "scale": [float(scale.x), float(scale.z), float(scale.y)],
        "density": float(value_of(socket(node, "Density"), 1.0)),
        "color": [float(c) for c in colour[:3]],
        "anisotropy": float(value_of(socket(node, "Anisotropy"), 0.0)),
    }


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
    volumes = []
    for instance in depsgraph.object_instances:
        obj = instance.object
        if obj.type not in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}:
            continue
        if not instance.show_self and not instance.is_instance:
            continue
        name = obj.name if not instance.is_instance else "%s.%d" % (obj.name, instance.random_id)
        # A box of fog is air, not a wall: written as a volume, never as a
        # mesh. Sent as a mesh it enclosed the whole scene in an opaque box
        # and every frame of the sky demo came out black.
        volume = volume_node_of(obj)
        if volume is not None:
            volumes.append(volume_json(obj, instance.matrix_world.copy(), volume, name))
            continue
        add_mesh_instance(writer, obj, instance.matrix_world.copy(), overrides, name)
    sun, lights = lights_json(depsgraph, writer, overrides, warnings)
    gltf = writer.write("scene")
    warnings.extend(writer.warnings)

    world, atmosphere, world_sun = world_json(scene, warnings)
    if sun is None and world_sun is not None:
        sun = world_sun
    setup = {
        "camera": camera_json(depsgraph, scene, width, height, warnings),
        "world": world,
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
    if atmosphere is not None:
        setup["atmosphere"] = atmosphere
    if volumes:
        setup["volumes"] = volumes
    if setup["camera"] is None:
        del setup["camera"]
    setup_path = os.path.join(out_dir, "setup.json")
    with open(setup_path, "w") as f:
        json.dump(setup, f, indent=1)
    return gltf, setup_path, warnings
