# Bake Materials for Frost: what Frost cannot read -- a procedural node
# graph, a picture mapped by Generated coordinates, a shader that is not the
# Principled BSDF -- baked by Cycles into pictures on a UV map made for the
# purpose, once, and used by the export in the material's place. This is
# what every pipeline that hands a Blender scene to another renderer does;
# the mech in Blender's sky demo is rust photographs mixed through colour
# ramps by pointiness and vertex colour, and no exporter can hand that
# graph over as it is.
#
# A bake is of a material on an object (Generated coordinates depend on the
# object's bounds), so the pictures and the UV map are per object, kept in
# ~/Library/Caches/Frost for Blender/bakes/<file>/, and named in a custom
# property on the object (BAKE_KEY) that the export reads. Forget the Bakes
# takes the property off; the UV map stays, since it does no harm.
import hashlib
import math
import os
import tempfile
import subprocess
import shutil
import time

import bpy
import numpy as np
from bpy.props import BoolProperty
from mathutils import Vector

from . import export

BAKE_UV = export.BAKE_UV
BAKE_KEY = export.BAKE_KEY
WORLD_KEY = export.WORLD_KEY

# (our name, Cycles' bake type, what the picture holds)
# The colour is baked as an emission of whatever drives Base Color, not as
# Cycles' DIFFUSE pass: a metal has no diffuse, so every metallic material's
# colour came back black -- a mirror with nothing to reflect, which is what an
# anodised part, a brushed panel and a gold trim all baked to.
PASSES = (("color", 'EMIT', 'sRGB'), ("roughness", 'ROUGHNESS', 'Non-Color'),
          ("normal", 'NORMAL', 'Non-Color'), ("emit", 'EMIT', 'sRGB'))
# Bakes made by an older baker are made again: 2 is the colour from the
# socket rather than from the diffuse pass, 3 is sixteen samples a texel.
OBJECT_BAKE_VERSION = "3"


def bake_size_for(obj, base):
    """A bake sized to the object: a screw does not need a hero's texture,
    and a scene of a thousand small parts would take an hour at the size a
    vehicle wants."""
    try:
        corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        low = Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))
        high = Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners)))
        diagonal = (high - low).length
    except Exception:
        return base
    if diagonal < 0.05:
        return max(128, base // 4)
    if diagonal < 0.3:
        return max(256, base // 2)
    return base


def object_stamp(obj, size, material_stamps=None):
    """What the object's bake was made from: its materials' graphs, its
    mesh, the size. A bake whose stamp no longer matches is baked again."""
    parts = ["bake=%s" % OBJECT_BAKE_VERSION, "size=%d" % size,
             "verts=%d" % (len(obj.data.vertices) if obj.data is not None else 0)]
    for slot in obj.material_slots:
        material = slot.material
        if material is None:
            parts.append("-")
            continue
        if material_stamps is not None and material.name in material_stamps:
            parts.append(material_stamps[material.name])
            continue
        stamp = export.graph_stamp(material.node_tree, material.name)
        if material_stamps is not None:
            material_stamps[material.name] = stamp
        parts.append(stamp)
    return hashlib.sha1("\n".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


def objects_needing_bake(scene, only_selected=False):
    """The mesh objects whose materials Frost cannot read and that have no
    bake, or a bake of other materials, another mesh or another size."""
    result = []
    base = int(scene.frost.bake_size)
    objects = bpy.context.selected_objects if only_selected else scene.objects
    material_stamps = {}
    unreadable = {}
    for obj in objects:
        if obj.type != 'MESH' or not obj.material_slots or obj.hide_render:
            continue
        needs = False
        for slot in obj.material_slots:
            material = slot.material
            if material is None:
                continue
            if material.name not in unreadable:
                unreadable[material.name] = export.needs_bake(material)
            if unreadable[material.name]:
                needs = True
        if not needs:
            continue
        record = export.bake_record(obj)
        if record is not None and record.get("stamp") == object_stamp(obj, bake_size_for(obj, base), material_stamps):
            continue
        result.append(obj)
    return result


def world_needs_bake(scene):
    """Whether the world reaches Frost only by baking -- a Sky Texture (the
    very same sky rather than an atmosphere near it), or a graph Frost does
    not read -- and has no bake of the world as it stands."""
    background, source = export.world_source(scene.world)
    if source is None or source.type == 'TEX_ENVIRONMENT':
        return False
    return export.world_record_cached(scene) is None


world_folder = export.world_folder


def blend_stem():
    return bpy.path.clean_name(os.path.splitext(os.path.basename(bpy.data.filepath))[0]) or "untitled"


def start_world_bake(scene):
    """Starts the world's bake in a second, headless Blender and returns
    (process, the folder to remove after), or None when there is nothing to
    start.

    The bake is a Cycles render of the sky, and a render cannot start a
    render: inside RenderEngine.render no operator has a context to run in.
    So the bake hung off the F12 operator, and a render started any other
    way -- Render > Render Image, a script, another add-on's button, the
    Rendered viewport -- went out with a plain grey for a world: a studio lit
    from every side at once, which looks nothing like the scene and says so
    only in a log. The world alone is written to a file of its own and baked
    from there, so it does not matter who started the render or whether the
    project was ever saved. The picture lands in the same cache under the
    same stamp, which is where every session looks."""
    original = getattr(scene, "original", scene)
    world = original.world
    if world is None:
        return None
    work = tempfile.mkdtemp(prefix="frost-world-")
    blend = os.path.join(work, "world.blend")
    try:
        bpy.data.libraries.write(blend, {world}, fake_user=True, path_remap='ABSOLUTE')
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bake_world_cli.py")
    command = [bpy.app.binary_path, "-b", "--factory-startup", blend, "--python", script, "--",
               world.name, export.world_stamp(original), blend_stem(),
               "1" if original.render.film_transparent else "0"]
    # Into a file rather than a pipe: the viewport does not sit reading it,
    # and a pipe nobody reads fills, and the bake stops with it.
    output = open(os.path.join(work, "bake.log"), "w")
    process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
    output.close()
    return process, work


def finish_world_bake(started, log=None):
    """Waits for a bake started above; True when the picture is there."""
    process, work = started
    output = ""
    try:
        process.wait(timeout=300)
        with open(os.path.join(work, "bake.log"), errors="replace") as f:
            output = f.read()
    except subprocess.TimeoutExpired:
        process.kill()
        output = "timed out"
    except OSError:
        pass
    finally:
        shutil.rmtree(work, ignore_errors=True)
    ok = process.returncode == 0 and "frost world baked" in (output or "")
    if not ok and log is not None:
        tail = [line for line in (output or "").splitlines() if line.strip()][-4:]
        log("the world could not be baked: " + " / ".join(tail))
    return ok


def save_linear_exr(pixels, width, height, path):
    """Float RGBA rows, from the bottom up as Blender keeps them, written
    as a plain OpenEXR tagged linear."""
    fresh = bpy.data.images.new("frost-world", width, height, float_buffer=True, alpha=True)
    try:
        try:
            fresh.colorspace_settings.name = 'Linear Rec.709'
        except TypeError:
            pass
        fresh.pixels.foreach_set(np.ascontiguousarray(pixels, dtype=np.float32).reshape(-1))
        fresh.filepath_raw = path
        fresh.file_format = 'OPEN_EXR'
        fresh.save()
    finally:
        bpy.data.images.remove(fresh)


def world_panorama(temp, width, height, path):
    """One equirectangular render of the temporary world scene, as linear
    floats. Both branches of a world go through this, so the two are shot
    through the very same lens and land on the same texels."""
    raw = path + ".raw.exr"
    temp.render.resolution_x, temp.render.resolution_y = width, height
    temp.render.filepath = raw
    bpy.ops.render.render(write_still=True, scene=temp.name)
    image = bpy.data.images.load(raw)
    try:
        pixels = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(pixels)
    finally:
        bpy.data.images.remove(image)
    try:
        os.remove(raw)
    except OSError:
        pass
    return pixels.reshape(height, width, 4)


def flat_colour(pixels):
    """The one colour a picture is, when it is one colour and nothing else;
    otherwise None. A studio's backdrop is usually a flat grey or a black,
    and the colour itself is exact where a two-megapixel picture of it is a
    file to carry around and read back."""
    rgb = pixels[:, :, :3].reshape(-1, 3)
    low, high = rgb.min(axis=0), rgb.max(axis=0)
    if float(np.max(high - low)) > 1e-4:
        return None
    return [float(c) for c in (low + high) * 0.5]


# What a Light Path node says to a ray that carries light onto a surface
# rather than a picture to the camera.
LIGHTING_RAY = {"Is Camera Ray": 0.0, "Is Shadow Ray": 0.0, "Is Diffuse Ray": 1.0, "Is Glossy Ray": 0.0,
                "Is Singular Ray": 0.0, "Is Reflection Ray": 1.0, "Is Transmission Ray": 0.0,
                "Is Volume Scatter Ray": 0.0, "Ray Length": 1.0, "Ray Depth": 1.0, "Diffuse Depth": 1.0,
                "Glossy Depth": 0.0, "Transparent Depth": 0.0, "Transmission Depth": 0.0, "Portal Depth": 0.0}


def pin_light_paths(tree, seen=None):
    """Unhooks every Light Path output in the tree and holds what it fed at
    the value a lighting ray would have given it; returns what to put back.

    Muting the node is the obvious way to take the camera out of a world,
    and it does not do that: a muted node with nothing to pass through
    leaves whatever it fed at that socket's own default, and a Mix's factor
    defaults to a half. Every studio world -- one thing for the camera,
    another for the light -- was baked as half of each: lit at half its
    strength, with half of the backdrop mixed into the light."""
    pinned = []
    if tree is None:
        return pinned
    seen = seen if seen is not None else set()
    if tree.name in seen:
        return pinned
    seen.add(tree.name)
    for node in tree.nodes:
        if node.type == 'GROUP' and node.node_tree is not None:
            pinned.extend(pin_light_paths(node.node_tree, seen))
        if node.type != 'LIGHT_PATH':
            continue
        for output in node.outputs:
            value = LIGHTING_RAY.get(output.name, 0.0)
            for link in list(output.links):
                source, target = link.from_socket, link.to_socket
                if not hasattr(target, "default_value"):
                    continue
                before = target.default_value
                try:
                    before = tuple(before)
                except TypeError:
                    pass
                tree.links.remove(link)
                try:
                    if isinstance(before, tuple):
                        target.default_value = tuple([value] * 3 + [1.0])[:len(before)]
                    else:
                        target.default_value = value
                except (TypeError, ValueError):
                    pass
                pinned.append((tree, source, target, before))
    return pinned


def unpin_light_paths(pinned):
    for tree, source, target, before in pinned:
        try:
            target.default_value = before
            tree.links.new(source, target)
        except Exception:
            pass


def bake_world(scene, folder, stamp=None, stem=None):
    """The world alone, rendered by Cycles as an equirectangular picture of
    the sky and recorded on the scene, so Frost lights the scene with the
    very same sky and shows it behind: a Sky Texture, a graph of colour
    ramps and blackbodies, anything.

    The sun's disc is in the picture like everything else in the sky: at
    two thousand texels round, a half-degree disc covers a few of them,
    which is where the sun is and how soft its shadow is, and Frost's
    environment sampling finds them. It was measured on its own for an
    afternoon instead -- a narrow view at the sun with the disc on and then
    off, the difference times the solid angle -- and the two renders came
    back identical to four figures, so the sun was left out of the export
    altogether and every frame came back three times too dark.

    A world that shows one thing to the camera and another to everything
    else -- a Light Path node into a Mix, which is how a studio backdrop is
    made black behind the product while the softboxes still light it -- is
    baked twice. Once with those nodes held at what a lighting ray gives
    them (`pin_light_paths`), which is what lights the scene and becomes
    the sky; and once as the world stands, which is what
    the camera sees and becomes the backdrop: a colour when it is one
    colour, a picture when it is not. Baking the lighting branch alone hung
    the softbox rig itself behind the product -- "it just puts in skies that
    were not a part of the project" -- and baking the camera's branch alone
    is the black frame this started as.

    Returns the record, or raises."""
    world = scene.world
    background, source = export.world_source(world)
    if background is None:
        return None
    # Given when this is a second Blender baking on another's behalf: the
    # stamp is the asking session's, so that session finds the picture.
    stem = stem or blend_stem()
    stamp = stamp or export.world_stamp(scene)
    path = os.path.join(folder, "%s-%s-%s.exr" % (stem, bpy.path.clean_name(world.name), stamp))
    width, height = 2048, 1024
    tree = world.node_tree
    pinned = []
    temp = bpy.data.scenes.new("Frost World Bake")
    camera_data = bpy.data.cameras.new("Frost World Camera")
    camera = bpy.data.objects.new("Frost World Camera", camera_data)
    try:
        temp.world = world
        temp.collection.objects.link(camera)
        temp.camera = camera
        temp.render.engine = 'CYCLES'
        temp.cycles.samples = 4
        temp.cycles.use_denoising = False
        temp.cycles.use_adaptive_sampling = False
        temp.cycles.device = 'GPU' if metal_ready() else 'CPU'
        temp.render.resolution_percentage = 100
        temp.render.film_transparent = False
        temp.view_settings.view_transform = 'Standard'
        temp.view_settings.look = 'None'
        temp.render.image_settings.file_format = 'OPEN_EXR'
        temp.render.image_settings.color_depth = '32'
        temp.render.dither_intensity = 0.0
        camera_data.type = 'PANO'
        try:
            camera_data.panorama_type = 'EQUIRECTANGULAR'
        except AttributeError:
            camera_data.cycles.panorama_type = 'EQUIRECTANGULAR'
        # Turned so the picture is what an Environment Texture maps back
        # onto the sky, texel for texel: a camera looks out at the sphere
        # and a texture is looked at from inside it. Measured, not reasoned
        # about -- a sun put at a known bearing, the panorama rendered at
        # each quarter turn, and the one whose brightest texel lands where
        # the Environment Texture's own mapping says it should.
        camera.rotation_euler = (math.pi / 2.0, 0.0, -math.pi / 2.0)
        record = {"stamp": stamp, "image": path, "when": time.strftime("%Y-%m-%d %H:%M")}

        # What lights the scene: the world as a ray that is not the camera's
        # finds it.
        pinned = pin_light_paths(tree)
        try:
            save_linear_exr(world_panorama(temp, width, height, path), width, height, path)
        finally:
            unpin_light_paths(pinned)

        # And what the camera sees, when the project says that is something
        # else. A transparent film says it too: Frost has no alpha channel,
        # and black is what a transparent film is composited over.
        if scene.render.film_transparent:
            record["backdrop_color"] = [0.0, 0.0, 0.0]
        elif pinned:
            seen = world_panorama(temp, width, height, path + ".backdrop")
            colour = flat_colour(seen)
            if colour is not None:
                record["backdrop_color"] = colour
            else:
                backdrop = path[:-4] + "-backdrop.exr"
                save_linear_exr(seen, width, height, backdrop)
                record["backdrop"] = backdrop

        original = getattr(scene, "original", scene)
        original[WORLD_KEY] = record
        export.manifest_write(folder, stamp, record)
        return record
    finally:
        try:
            bpy.data.scenes.remove(temp)
            bpy.data.objects.remove(camera)
            bpy.data.cameras.remove(camera_data)
        except (ReferenceError, RuntimeError, TypeError):
            pass


def pending(scene):
    """What a render would bake first: the objects, and whether the world."""
    return objects_needing_bake(scene), world_needs_bake(scene)


def bake_pending(scene, report=None, wm=None, only_selected=False):
    """Bakes every object that needs it, sized to the object, and the world
    when it needs it. Returns (objects baked, objects wanted, world baked)."""
    base = int(scene.frost.bake_size)
    objects = objects_needing_bake(scene, only_selected)
    world = world_needs_bake(scene)
    if not objects and not world:
        return 0, 0, False
    folder = bake_folder()
    total = len(objects) + (1 if world else 0)
    if wm is not None:
        wm.progress_begin(0, total)
    done = 0
    world_done = False
    try:
        for k, obj in enumerate(objects):
            if wm is not None:
                wm.progress_update(k)
            try:
                if bake_object(scene, obj, bake_size_for(obj, base), folder) is not None:
                    done += 1
            except RuntimeError as error:
                if report is not None:
                    report({'WARNING'}, "%s: %s" % (obj.name, error))
        if world:
            if wm is not None:
                wm.progress_update(total - 1)
            try:
                world_done = bake_world(scene, world_folder()) is not None
            except RuntimeError as error:
                if report is not None:
                    report({'WARNING'}, "the world: %s" % error)
    finally:
        if wm is not None:
            wm.progress_end()
    return done, len(objects), world_done


bake_folder = export.bake_folder


def ensure_uv(obj):
    """The bake's UV map: made by Smart UV Project when the object has none
    of that name, so every face has a place of its own on the picture. The
    map is made active (the bake writes into the active map) but not the
    render map: the materials' own mapping must stay what it was while they
    are baked."""
    mesh = obj.data
    if mesh.uv_layers.get(BAKE_UV) is None:
        mesh.uv_layers.new(name=BAKE_UV)
        mesh.uv_layers.active_index = mesh.uv_layers.find(BAKE_UV)
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.smart_project(angle_limit=math.radians(66.0), island_margin=0.02, correct_aspect=True,
                                 scale_to_bounds=False)
        bpy.ops.object.mode_set(mode='OBJECT')
    # By name, and after the edit-mode round trip: leaving edit mode rebuilds
    # the mesh's layers, and a reference taken before it can point at
    # nothing -- "No active UV layer found" from the bake, on the objects
    # that had just been unwrapped.
    index = mesh.uv_layers.find(BAKE_UV)
    if index < 0:
        raise RuntimeError("the bake's UV map could not be made on %s" % obj.name)
    mesh.uv_layers.active_index = index
    return mesh.uv_layers[index]


def metal_ready():
    """Whether Cycles is set up to use a Metal GPU in this Blender: the bake
    goes there when it is, and stays on the CPU otherwise rather than change
    the person's preferences."""
    try:
        prefs = bpy.context.preferences.addons['cycles'].preferences
        return prefs.compute_device_type == 'METAL' and any(
            device.type == 'METAL' and device.use for device in prefs.devices)
    except Exception:
        return False


def emit_base_colour(targets, colour_sources):
    """Rewires each material to emit whatever drives its Base Color, so an
    EMIT bake is a bake of that socket and nothing else -- no lighting, no
    diffuse-or-metal question. Returns what to put back."""
    rewired = []
    for index, material, _, _, _ in targets:
        tree = material.node_tree
        output = next((n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
        if output is None:
            continue
        surface = output.inputs["Surface"]
        original = surface.links[0].from_socket if surface.is_linked else None
        emission = tree.nodes.new('ShaderNodeEmission')
        emission.name = "Frost Bake Colour"
        emission.inputs["Strength"].default_value = 1.0
        source = colour_sources.get(index)
        if source is not None:
            tree.links.new(source, emission.inputs["Color"])
        else:
            emission.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
        tree.links.new(emission.outputs[0], surface)
        rewired.append((tree, output, original, emission))
    return rewired


def restore_surfaces(rewired):
    for tree, output, original, emission in rewired:
        try:
            if original is not None:
                tree.links.new(original, output.inputs["Surface"])
            tree.nodes.remove(emission)
        except Exception:
            pass


def bake_object(scene, obj, size, folder):
    """Bakes every material on the object into pictures of `size` and
    records them on the object. Returns the record, or raises."""
    saved = {"engine": scene.render.engine, "samples": scene.cycles.samples, "device": scene.cycles.device,
             "denoise": scene.cycles.use_denoising, "adaptive": scene.cycles.use_adaptive_sampling,
             "active": bpy.context.view_layer.objects.active, "selected": list(bpy.context.selected_objects)}
    bake = scene.render.bake
    saved_bake = {name: getattr(bake, name) for name in
                  ("use_selected_to_active", "margin", "use_clear", "target", "use_pass_direct",
                   "use_pass_indirect", "use_pass_color", "normal_space")}
    scene.render.engine = 'CYCLES'
    # Sixteen samples a texel, not one. A bake has no lighting noise in it,
    # but a procedural finish -- a blasted metal's speckle, a moulded
    # plastic's grain -- varies well inside one texel, and one sample of it is
    # one random draw: every texel a facet with its own tilt and tone, which
    # renders as grit, brighter and rougher than the surface is. Averaged
    # over the texel it is what a camera that far away sees. On the GPU when
    # the person's Cycles already uses it.
    scene.cycles.samples = 16
    scene.cycles.device = 'GPU' if metal_ready() else 'CPU'
    scene.cycles.use_denoising = False
    scene.cycles.use_adaptive_sampling = False
    bake.use_selected_to_active = False
    bake.margin = max(4, size // 128)
    bake.use_clear = True
    bake.target = 'IMAGE_TEXTURES'
    bake.use_pass_direct = False
    bake.use_pass_indirect = False
    bake.use_pass_color = True
    bake.normal_space = 'TANGENT'

    targets = []   # (slot index, material, the temporary node, the picture, the node that was active)
    colour_sources = {}   # slot index -> the socket that drives the material's Base Color
    wanted = set()   # the passes any material on the object needs
    record = {"uv": BAKE_UV, "size": size, "materials": {}, "when": time.strftime("%Y-%m-%d %H:%M"),
              "stamp": object_stamp(obj, size)}
    try:
        ensure_uv(obj)
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        stem = bpy.path.clean_name(obj.name)
        for index, slot in enumerate(obj.material_slots):
            material = slot.material
            if material is None or material.node_tree is None:
                continue
            nodes = material.node_tree.nodes
            node = nodes.new('ShaderNodeTexImage')
            node.name = "Frost Bake Target"
            node.label = "Frost bake target (removed after)"
            picture = bpy.data.images.new("FrostBake-%s-%d" % (stem, index), size, size, alpha=False,
                                          float_buffer=True)
            node.image = picture
            targets.append((index, material, node, picture, nodes.active))
            nodes.active = node
            surface = export.surface_of(material)
            record["materials"][str(index)] = {
                "metallic": export.number_of(surface["metallic"], 0.0),
                "roughness_value": export.number_of(surface["roughness"], 0.5),
                "transmission": export.number_of(surface["transmission"], 0.0),
                "ior": export.number_of(surface["ior"], 1.45),
            }
            # Only the passes the material needs: each one is a whole bake,
            # with the object's acceleration structure built again, and a
            # million-triangle part spends most of its time there. A colour
            # nothing is wired into is a number, not a picture.
            base = surface["base"]
            if export.is_socket(base) and base.is_linked:
                wanted.add("color")
                colour_sources[index] = base.links[0].from_socket
            else:
                record["materials"][str(index)]["color_value"] = [
                    float(c) for c in export.colour_of(base, (0.8, 0.8, 0.8))]
            if export.is_socket(surface["roughness"]) and surface["roughness"].is_linked:
                wanted.add("roughness")
            if export.is_socket(surface["normal"]) and surface["normal"].is_linked:
                wanted.add("normal")
            if (export.is_socket(surface["emission"]) and surface["emission"].is_linked) or \
                    export.number_of(surface["strength"], 0.0) > 0.0:
                wanted.add("emit")
        if not targets:
            return None
        for name, kind, space in PASSES:
            if name not in wanted:
                continue
            for _, _, node, picture, _ in targets:
                picture.colorspace_settings.name = space
                node.image = picture
            rewired = emit_base_colour(targets, colour_sources) if name == "color" else []
            try:
                bpy.ops.object.bake(type=kind, margin=bake.margin, use_clear=True)
            finally:
                restore_surfaces(rewired)
            for index, material, node, picture, _ in targets:
                entry = record["materials"][str(index)]
                if name == "color" and index not in colour_sources:
                    continue
                path = os.path.join(folder, "%s-%d-%s.png" % (stem, index, name))
                pixels = np.empty(size * size * 4, dtype=np.float32)
                picture.pixels.foreach_get(pixels)
                pixels = pixels.reshape(size, size, 4)
                if name == "emit":
                    # The strength is the brightest texel, and the picture is
                    # the rest over it: a PNG holds nothing over one.
                    strength = float(pixels[:, :, :3].max())
                    entry["emit_strength"] = strength
                    if strength <= 1e-4:
                        continue
                    pixels = pixels.copy()
                    pixels[:, :, :3] /= max(strength, 1.0)
                    entry["emit_strength"] = max(strength, 1.0)
                elif name == "roughness":
                    # glTF's packing, which Frost's importer splits: roughness in
                    # green and the metallic value in blue.
                    packed = pixels.copy()
                    packed[:, :, 0] = 0.0
                    packed[:, :, 2] = entry["metallic"]
                    pixels = packed
                out = bpy.data.images.new("FrostBakeOut", size, size, alpha=False, float_buffer=True)
                try:
                    out.colorspace_settings.name = space
                    out.pixels.foreach_set(pixels.reshape(-1))
                    out.filepath_raw = path
                    out.file_format = 'PNG'
                    out.save()
                finally:
                    bpy.data.images.remove(out)
                entry[name] = path
        obj[BAKE_KEY] = record
        export.manifest_write(folder, obj.name, record)
        return record
    finally:
        for _, material, node, picture, was_active in targets:
            try:
                material.node_tree.nodes.remove(node)
                if was_active is not None:
                    material.node_tree.nodes.active = was_active
            except Exception:
                pass
            try:
                bpy.data.images.remove(picture)
            except Exception:
                pass
        for name, value in saved_bake.items():
            setattr(bake, name, value)
        scene.cycles.samples = saved["samples"]
        scene.cycles.device = saved["device"]
        scene.cycles.use_denoising = saved["denoise"]
        scene.cycles.use_adaptive_sampling = saved["adaptive"]
        scene.render.engine = saved["engine"]
        bpy.ops.object.select_all(action='DESELECT')
        for other in saved["selected"]:
            try:
                other.select_set(True)
            except Exception:
                pass
        if saved["active"] is not None:
            bpy.context.view_layer.objects.active = saved["active"]


class FROST_OT_bake(bpy.types.Operator):
    bl_idname = "frost.bake"
    bl_label = "Bake Materials for Frost"
    bl_description = ("Bakes every material Frost cannot read -- procedural graphs, pictures mapped by "
                      "position, shaders other than the Principled -- into textures on a UV map made for "
                      "it, once per object, and renders with those")
    bl_options = {'REGISTER'}

    only_selected: BoolProperty(name="Selected Only", default=False)

    def execute(self, context):
        scene = context.scene
        started = time.time()
        done, wanted, world = bake_pending(scene, self.report, context.window_manager, self.only_selected)
        if not wanted and not world:
            self.report({'INFO'}, "Nothing to bake: Frost reads everything here as it is, or it is baked already")
            return {'FINISHED'}
        self.report({'INFO'}, "Baked %d of %d objects%s in %.0f s; Frost renders with the bakes" %
                    (done, wanted, " and the world" if world else "", time.time() - started))
        return {'FINISHED'}


class FROST_OT_clear_bake(bpy.types.Operator):
    bl_idname = "frost.clear_bake"
    bl_label = "Forget the Bakes"
    bl_description = "Renders every material as it is again; the baked pictures stay in the cache"
    bl_options = {'REGISTER'}

    def execute(self, context):
        count = 0
        for obj in context.scene.objects:
            if obj.get(BAKE_KEY) is not None:
                del obj[BAKE_KEY]
                count += 1
        if context.scene.get(WORLD_KEY) is not None:
            del context.scene[WORLD_KEY]
        self.report({'INFO'}, "Forgot the bakes on %d objects and the world" % count)
        return {'FINISHED'}


class FROST_OT_render(bpy.types.Operator):
    """Render with Frost: what Frost cannot read as it is -- a procedural
    material, a picture mapped by position, the world -- is baked first,
    then the render starts. On F12 and Ctrl+F12 while Frost is the engine."""
    bl_idname = "frost.render"
    bl_label = "Render with Frost"
    bl_description = ("Bakes whatever Frost cannot read as it is -- materials, the world -- then renders; "
                      "F12, or Ctrl+F12 for the animation")
    bl_options = {'REGISTER'}

    animation: BoolProperty(name="Animation", default=False)

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.scene.render.engine == 'FROST'

    def execute(self, context):
        scene = context.scene
        if scene.frost.auto_bake:
            started = time.time()
            try:
                done, wanted, world = bake_pending(scene, self.report, context.window_manager)
            except RuntimeError as error:
                self.report({'WARNING'}, "Baking: %s" % error)
                done, wanted, world = 0, 0, False
            if wanted or world:
                self.report({'INFO'}, "Baked %d of %d objects%s for Frost in %.0f s" %
                            (done, wanted, " and the world" if world else "", time.time() - started))
        return bpy.ops.render.render('INVOKE_DEFAULT', animation=self.animation)


classes = (FROST_OT_bake, FROST_OT_clear_bake, FROST_OT_render)
addon_keymaps = []


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    # F12 and Ctrl+F12 go to the operator above while Frost is the engine
    # (its poll), and fall through to Blender's own render otherwise.
    try:
        keyconfig = bpy.context.window_manager.keyconfigs.addon
    except AttributeError:
        keyconfig = None
    if keyconfig is not None:
        keymap = keyconfig.keymaps.new(name='Screen', space_type='EMPTY')
        still = keymap.keymap_items.new("frost.render", 'F12', 'PRESS')
        still.properties.animation = False
        animation = keymap.keymap_items.new("frost.render", 'F12', 'PRESS', ctrl=True)
        animation.properties.animation = True
        addon_keymaps.extend([(keymap, still), (keymap, animation)])


def unregister():
    for keymap, item in addon_keymaps:
        try:
            keymap.keymap_items.remove(item)
        except Exception:
            pass
    addon_keymaps.clear()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
