# Frost for Blender: FrioStudio's path tracer as a Blender render engine.
#
# Press F12 (or Render > Render Image) with Frost chosen as the render
# engine and the scene is handed to the `frost` executable -- the
# geometry with its materials, the camera, the lights and the world -- and
# the finished frame comes back into Blender's render window.
#
# What this needs: an Apple silicon Mac and the frost executable, which
# ships inside this add-on (bin/frost) and also inside FrioStudio.

bl_info = {
    "name": "Frost Render Engine",
    "author": "Mate Szollos",
    "version": (1, 1, 9),
    "blender": (4, 2, 0),
    "location": "Render Properties > Render Engine > Frost",
    "description": "Render with Frost, FrioStudio's path tracer, on Apple silicon Macs",
    "category": "Render",
}

# Installed over a running Blender, this file is read again and nothing else
# is: Blender reloads an add-on's top module when it changes on disk, and a
# plain `from . import engine` then hands back the module already in memory.
# Every update put in without quitting Blender went on running the version
# before it -- the new frost on disk, driven by the old code -- and a release
# that fixed a render looked, to the person who installed it, like a release
# that fixed nothing. So the modules are read again here, in the order they
# lean on each other, before anything is taken from them.
MODULES = ("properties", "png", "export", "bake", "engine", "viewport", "ui")

if "bpy" in locals():
    import importlib
    import sys

    for _name in MODULES:
        _module = sys.modules.get(__name__ + "." + _name)
        if _module is not None:
            importlib.reload(_module)

import bpy

from . import bake, engine, properties, ui


def code_is_current():
    """Whether the modules in memory are the ones this file belongs to. Each
    carries the version it was written for; one that says otherwise is a
    module Blender is still holding from before an update."""
    import sys
    for name in MODULES:
        module = sys.modules.get(__name__ + "." + name)
        if module is not None and getattr(module, "ADDON_VERSION", None) != bl_info["version"]:
            return False
    return True


def _say_restart():
    def draw(self, context):
        self.layout.label(text="Frost was updated while Blender was running,")
        self.layout.label(text="and part of the old version is still loaded.")
        self.layout.label(text="Quit and reopen Blender to finish the update.")
    try:
        bpy.context.window_manager.popup_menu(draw, title="Frost for Blender", icon='ERROR')
    except Exception:
        pass
    return None


def register():
    properties.register()
    engine.register()
    bake.register()
    ui.register()
    if not code_is_current():
        # It should not happen now that the modules are read again above, and
        # if it does the one thing worse than old code is old code in silence.
        print("Frost for Blender: old modules are still loaded; restart Blender to finish the update")
        try:
            bpy.app.timers.register(_say_restart, first_interval=1.0)
        except Exception:
            pass


def unregister():
    ui.unregister()
    bake.unregister()
    engine.unregister()
    properties.unregister()
