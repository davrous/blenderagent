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
    (render if arguments[0] == "render" else encode)(directory)