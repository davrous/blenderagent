"""Deterministic Blender camera and snapshot-only animation rendering."""

import json
import os
import sys
from pathlib import Path


def apply_camera_keys(keys: list[dict], fps: int, frames: int, interpolation: str):
    import bpy
    from mathutils import Vector

    scene = bpy.context.scene
    camera = scene.camera
    if camera is None:
        camera = bpy.data.objects.new("VideoCamera", bpy.data.cameras.new("VideoCamera"))
        scene.collection.objects.link(camera)
        scene.camera = camera
    camera.animation_data_clear()
    camera.data.animation_data_clear()
    for constraint in list(camera.constraints):
        camera.constraints.remove(constraint)
    camera.parent = None
    camera.rotation_mode = "QUATERNION"
    previous_rotation = None
    for key in keys:
        camera.location = key["position"]
        rotation = (Vector(key["target"]) - camera.location).to_track_quat("-Z", "Y")
        if previous_rotation is not None and rotation.dot(previous_rotation) < 0:
            rotation.negate()
        camera.rotation_quaternion = rotation
        previous_rotation = rotation.copy()
        camera.data.lens = key["lens"]
        camera.keyframe_insert(data_path="location", frame=key["frame"])
        camera.keyframe_insert(data_path="rotation_quaternion", frame=key["frame"])
        camera.data.keyframe_insert(data_path="lens", frame=key["frame"])
    for animated in (camera, camera.data):
        action = animated.animation_data.action
        for curve in action.fcurves:
            for point in curve.keyframe_points:
                point.interpolation = interpolation
                point.handle_left_type = "AUTO_CLAMPED"
                point.handle_right_type = "AUTO_CLAMPED"
    scene.render.fps = fps
    scene.render.fps_base = 1
    scene.frame_start = 1
    scene.frame_end = frames
    scene.frame_set(1)
    return {"camera": camera.name, "frames": frames, "fps": fps}


def render_snapshot(directory: Path):
    import bpy

    settings = json.loads((directory / "settings.json").read_text())
    scene = bpy.context.scene
    if scene.camera is None:
        raise ValueError("Apply a camera path before rendering.")
    scene.render.engine = "BLENDER_WORKBENCH" if settings["mode"] == "clay" else settings["engine"]
    scene.render.resolution_x = settings["width"]
    scene.render.resolution_y = settings["height"]
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.fps = settings["fps"]
    scene.render.fps_base = 1
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 2
    scene.render.use_compositing = False
    scene.render.use_sequencer = False
    scene.render.use_border = False
    if settings["mode"] == "clay":
        shading = scene.display.shading
        shading.light = "STUDIO"
        shading.color_type = "SINGLE"
        shading.single_color = (0.65, 0.65, 0.65)
        shading.background_type = "WORLD"
        if scene.world is None:
            scene.world = bpy.data.worlds.new("ClayWorld")
        scene.world.color = (0.95, 0.95, 0.95)
        shading.show_shadows = True
        shading.show_cavity = True
        shading.cavity_type = "BOTH"
        scene.render.film_transparent = False
    elif settings["engine"] == "CYCLES":
        scene.cycles.device = "CPU"
        scene.cycles.samples = settings["samples"]
        scene.cycles.use_denoising = True
    else:
        scene.eevee.taa_render_samples = settings["samples"]
    frames = directory / "frames"
    frames.mkdir(exist_ok=True)
    for frame in range(1, settings["frames"] + 1):
        if (directory / "cancel").exists():
            return
        target = frames / f"{frame:06d}.png"
        if not target.exists():
            scene.frame_set(frame)
            scene.render.filepath = str(frames / "pending.png")
            bpy.ops.render.render(write_still=True)
            os.replace(frames / "pending.png", target)
        progress = directory / "progress.tmp"
        progress.write_text(str(frame))
        os.replace(progress, directory / "progress")


if __name__ == "__main__":
    render_snapshot(Path(sys.argv[sys.argv.index("--") + 1]).resolve())