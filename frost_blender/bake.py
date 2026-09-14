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
import math
import os
import time

import bpy
import numpy as np
from bpy.props import BoolProperty

from . import export

BAKE_UV = export.BAKE_UV
BAKE_KEY = export.BAKE_KEY

# (our name, Cycles' bake type, what the picture holds)
PASSES = (("color", 'DIFFUSE', 'sRGB'), ("roughness", 'ROUGHNESS', 'Non-Color'),
          ("normal", 'NORMAL', 'Non-Color'), ("emit", 'EMIT', 'sRGB'))


def objects_needing_bake(scene, only_selected=False):
    """The mesh objects whose materials Frost cannot read and that have no
    bake yet, or whose bake is older than the last save."""
    result = []
    objects = bpy.context.selected_objects if only_selected else scene.objects
    for obj in objects:
        if obj.type != 'MESH' or not obj.material_slots or obj.hide_render:
            continue
        if obj.get(BAKE_KEY) is not None:
            continue
        if any(export.needs_bake(slot.material) for slot in obj.material_slots if slot.material is not None):
            result.append(obj)
    return result


def bake_folder():
    base = os.path.join(os.path.expanduser("~"), "Library", "Caches", "Frost for Blender", "bakes")
    stem = bpy.path.clean_name(os.path.splitext(os.path.basename(bpy.data.filepath))[0]) or "untitled"
    folder = os.path.join(base, stem)
    os.makedirs(folder, exist_ok=True)
    return folder


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
    record = {"uv": BAKE_UV, "size": size, "materials": {}, "when": time.strftime("%Y-%m-%d %H:%M")}
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
        size = int(scene.frost.bake_size)
        objects = objects_needing_bake(scene, self.only_selected)
        if not objects:
            self.report({'INFO'}, "Nothing to bake: Frost reads every material here, or it is baked already")
            return {'FINISHED'}
        folder = bake_folder()
        wm = context.window_manager
        wm.progress_begin(0, len(objects))
        started = time.time()
        done = 0
        try:
            for k, obj in enumerate(objects):
                wm.progress_update(k)
                try:
                    if bake_object(scene, obj, size, folder) is not None:
                        done += 1
                except RuntimeError as error:
                    self.report({'WARNING'}, "%s: %s" % (obj.name, error))
        finally:
            wm.progress_end()
        self.report({'INFO'}, "Baked %d of %d objects at %d in %.0f s; Frost renders with the bakes" %
                    (done, len(objects), size, time.time() - started))
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
        self.report({'INFO'}, "Forgot the bakes on %d objects" % count)
        return {'FINISHED'}


classes = (FROST_OT_bake, FROST_OT_clear_bake)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
