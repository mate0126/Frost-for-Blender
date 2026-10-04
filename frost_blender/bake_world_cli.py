# Run by a second, headless Blender on a file holding one world:
#
#   blender -b --factory-startup world.blend --python bake_world_cli.py -- \
#           <world name> <stamp> <stem> <film transparent: 0 or 1>
#
# bake.start_world_bake says why. Prints "frost world baked" when the picture
# and its record are in the cache.
import importlib
import os
import sys

import bpy

name, stamp, stem, transparent = sys.argv[sys.argv.index("--") + 1:][:4]
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(here))
bake = importlib.import_module(os.path.basename(here) + ".bake")

world = bpy.data.worlds.get(name) or (bpy.data.worlds[0] if bpy.data.worlds else None)
if world is None:
    print("frost world bake: the file holds no world")
    sys.exit(1)
scene = bpy.data.scenes[0] if bpy.data.scenes else bpy.data.scenes.new("Scene")
scene.world = world
scene.render.film_transparent = transparent == "1"
record = bake.bake_world(scene, bake.world_folder(), stamp=stamp, stem=stem)
if record is None:
    print("frost world bake: nothing to bake")
    sys.exit(1)
print("frost world baked")
