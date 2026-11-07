"""Run in Blender's Python for render verification, or normal Python to encode/probe."""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def render(directory):
    import bpy
    from blender_video import apply_camera_keys, render_snapshot
    from media_analysis import render_settings, validate_camera_path

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.object
    material = bpy.data.materials.new("OriginalRed")
    material.diffuse_color = (0.8, 0.03, 0.03, 1)
    cube.data.materials.append(material)
    bpy.ops.mesh.primitive_plane_add(size=20, location=(0, 0, -1))
    bpy.ops.object.light_add(type="AREA", location=(0, -3, 6))
    bpy.context.object.data.energy = 1200
    bpy.ops.object.camera_add(location=(5, -5, 3))
    bpy.context.scene.camera = bpy.context.object
    keys = validate_camera_path([
        {"time": 0, "position": [5, -5, 3], "target": [0, 0, 0]},
        {"time": 4, "position": [5, -3, 3], "target": [0, 0, 0]},
    ], 4, 12)
    apply_camera_keys(keys, 12, 48, "LINEAR")
    bpy.ops.wm.save_as_mainfile(filepath=str(directory / "original.blend"))
    for mode in ("clay", "standard"):
        bpy.ops.wm.open_mainfile(filepath=str(directory / "original.blend"))
        target = directory / mode
        target.mkdir()
        settings = render_settings(duration_seconds=4, fps=12, mode=mode, samples=4)
        (target / "settings.json").write_text(json.dumps(settings))
        before = [(obj.name, [slot.name if slot else None for slot in obj.data.materials])
                  for obj in bpy.data.objects if obj.type == "MESH"]
        render_snapshot(target)
        after = [(obj.name, [slot.name if slot else None for slot in obj.data.materials])
                 for obj in bpy.data.objects if obj.type == "MESH"]
        assert before == after, "Rendering changed original material slots"
        assert len(list((target / "frames").glob("*.png"))) == 48
        assert (target / "progress").read_text() == "48"
        print(f"PASS {mode}: 48 frames, original materials unchanged", flush=True)
    cancelled = directory / "cancelled"
    cancelled.mkdir()
    (cancelled / "settings.json").write_text(json.dumps(render_settings(duration_seconds=4, fps=12)))
    (cancelled / "cancel").touch()
    render_snapshot(cancelled)
    assert not list((cancelled / "frames").glob("*.png"))
    print("PASS cancellation before first frame", flush=True)


def inspect_animation(directory, duration=15, mode="standard"):
    import bpy
    from blender_startup import BlenderMCPServer
    from blender_video import apply_camera_keys
    from media_analysis import render_settings, validate_camera_path

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.object
    cube_name = cube.name
    cube.location.x = -2
    cube.keyframe_insert(data_path="location", frame=1)
    cube.location.x = 2
    frames = duration * 25
    cube.keyframe_insert(data_path="location", frame=frames)
    image = bpy.data.images.new("InspectionTexture", width=16, height=16)
    image.filepath_raw = str(directory / "texture.png")
    image.file_format = "PNG"
    image.save()
    material = bpy.data.materials.new("InspectionMaterial")
    material.diffuse_color = (0.8, 0.03, 0.03, 1)
    material.use_nodes = True
    texture = material.node_tree.nodes.new("ShaderNodeTexImage")
    texture.image = image
    material.node_tree.links.new(texture.outputs["Color"], material.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
    cube.data.materials.append(material)
    keys = validate_camera_path([
        {"time": 0, "position": [5, -5, 3], "target": [0, 0, 0]},
        {"time": duration, "position": [5, -3, 3], "target": [0, 0, 0]},
    ], duration, 25)
    apply_camera_keys(keys, 25, frames, "LINEAR")
    scene = bpy.context.scene
    scene.render.resolution_x, scene.render.resolution_y = 320, 200
    scene.frame_set(123)
    original_path = bpy.data.filepath
    original_world = scene.world
    original_shading = scene.display.shading.color_type
    server = BlenderMCPServer.__new__(BlenderMCPServer)
    result = server.snapshot_animation(str(directory / "scene.blend"), settings=render_settings(duration, 25, "480p", mode, samples=8))
    assert result["saved"]
    assert (scene.frame_current, scene.render.resolution_x, scene.render.resolution_y) == (123, 320, 200)
    assert bpy.data.filepath == original_path
    assert scene.world == original_world
    assert scene.display.shading.color_type == original_shading
    (directory / "texture.png").unlink()
    bpy.ops.wm.open_mainfile(filepath=str(directory / "scene.blend"))
    scene = bpy.context.scene
    assert (scene.render.fps, scene.frame_start, scene.frame_end, scene.frame_current) == (25, 1, frames, 1)
    assert (scene.render.resolution_x, scene.render.resolution_y) == (854, 480)
    assert scene.eevee.taa_render_samples == 8
    assert scene.render.engine == ("BLENDER_WORKBENCH" if mode == "clay" else "BLENDER_EEVEE_NEXT")
    if mode == "clay":
        assert scene.display.shading.color_type == "SINGLE"
        assert tuple(round(value, 2) for value in scene.display.shading.single_color) == (0.65, 0.65, 0.65)
    assert bpy.data.objects[cube_name].data.materials[0].name == "InspectionMaterial"
    assert bpy.data.images["InspectionTexture"].packed_file is not None
    assert scene.camera.animation_data is not None
    scene.frame_set(1)
    assert bpy.data.objects[cube_name].location.x == -2
    scene.frame_set(frames)
    assert bpy.data.objects[cube_name].location.x == 2
    assert (directory / "viewport.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    preview = bpy.data.images.load(str(directory / "viewport.png"))
    assert 0 < min(preview.size) <= max(preview.size) <= 800
    bpy.data.images.remove(preview)
    print(f"PASS viewport PNG and downloadable .blend: packed assets, camera/object animation, {duration}s/25 fps/480p/{mode}, live scene unchanged", flush=True)


def encode(directory):
    from media_analysis import inspect_media, run_media

    for mode in ("clay", "standard"):
        target = directory / mode
        run_media(["ffmpeg", "-v", "error", "-nostdin", "-y", "-framerate", "12",
                   "-i", str(target / "frames" / "%06d.png"), "-c:v", "libx264", "-threads", "2",
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target / "video.mp4")])
        metadata = inspect_media(target / "video.mp4")
        assert metadata["duration_seconds"] == 4
        probe = json.loads(run_media(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(target / "video.mp4")]).stdout)
        assert probe["streams"][0]["codec_name"] == "h264"
        assert probe["streams"][0]["r_frame_rate"] == "12/1"
        print(f"PASS {mode}: H.264 MP4, 4 seconds, 12 fps", flush=True)


if __name__ == "__main__":
    arguments = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    directory = Path(arguments[1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if arguments[0] == "inspect-all":
        for duration in (5, 10, 15):
            for mode in ("clay", "standard"):
                target = directory / f"{duration}-{mode}"
                target.mkdir()
                inspect_animation(target, duration, mode)
    elif arguments[0] == "inspect":
        inspect_animation(directory, int(arguments[2]) if len(arguments) > 2 else 15, arguments[3] if len(arguments) > 3 else "standard")
    else:
        (render if arguments[0] == "render" else encode)(directory)