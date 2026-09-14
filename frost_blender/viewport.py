# The traced viewport: Blender's Rendered shading mode drawn by a frost
# session that stays open. The scene is written out once (and again when
# it changes), frost loads it and waits; every time the view moves the
# add-on sends the new camera down frost's stdin, frost starts the
# accumulation again and writes the picture so far to a file after every
# pass, and the add-on draws the latest one over the viewport.

import math
import os
import shutil
import subprocess
import tempfile
import threading
import time

import bpy
import gpu
import numpy as np
from gpu_extras.presets import draw_texture_2d
from mathutils import Vector

from . import engine as engine_module
from . import export, properties


class Session:
    """One frost process, one exported scene, one frame file."""

    def __init__(self, frost, work):
        self.frost = frost
        self.work = work
        self.frame_path = os.path.join(work, "view.bgra")
        self.process = None
        self.sequence_seen = 0
        self.last_view = None
        self.lock = threading.Lock()

    def start(self, gltf, setup):
        self.stop()
        command = [self.frost, gltf, "--setup", setup, "--interactive", self.frame_path]
        engine_module.log("viewport: " + " ".join(command))
        self.errors = open(os.path.join(self.work, "frost-stderr.log"), "w")
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=self.errors, text=True, bufsize=1)
        self.sequence_seen = 0
        self.last_view = None
        self.views_sent = 0
        self.frames_seen = 0
        self.started_at = time.time()

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def send_view(self, view):
        if not self.alive() or view == self.last_view:
            return
        self.last_view = view
        try:
            self.process.stdin.write("view " + " ".join("%.6g" % v for v in view) + "\n")
            self.process.stdin.flush()
            self.views_sent += 1
            if self.views_sent == 1:
                engine_module.log("viewport: first view %dx%d" % (view[0], view[1]))
        except (OSError, ValueError) as error:
            engine_module.log("viewport: could not send the view: %s" % error)

    def latest_frame(self):
        """(sequence, width, height, rgba float32 rows from the bottom) if a
        newer frame than the last is there, else None."""
        try:
            with open(self.frame_path, "rb") as f:
                data = f.read()
        except OSError:
            return None
        if len(data) < 16 or data[:4] != b"FRSN":
            return None
        w = int.from_bytes(data[4:8], "little")
        h = int.from_bytes(data[8:12], "little")
        sequence = int.from_bytes(data[12:16], "little")
        if sequence == self.sequence_seen or len(data) < 16 + w * h * 4:
            return None
        self.sequence_seen = sequence
        self.frames_seen += 1
        if self.frames_seen == 1:
            engine_module.log("viewport: first frame %dx%d after %.2f s" % (w, h, time.time() - self.started_at))
        bgra = np.frombuffer(data, dtype=np.uint8, count=w * h * 4, offset=16).reshape(h, w, 4)
        rgb = engine_module._SRGB_TO_LINEAR[bgra[::-1, :, 2::-1]]
        rgba = np.concatenate([rgb, np.ones((h, w, 1), dtype=np.float32)], axis=2)
        return sequence, w, h, np.ascontiguousarray(rgba)

    def stop(self):
        if self.process is not None:
            if self.process.poll() is not None:
                engine_module.log("viewport: frost had already exited with %s" % self.process.returncode)
            try:
                if self.process.poll() is None:
                    try:
                        self.process.stdin.write("quit\n")
                        self.process.stdin.flush()
                    except (OSError, ValueError):
                        pass
                    try:
                        self.process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
            finally:
                self.process = None
                try:
                    self.errors.close()
                except Exception:
                    pass

    def close(self):
        self.stop()
        shutil.rmtree(self.work, ignore_errors=True)


def view_of(context, scale):
    """The viewport's camera as frost wants it: width, height, eye, forward,
    up (Frost's axes) and the vertical field of view. None for an
    orthographic view, which Frost does not draw yet."""
    region = context.region
    rv3d = context.region_data
    if region is None or rv3d is None or not rv3d.is_perspective:
        return None
    width = max(int(region.width * scale), 16)
    height = max(int(region.height * scale), 16)
    inverse = rv3d.view_matrix.inverted()
    eye = inverse.translation
    rotation = inverse.to_3x3()
    forward = (rotation @ Vector((0.0, 0.0, -1.0))).normalized()
    up = (rotation @ Vector((0.0, 1.0, 0.0))).normalized()
    m11 = rv3d.window_matrix[1][1]
    fov_y = 2.0 * math.atan(1.0 / m11) if abs(m11) > 1e-6 else math.radians(40.0)
    return (width, height, *export.to_frost(eye), *export.to_frost(forward), *export.to_frost(up), fov_y)


class ViewportDrawing:
    """The latest frame as a texture the size of the region."""

    def __init__(self):
        self.texture = None
        self.size = (0, 0)

    def update(self, width, height, rgba):
        buffer = gpu.types.Buffer('FLOAT', width * height * 4, rgba.reshape(-1))
        self.texture = gpu.types.GPUTexture((width, height), format='RGBA16F', data=buffer)
        self.size = (width, height)

    def draw(self, region_width, region_height):
        if self.texture is None:
            return
        draw_texture_2d(self.texture, (0, 0), region_width, region_height)


def start_session(engine, context, depsgraph):
    """Exports the scene and starts frost; called from view_update."""
    frost = properties.find_frost(properties.preferences(context))
    if not frost:
        return None
    scene = depsgraph.scene_eval
    work = tempfile.mkdtemp(prefix="frost-view-")
    settings = scene.frost
    try:
        gltf, setup, warnings = export.export_scene(depsgraph, scene, work, 64, 64, settings)
    except Exception as error:
        engine_module.log("viewport export failed: %s" % error)
        shutil.rmtree(work, ignore_errors=True)
        return None
    for warning in warnings:
        engine_module.log("viewport warning: " + warning)
    session = Session(frost, work)
    session.start(gltf, setup)
    return session


def redraw_when_frames_arrive(engine_ref, session):
    """A timer that wakes the viewport each time frost has a newer frame,
    and stops when the engine is gone."""
    def poll():
        engine = engine_ref()
        if engine is None or session.process is None:
            return None
        try:
            mtime = os.path.getmtime(session.frame_path)
        except OSError:
            mtime = 0.0
        if mtime != poll.last_mtime:
            poll.last_mtime = mtime
            engine.tag_redraw()
        return 0.05
    poll.last_mtime = 0.0
    bpy.app.timers.register(poll, first_interval=0.1)
