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


def log_path():
    folder = os.path.join(os.path.expanduser("~"), "Library", "Logs", "Frost for Blender")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "last-render.log")


def log_start():
    try:
        with open(log_path(), "w") as f:
            f.write("Frost for Blender render log\n")
    except OSError:
        pass


def log(line):
    try:
        with open(log_path(), "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


class FrostRenderEngine(bpy.types.RenderEngine):
    bl_idname = "FROST"
    bl_label = "Frost"
    bl_use_preview = False
    bl_use_shading_nodes_custom = False
    bl_use_eevee_viewport = True

    def render(self, depsgraph):
        try:
            self.render_or_raise(depsgraph)
        except Exception as error:   # shown in the render window, not lost in the console
            message = "Frost: %s" % error
            self.error_set(message)
            self.report({'ERROR'}, message + " (see " + log_path() + ")")
            log("failed: %s" % error)
            raise

    def render_or_raise(self, depsgraph):
        scene = depsgraph.scene_eval
        scale = scene.render.resolution_percentage / 100.0
        width = max(int(scene.render.resolution_x * scale), 8)
        height = max(int(scene.render.resolution_y * scale), 8)
        log_start()

        if not properties.machine_is_apple_silicon():
            raise RuntimeError("Frost runs on Apple silicon Macs only")
        frost = properties.find_frost(properties.preferences())
        if not frost:
            raise RuntimeError("the frost executable was not found; set its path in the add-on's preferences")
        if not os.access(frost, os.X_OK):
            raise RuntimeError("the frost executable at %s cannot be run" % frost)

        work = tempfile.mkdtemp(prefix="frost-")
        try:
            self.update_stats("Frost", "Writing the scene")
            gltf, setup, warnings = export.export_scene(depsgraph, scene, work, width, height, scene.frost)
            for warning in warnings:
                self.report({'WARNING'}, warning)
                log("warning: " + warning)
            if self.test_break():
                return

            out = os.path.join(work, "frame.png")
            command = [frost, gltf, "--setup", setup, "--out", out]
            log("running: " + " ".join(command))
            self.update_stats("Frost", "Rendering")
            try:
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, bufsize=1)
            except OSError as error:
                raise RuntimeError("could not start frost (%s). macOS may have refused it: open the add-on's "
                                   "preferences and press Repair, or set the path to FrioStudio's frost" % error)
            progress = re.compile(r"frame \d+: (\d+)%")
            tail = []
            for line in process.stdout:
                log("frost: " + line.rstrip())
                tail.append(line.rstrip())
                tail = tail[-20:]
                match = progress.search(line)
                if match:
                    self.update_progress(int(match.group(1)) / 100.0)
                if self.test_break():
                    process.kill()
                    process.wait()
                    log("cancelled")
                    return
            process.wait()
            log("frost exited with %d" % process.returncode)
            if process.returncode == -9 or process.returncode == 137:
                raise RuntimeError("macOS stopped frost before it could run. Open the add-on's preferences and "
                                   "press Repair, then render again")
            if process.returncode != 0 or not os.path.isfile(out):
                raise RuntimeError("frost did not render: " + (" / ".join(tail[-3:]) or "no output"))

            # Blender's render result loads EXR files and nothing else, so the
            # frame is decoded here and handed to the Combined pass as
            # scene-linear floats, rows from the bottom as Blender keeps them.
            self.update_stats("Frost", "Loading the frame")
            frame_width, frame_height, rgb = png.read_png(out)
            if (frame_width, frame_height) != (width, height):
                raise RuntimeError("frost's frame is %dx%d, not %dx%d" % (frame_width, frame_height, width, height))
            linear = png.srgb_to_linear(rgb[::-1])
            rgba = np.concatenate([linear, np.ones((height, width, 1), dtype=np.float32)], axis=2)
            result = self.begin_result(0, 0, width, height)
            try:
                combined = result.layers[0].passes["Combined"]
                combined.rect.foreach_set(rgba.reshape(-1).astype(np.float32))
            finally:
                self.end_result(result)
            self.update_progress(1.0)
            log("done")
        finally:
            shutil.rmtree(work, ignore_errors=True)


def register():
    bpy.utils.register_class(FrostRenderEngine)


def unregister():
    bpy.utils.unregister_class(FrostRenderEngine)
