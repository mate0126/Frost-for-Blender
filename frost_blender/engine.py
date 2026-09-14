# The render engine: Blender hands over the evaluated scene, this writes it
# out, runs frost, follows its progress, and puts the picture in the render
# window. A render is a subprocess, so cancelling is killing it.

import os
import re
import shutil
import subprocess
import tempfile
import time

import weakref

import bpy
import gpu
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


# 8-bit sRGB to scene-linear, as a table: a snapshot arrives twice a second.
_SRGB_TO_LINEAR = png.srgb_to_linear(np.arange(256, dtype=np.uint8)).astype(np.float32)


def read_snapshot(path, width, height):
    """frost's picture so far: a 16-byte header (FRSN, width, height,
    sequence) and BGRA rows from the top. Returns the Combined pass's
    floats, rows from the bottom, or None if the file is not there yet."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if len(data) < 16 or data[:4] != b"FRSN":
        return None
    w, h = int.from_bytes(data[4:8], "little"), int.from_bytes(data[8:12], "little")
    if (w, h) != (width, height) or len(data) < 16 + w * h * 4:
        return None
    bgra = np.frombuffer(data, dtype=np.uint8, count=w * h * 4, offset=16).reshape(h, w, 4)
    rgb = _SRGB_TO_LINEAR[bgra[::-1, :, 2::-1]]
    rgba = np.concatenate([rgb, np.ones((h, w, 1), dtype=np.float32)], axis=2)
    return rgba.reshape(-1)


class FrostRenderEngine(bpy.types.RenderEngine):
    bl_idname = "FROST"
    bl_label = "Frost"
    bl_use_preview = False
    bl_use_shading_nodes_custom = False
    bl_use_eevee_viewport = False   # the Rendered viewport mode is Frost's own

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = None
        self.drawing = None
        self.restart_due = None

    def __del__(self):
        session = getattr(self, "session", None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    # ---- the traced viewport ----------------------------------------------

    def view_update(self, context, depsgraph):
        """The scene changed, or the viewport just went to Rendered. The
        first time, the scene is written out and the frost session started
        here and now; a later change only asks for a restart, which the
        timer makes a moment after the last change in a run of them -- a
        drag is dozens of updates a second, and an export for each was a
        viewport that stuttered for the length of the drag."""
        from . import viewport
        if self.session is None or not self.session.alive():
            self.session = viewport.start_session(self, context, depsgraph)
            if self.session is not None:
                try:
                    reference = weakref.ref(self)
                except TypeError:
                    reference = lambda engine=self: engine
                viewport.redraw_when_frames_arrive(reference, self.session)
            self.tag_redraw()
            return
        for update in depsgraph.updates:
            if update.is_updated_geometry or update.is_updated_transform or update.is_updated_shading:
                if not isinstance(update.id, (bpy.types.Scene, bpy.types.Screen, bpy.types.WindowManager)):
                    self.restart_due = time.time() + 0.3
                    break

    def restart_session(self):
        """Called by the timer once a run of changes has gone quiet."""
        from . import viewport
        self.restart_due = None
        old = self.session
        self.session = viewport.start_session(self, bpy.context, bpy.context.evaluated_depsgraph_get())
        if old is not None:
            old.close()
        if self.session is not None:
            try:
                reference = weakref.ref(self)
            except TypeError:
                reference = lambda engine=self: engine
            viewport.redraw_when_frames_arrive(reference, self.session)
        self.tag_redraw()

    def view_draw(self, context, depsgraph):
        """Called for every redraw: send the view if it moved, and draw the
        latest frame frost has written."""
        try:
            self.view_draw_or_raise(context, depsgraph)
        except Exception as error:
            log("viewport draw failed: %r" % (error,))
            raise

    def view_draw_or_raise(self, context, depsgraph):
        from . import viewport
        if self.session is None:
            return
        scene = depsgraph.scene_eval
        view = viewport.view_of(context, scene.frost.viewport_scale)
        if view is not None:
            self.session.send_view(view)
        frame = self.session.latest_frame()
        if self.drawing is None:
            self.drawing = viewport.ViewportDrawing()
        if frame is not None:
            sequence, width, height, rgba = frame
            self.drawing.update(width, height, rgba)
        gpu.state.blend_set('ALPHA_PREMULT')
        self.bind_display_space_shader(scene)
        self.drawing.draw(context.region.width, context.region.height)
        self.unbind_display_space_shader()
        gpu.state.blend_set('NONE')

    # ---- the finished frame -----------------------------------------------

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
            snapshot_path = os.path.join(work, "progress.bgra")
            command = [frost, gltf, "--setup", setup, "--out", out, "--progress", snapshot_path]
            log("running: " + " ".join(command))
            self.update_stats("Frost", "Rendering")
            try:
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, bufsize=1)
            except OSError as error:
                raise RuntimeError("could not start frost (%s). macOS may have refused it: open the add-on's "
                                   "preferences and press Repair, or set the path to FrioStudio's frost" % error)
            progress = re.compile(r"frame \d+: (\d+)%")
            snapshot = re.compile(r"^snapshot (\d+)/(\d+)")
            tail = []
            # The render window fills in as the buckets finish: frost writes
            # the picture so far every half second and says so, and each one
            # goes into the result while the render goes on.
            result = self.begin_result(0, 0, width, height)
            combined = result.layers[0].passes["Combined"]
            snapshots = 0
            try:
                for line in process.stdout:
                    tail.append(line.rstrip())
                    tail = tail[-20:]
                    if line.startswith("snapshot final"):
                        continue
                    match = snapshot.match(line)
                    if match:
                        done, count = int(match.group(1)), int(match.group(2))
                        self.update_stats("Frost", "%d of %d buckets" % (done, count))
                        self.update_progress(done / max(count, 1))
                        pixels = read_snapshot(snapshot_path, width, height)
                        if pixels is not None:
                            combined.rect.foreach_set(pixels)
                            self.update_result(result)
                            snapshots += 1
                        continue
                    log("frost: " + line.rstrip())
                    match = progress.search(line)
                    if match:
                        self.update_progress(int(match.group(1)) / 100.0)
                    if self.test_break():
                        process.kill()
                        process.wait()
                        log("cancelled")
                        return
                process.wait()
                log("%d snapshots shown while rendering" % snapshots)
            finally:
                self.end_result(result)
            log("frost exited with %d" % process.returncode)
            if process.returncode == -9 or process.returncode == 137:
                raise RuntimeError("macOS stopped frost before it could run. Open the add-on's preferences and "
                                   "press Repair, then render again")
            if process.returncode != 0 or not os.path.isfile(out):
                raise RuntimeError("frost did not render: " + (" / ".join(tail[-3:]) or "no output"))

            # Blender's render result loads EXR files and nothing else. The
            # finished frame is in the progress file, denoised and through the
            # lens, read the same cheap way as the snapshots; the PNG is decoded
            # only if that is somehow not there.
            self.update_stats("Frost", "Loading the frame")
            pixels = read_snapshot(snapshot_path, width, height)
            if pixels is None:
                frame_width, frame_height, rgb = png.read_png(out)
                if (frame_width, frame_height) != (width, height):
                    raise RuntimeError("frost's frame is %dx%d, not %dx%d" % (frame_width, frame_height, width, height))
                linear = png.srgb_to_linear(rgb[::-1])
                pixels = np.concatenate([linear, np.ones((height, width, 1), dtype=np.float32)], axis=2).reshape(-1)
            final = self.begin_result(0, 0, width, height)
            try:
                final.layers[0].passes["Combined"].rect.foreach_set(pixels.astype(np.float32))
            finally:
                self.end_result(final)
            self.update_progress(1.0)
            log("done")
        finally:
            shutil.rmtree(work, ignore_errors=True)


def register():
    bpy.utils.register_class(FrostRenderEngine)


def unregister():
    bpy.utils.unregister_class(FrostRenderEngine)
