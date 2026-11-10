"""Bounded still-image settings and isolated Blender snapshot rendering."""

import json
import os
import re
import struct
import sys
from pathlib import Path

MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_SIDE = 8192
SAMPLE_PROGRESS = re.compile(r"(?:Rendered|Sample|Rendering)\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)


def image_settings(width: int, height: int, samples: int, engine: str) -> dict:
    if (type(width) is not int or type(height) is not int or min(width, height) < 1
            or max(width, height) > MAX_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS):
        raise ValueError("Still images require positive dimensions, at most 16 megapixels and 8192 pixels per side.")
    if type(samples) is not int or not 1 <= samples <= 512:
        raise ValueError("Still image samples must be between 1 and 512.")
    if engine not in ("CYCLES", "BLENDER_EEVEE_NEXT"):
        raise ValueError("Still images require CYCLES or BLENDER_EEVEE_NEXT.")
    return {"kind": "image", "width": width, "height": height, "samples": samples,
            "engine": engine, "mode": "standard", "resolution": f"{width}x{height}"}


def needs_async_image(width: int, height: int) -> bool:
    return width > 1920 or height > 1080


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as source:
        header = source.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError("Blender did not produce a valid PNG image.")
    return struct.unpack(">II", header[16:24])


def write_progress(directory: Path, value: int) -> None:
    temporary = directory / "progress.tmp"
    temporary.write_text(str(value))
    os.replace(temporary, directory / "progress")


def sample_progress(text: str) -> int | None:
    matches = SAMPLE_PROGRESS.findall(text)
    if not matches or int(matches[-1][1]) <= 0:
        return None
    done, total = (int(value) for value in matches[-1])
    return max(0, min(90, done * 90 // total))


def render_image_snapshot(directory: Path) -> None:
    import bpy

    raw = json.loads((directory / "settings.json").read_text())
    settings = image_settings(raw["width"], raw["height"], raw["samples"], raw["engine"])
    scene = bpy.context.scene
    if scene.camera is None:
        raise ValueError("The saved scene has no active camera.")
    scene.render.engine = settings["engine"]
    scene.render.resolution_x = settings["width"]
    scene.render.resolution_y = settings["height"]
    scene.render.resolution_percentage = 100
    scene.render.use_border = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 2
    scene.render.filepath = str(directory / "pending.png")
    if settings["engine"] == "CYCLES":
        scene.cycles.device = "CPU"
        scene.cycles.samples = settings["samples"]
    else:
        scene.eevee.taa_render_samples = settings["samples"]
    progress = 1

    def report(*args):
        nonlocal progress
        reported = sample_progress(" ".join(str(arg) for arg in args))
        if reported is not None:
            value = max(progress, reported)
            if value != progress:
                progress = value
                write_progress(directory, progress)

    bpy.app.handlers.render_stats.append(report)
    try:
        write_progress(directory, progress)
        bpy.ops.render.render(write_still=True)
        result = directory / "pending.png"
        if png_dimensions(result) != (settings["width"], settings["height"]):
            raise ValueError("Rendered image dimensions do not match the requested size.")
        os.replace(result, directory / "result.png")
        write_progress(directory, 95)
    finally:
        bpy.app.handlers.render_stats.remove(report)


if __name__ == "__main__":
    render_image_snapshot(Path(sys.argv[sys.argv.index("--") + 1]).resolve())
