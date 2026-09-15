"""Bounded decoded reference media and deterministic camera contracts."""

import json
import math
import os
import subprocess
from pathlib import Path

MAX_MEDIA_BYTES = 200 * 1024 * 1024
RESOLUTIONS = {"480p": (854, 480), "720p": (1280, 720)}


def run_media(command: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise ValueError("Install ffmpeg and ffprobe to process reference media.") from None
    except subprocess.TimeoutExpired:
        raise ValueError("Media processing exceeded its time budget.") from None
    if result.returncode:
        raise ValueError("The media could not be decoded.")
    return result


def inspect_media(path: Path) -> dict:
    if not 0 < path.stat().st_size <= MAX_MEDIA_BYTES:
        raise ValueError("Media must be between 1 byte and 200 MiB.")
    with path.open("rb") as source:
        header = source.read(32)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        media_type = "image/png"
    elif header.startswith(b"\xff\xd8\xff"):
        media_type = "image/jpeg"
    elif header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        media_type = "image/webp"
    elif header[4:8] == b"ftyp" and header[8:12] not in (b"avif", b"avis", b"heic", b"heix"):
        media_type = "video/mp4"
    else:
        raise ValueError("Use a PNG, JPEG, WebP image or an MP4 video.")
    probe = run_media([
        os.getenv("FFPROBE_PATH", "ffprobe"), "-v", "error", "-protocol_whitelist", "file,pipe",
        "-show_streams", "-show_format", "-of", "json", str(path),
    ])
    metadata = json.loads(probe.stdout)
    streams = [stream for stream in metadata.get("streams", []) if stream.get("codec_type") == "video"]
    if len(streams) != 1:
        raise ValueError("Media must contain exactly one visual stream.")
    stream = streams[0]
    width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
    if min(width, height) <= 0 or width * height > 16_000_000 or max(width, height) > 8192:
        raise ValueError("Images must be at most 16 megapixels and 8192 pixels per side.")
    duration = 0.0
    if media_type == "video/mp4":
        if "mp4" not in metadata.get("format", {}).get("format_name", ""):
            raise ValueError("The video must use the MP4 container.")
        duration = float(stream.get("duration", metadata.get("format", {}).get("duration", 0)))
        if not math.isfinite(duration) or not 4 <= duration <= 30:
            raise ValueError("Reference videos must be between 4 and 30 seconds.")
        if width * height > 1920 * 1080:
            raise ValueError("Reference videos must be at most 1080p.")
    run_media([
        os.getenv("FFMPEG_PATH", "ffmpeg"), "-v", "error", "-xerror", "-nostdin", "-threads", "1",
        "-protocol_whitelist", "file,pipe", "-i", str(path), "-map", "0:v:0", "-an",
        *([] if duration else ["-frames:v", "1"]), "-f", "null", "-",
    ], timeout=90)
    return {"media_type": media_type, "width": width, "height": height, "duration_seconds": duration}


def sample_reference(path: Path, directory: Path) -> tuple[dict, list[Path]]:
    metadata = inspect_media(path)
    directory.mkdir(parents=True, exist_ok=True)
    count = 6 if metadata["duration_seconds"] else 1
    frames = []
    for index in range(count):
        target = directory / f"reference-{index}.jpg"
        timestamp = metadata["duration_seconds"] * index / count
        run_media([
            os.getenv("FFMPEG_PATH", "ffmpeg"), "-v", "error", "-nostdin", "-y", "-threads", "1",
            "-protocol_whitelist", "file,pipe", "-ss", str(timestamp), "-i", str(path),
            "-frames:v", "1", "-vf", "scale=1024:1024:force_original_aspect_ratio=decrease",
            "-q:v", "3", str(target),
        ])
        frames.append(target)
    return metadata, frames


def render_settings(duration_seconds=5, fps=24, resolution="480p", mode="clay", engine="BLENDER_EEVEE_NEXT", samples=16) -> dict:
    if type(duration_seconds) not in (int, float) or not math.isfinite(duration_seconds) or not 4 <= duration_seconds <= 30:
        raise ValueError("Animation duration must be between 4 and 30 seconds.")
    if type(fps) is not int or fps not in (12, 24, 30):
        raise ValueError("Use 12, 24 or 30 fps.")
    if resolution not in RESOLUTIONS or mode not in ("clay", "standard"):
        raise ValueError("Use clay/standard mode and 480p/720p resolution.")
    if engine not in ("BLENDER_EEVEE_NEXT", "CYCLES") or type(samples) is not int or not 1 <= samples <= 32:
        raise ValueError("Use Eevee or Cycles with 1-32 samples.")
    frames = round(duration_seconds * fps)
    width, height = RESOLUTIONS[resolution]
    budget = 80_000_000 if mode == "standard" and engine == "CYCLES" else 400_000_000
    if frames * width * height > budget:
        raise ValueError("Animation exceeds this container's render budget; lower fps, duration or resolution.")
    return dict(duration_seconds=frames / fps, fps=fps, resolution=resolution, mode=mode,
                engine=engine, samples=samples, frames=frames, width=width, height=height)


def validate_camera_path(keyframes: list[dict], duration_seconds: float, fps: int = 24, interpolation="LINEAR") -> list[dict]:
    render_settings(duration_seconds, fps)
    if interpolation not in ("LINEAR", "BEZIER") or not isinstance(keyframes, list) or not 2 <= len(keyframes) <= 64:
        raise ValueError("Provide 2-64 camera keys with LINEAR or BEZIER interpolation.")
    result = []
    previous = 0
    for key in keyframes:
        timestamp = key.get("time")
        lens = key.get("lens", 50)
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or not 0 <= timestamp <= duration_seconds:
            raise ValueError("Camera times must be finite and within the animation.")
        if type(lens) not in (int, float) or not math.isfinite(lens) or not 10 <= lens <= 200:
            raise ValueError("Camera lens must be between 10 and 200 mm.")
        vectors = []
        for field in ("position", "target"):
            vector = key.get(field)
            if not isinstance(vector, list) or len(vector) != 3 or any(
                type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 10000 for value in vector
            ):
                raise ValueError("Camera positions and targets require three finite coordinates within 10000 units.")
            vectors.append(vector)
        if sum((left - right) ** 2 for left, right in zip(*vectors)) < 0.000001:
            raise ValueError("Camera position and target must differ.")
        frame = min(round(timestamp * fps) + 1, round(duration_seconds * fps))
        if frame <= previous:
            raise ValueError("Camera keys must map to strictly increasing frames.")
        previous = frame
        result.append(dict(frame=frame, position=vectors[0], target=vectors[1], lens=lens))
    if result[0]["frame"] != 1 or result[-1]["frame"] != round(duration_seconds * fps):
        raise ValueError("Camera keys must cover the entire animation, from time 0 to its duration.")
    return result