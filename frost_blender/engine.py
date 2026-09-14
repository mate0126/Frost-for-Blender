# The render engine: Blender hands over the evaluated scene, this writes it
# out, runs frost, follows its progress, and puts the picture in the render
# window. A render is a subprocess, so cancelling is killing it.

import os
import re
import shutil
import subprocess
import tempfile

import bpy
import numpy as np

from . import export, png, properties


class FrostRenderEngine(bpy.types.RenderEngine):
    bl_idname = "FROST"
    bl_label = "Frost"
    bl_use_preview = False
    bl_use_shading_nodes_custom = False
    bl_use_eevee_viewport = True

    def render(self, depsgraph):
        scene = depsgraph.scene_eval
        scale = scene.render.resolution_percentage / 100.0
        width = max(int(scene.render.resolution_x * scale), 8)
        height = max(int(scene.render.resolution_y * scale), 8)

        frost = properties.find_frost(properties.preferences())
        if not frost:
            self.report({'ERROR'}, "Frost was not found. Set the path in the add-on's preferences.")
            return
        if not properties.machine_is_apple_silicon():
            self.report({'ERROR'}, "Frost runs on Apple silicon Macs only.")
            return

        work = tempfile.mkdtemp(prefix="frost-")
        try:
            self.update_stats("Frost", "Writing the scene")
            gltf, setup, warnings = export.export_scene(depsgraph, scene, work, width, height, scene.frost)
            for warning in warnings:
                self.report({'WARNING'}, warning)
            if self.test_break():
                return

            out = os.path.join(work, "frame.png")
            command = [frost, gltf, "--setup", setup, "--out", out]
            self.update_stats("Frost", "Rendering")
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, bufsize=1)
            progress = re.compile(r"frame \d+: (\d+)%")
            tail = []
            for line in process.stdout:
                tail.append(line.rstrip())
                tail = tail[-20:]
                match = progress.search(line)
                if match:
                    self.update_progress(int(match.group(1)) / 100.0)
                if self.test_break():
                    process.kill()
                    process.wait()
                    return
            process.wait()
            if process.returncode != 0 or not os.path.isfile(out):
                self.report({'ERROR'}, "Frost did not render: " + (" / ".join(tail[-3:]) or "no output"))
                return

            # Blender's render result loads EXR files and nothing else, so the
            # frame is decoded here and handed to the Combined pass as
            # scene-linear floats, rows from the bottom as Blender keeps them.
            self.update_stats("Frost", "Loading the frame")
            frame_width, frame_height, rgb = png.read_png(out)
            if (frame_width, frame_height) != (width, height):
                self.report({'ERROR'}, "Frost's frame is %dx%d, not %dx%d" % (frame_width, frame_height, width, height))
                return
            linear = png.srgb_to_linear(rgb[::-1])
            rgba = np.concatenate([linear, np.ones((height, width, 1), dtype=np.float32)], axis=2)
            result = self.begin_result(0, 0, width, height)
            try:
                combined = result.layers[0].passes["Combined"]
                combined.rect.foreach_set(rgba.reshape(-1).astype(np.float32))
            finally:
                self.end_result(result)
            self.update_progress(1.0)
        finally:
            shutil.rmtree(work, ignore_errors=True)


def register():
    bpy.utils.register_class(FrostRenderEngine)


def unregister():
    bpy.utils.unregister_class(FrostRenderEngine)
