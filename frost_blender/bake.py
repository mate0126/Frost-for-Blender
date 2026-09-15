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
PASSES = (("color", 'DIFFUSE', 'sRGB'), ("roughness", 'ROUGHNESS', 'Non-Color'),
          ("normal", 'NORMAL', 'Non-Color'), ("emit", 'EMIT', 'sRGB'))


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
    parts = ["size=%d" % size, "verts=%d" % (len(obj.data.vertices) if obj.data is not None else 0)]
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


def bake_world(scene, folder):
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
    baked with those nodes muted, so what is captured is what lights the
    scene rather than the black the camera sees. On the user's own product
    scene that is forty-eight times as much light, and the difference
    between a rendered frame and a black one.

    Returns the record, or raises."""
    world = scene.world
    background, source = export.world_source(world)
    if background is None:
        return None
    muted = []
    stem = bpy.path.clean_name(os.path.splitext(os.path.basename(bpy.data.filepath))[0]) or "untitled"
    stamp = export.world_stamp(scene)
    path = os.path.join(folder, "%s-%s-%s.exr" % (stem, bpy.path.clean_name(world.name), stamp))
    temp = bpy.data.scenes.new("Frost World Bake")
    camera_data = bpy.data.cameras.new("Frost World Camera")
    camera = bpy.data.objects.new("Frost World Camera", camera_data)
    try:
        for node in world.node_tree.nodes:
            if node.type == 'LIGHT_PATH' and not node.mute:
                node.mute = True
                muted.append(node)
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
        width, height = 2048, 1024
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
        save_linear_exr(pixels.reshape(height, width, 4), width, height, path)
        record = {"stamp": stamp, "image": path, "when": time.strftime("%Y-%m-%d %H:%M")}
        original = getattr(scene, "original", scene)
        original[WORLD_KEY] = record
        export.manifest_write(folder, stamp, record)
        return record
    finally:
        for node in muted:
            node.mute = False
        bpy.data.objects.remove(camera)
        bpy.data.cameras.remove(camera_data)
        bpy.data.scenes.remove(temp)


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
    # A colour, a roughness or a normal has no noise in it: one sample a
    # texel is the answer. On the GPU when the person's Cycles already uses it.
    scene.cycles.samples = 1
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
            # million-triangle part spends most of its time there.
            wanted.add("color")
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
            bpy.ops.object.bake(type=kind, pass_filter={'COLOR'} if kind == 'DIFFUSE' else set(),
                                margin=bake.margin, use_clear=True)
            for index, material, node, picture, _ in targets:
                entry = record["materials"][str(index)]
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
