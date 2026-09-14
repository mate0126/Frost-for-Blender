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
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "Render Properties > Render Engine > Frost",
    "description": "Render with Frost, FrioStudio's path tracer, on Apple silicon Macs",
    "category": "Render",
}

import bpy

from . import engine, properties, ui


def register():
    properties.register()
    engine.register()
    ui.register()


def unregister():
    ui.unregister()
    engine.unregister()
    properties.unregister()
