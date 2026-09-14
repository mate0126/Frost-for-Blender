# The render settings Frost has rows for, kept on the scene so they save
# with the file, and the add-on's own preference: where frost is.

import os
import platform
import shutil
import stat
import subprocess

import bpy
from bpy.props import BoolProperty, FloatProperty, IntProperty, PointerProperty, StringProperty


class FrostSceneSettings(bpy.types.PropertyGroup):
    samples: IntProperty(
        name="Samples", default=128, min=1, max=16384,
        description="Paths per pixel. With adaptive sampling on, the most any bucket takes")
    bounces: IntProperty(
        name="Bounces", default=6, min=1, max=32,
        description="How many times a path may bounce; 1 is direct light only")
    denoise: BoolProperty(
        name="Denoise", default=True,
        description="Filter the grain out of the finished frame, guided by what each pixel is looking at")
    adaptive: BoolProperty(
        name="Adaptive Sampling", default=True,
        description="A bucket stops once its pixels have settled to within the noise threshold")
    noise_threshold: FloatProperty(
        name="Noise Threshold", default=0.01, min=0.001, max=0.5, precision=3,
        description="The relative error a bucket may stop at. 0.01 is a clean frame; 0.05 is a preview")
    filter_glossy: FloatProperty(
        name="Filter Glossy", default=1.0, min=0.0, max=10.0,
        description="A glossy surface seen off a matt one is shaded a little rougher, so a highlight "
                    "bouncing onto a floor is a soft glow rather than specks. 1 matches Blender; 0 is exact")
    exposure: FloatProperty(
        name="Exposure", default=1.0, min=0.01, max=100.0,
        description="Multiplies the picture before the tonemap")
    viewport_scale: FloatProperty(
        name="Viewport Scale", default=0.5, min=0.125, max=1.0,
        description="The traced viewport's resolution as a fraction of the region: a half is four times "
                    "fewer paths than full size and answers a move far sooner")


def bundled_frost():
    """The frost executable that ships inside the add-on, if it does."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(here, "bin", "frost")
    return candidate if os.path.isfile(candidate) else ""


def repair_executable(path):
    """Blender installs an add-on zip with Python's zip reader, which keeps
    no permission bits, so the executable arrives unable to run; and a zip
    that came from a download leaves its quarantine flag on everything in
    it. Both are put right here, on our own signed and notarised binary."""
    try:
        mode = os.stat(path).st_mode
        if not mode & stat.S_IXUSR:
            os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
    try:
        subprocess.run(["xattr", "-d", "com.apple.quarantine", path], capture_output=True, check=False)
    except OSError:
        pass
    return os.access(path, os.X_OK)


def find_frost(preferences=None):
    """Where frost is: the preference if set, the bundled copy, the one
    FrioStudio installed into the Terminal, or the one inside the app."""
    if preferences is not None and preferences.frost_path:
        path = bpy.path.abspath(preferences.frost_path)
        if os.path.isfile(path):
            return path
    # A path in the environment wins over the search, for scripts and tests.
    if os.environ.get("FROST_PATH") and os.path.isfile(os.environ["FROST_PATH"]):
        return os.environ["FROST_PATH"]
    bundled = bundled_frost()
    if bundled and repair_executable(bundled):
        return bundled
    for candidate in (shutil.which("frost") or "", "/usr/local/bin/frost",
                      "/Applications/FrioStudio.app/Contents/MacOS/frost"):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return bundled


def machine_is_apple_silicon():
    return platform.system() == "Darwin" and platform.machine() == "arm64"


class FrostPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    frost_path: StringProperty(
        name="Frost executable", subtype='FILE_PATH', default="",
        description="Leave empty to use the copy inside the add-on, or the one FrioStudio installed")

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "frost_path")
        found = find_frost(self)
        if not machine_is_apple_silicon():
            layout.label(text="Frost runs on Apple silicon Macs only.", icon='ERROR')
        elif found and os.access(found, os.X_OK):
            layout.label(text="Using: " + found, icon='CHECKMARK')
        elif found:
            layout.label(text="Frost is here but cannot be run: " + found, icon='ERROR')
        else:
            layout.label(text="Frost was not found. Point this at the frost executable.", icon='ERROR')
        row = layout.row()
        row.operator("frost.repair", text="Repair the bundled frost", icon='FILE_REFRESH')
        row.operator("frost.show_log", text="Show the last render's log", icon='TEXT')
        layout.label(text="If a render shows nothing, the reason is in the render window's header and in the log.")


class FROST_OT_repair(bpy.types.Operator):
    bl_idname = "frost.repair"
    bl_label = "Repair the bundled frost"
    bl_description = "Makes the frost inside the add-on executable again and clears the download quarantine flag"

    def execute(self, context):
        path = bundled_frost()
        if not path:
            self.report({'ERROR'}, "This add-on has no frost inside it; set the path instead")
            return {'CANCELLED'}
        if repair_executable(path):
            self.report({'INFO'}, "frost can run: " + path)
            return {'FINISHED'}
        self.report({'ERROR'}, "frost still cannot be run: " + path)
        return {'CANCELLED'}


class FROST_OT_show_log(bpy.types.Operator):
    bl_idname = "frost.show_log"
    bl_label = "Show the last render's log"
    bl_description = "Opens the log Frost for Blender wrote for the last render"

    def execute(self, context):
        folder = os.path.join(os.path.expanduser("~"), "Library", "Logs", "Frost for Blender")
        path = os.path.join(folder, "last-render.log")
        if not os.path.isfile(path):
            self.report({'INFO'}, "No render has been logged yet")
            return {'CANCELLED'}
        subprocess.run(["open", "-R", path], check=False)
        return {'FINISHED'}


def preferences(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


classes = (FrostSceneSettings, FrostPreferences, FROST_OT_repair, FROST_OT_show_log)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.frost = PointerProperty(type=FrostSceneSettings)


def unregister():
    del bpy.types.Scene.frost
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
