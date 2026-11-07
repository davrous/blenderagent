"""Blob-backed video jobs with snapshot rendering and fail-closed paid approval."""

import asyncio
import contextvars
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from azure.core.exceptions import ResourceExistsError, ResourceModifiedError

from artifact_storage import ArtifactStorage
from media_analysis import render_settings, run_media
from wavespeed_client import RATES, TERMINAL_FAILURES, WaveSpeedClient

logger = logging.getLogger("blender_agent.video")
video_scope = contextvars.ContextVar("video_scope", default=None)
video_notifier = contextvars.ContextVar("video_notifier", default=None)
FINAL_STATES = {"completed", "failed", "cancelled", "submission_unknown", "awaiting_seedance_approval", "awaiting_render_approval"}
TRANSITIONS = {
    "awaiting_render_approval": {"queued", "cancelled"},
    "completed": {"queued"},
    "queued": {"rendering", "failed", "cancelled"},
    "rendering": {"encoding", "failed", "cancelled"},
    "encoding": {"completed", "awaiting_seedance_approval", "failed", "cancelled"},
    "awaiting_seedance_approval": {"wavespeed_uploading", "cancelled"},
    "wavespeed_uploading": {"wavespeed_submitting", "failed", "cancelled"},
    "wavespeed_submitting": {"wavespeed_processing", "submission_unknown"},
    "wavespeed_processing": {"completed", "failed", "cancelled"},
}


def job_name(scope: str, job_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", scope or "") or not re.fullmatch(r"[a-f0-9]{32}", job_id or ""):
        raise ValueError("Invalid conversation or job ID.")
    return f"video-jobs/{scope}/{job_id}.json"


class JobRepository:
    def __init__(self, storage):
        self.storage = storage

    def read(self, scope, job_id):
        document, etag = self.storage.read_json(job_name(scope, job_id))
        if document.get("scope") != scope or document.get("id") != job_id:
            raise ValueError("This job does not belong to the conversation.")
        return document, etag

    def update(self, document, etag, state=None, **changes):
        old_state = document["state"]
        if state is not None and state != old_state and state not in TRANSITIONS.get(old_state, set()):
            raise ValueError(f"Invalid job transition: {old_state} to {state}.")
        if old_state == "completed" and state == "queued" and not document.get("inspection_required"):
            raise ValueError("Only an inspected Blender animation can be rendered again.")
        updated = {**document, **changes, "state": state or old_state, "updated_at": time.time()}
        next_etag = self.storage.write_json(job_name(document["scope"], document["id"]), updated, etag=etag)
        logger.info("Video job %s state=%s", document["id"], updated["state"])
        return updated, next_etag

    def approve(self, scope, job_id, prompt, resolution="720p", generate_audio=False):
        if resolution not in RATES or not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 8000 or type(generate_audio) is not bool:
            raise ValueError("Provide a finishing prompt, valid resolution and audio setting.")
        document, etag = self.read(scope, job_id)
        if document["state"] != "awaiting_seedance_approval":
            return document
        if not os.getenv("WAVESPEED_API_KEY"):
            raise ValueError("Seedance is not configured on this agent.")
        updated, _ = self.update(document, etag, "wavespeed_uploading", approval={
            "prompt": prompt, "resolution": resolution, "generate_audio": generate_audio,
            "approved_at": time.time(), "estimate_usd": round(document["duration_seconds"] * 2 * RATES[resolution], 2),
        })
        return updated


class VideoJobs:
    def __init__(self, storage=None, root=None):
        self.storage = storage or ArtifactStorage()
        self.repo = JobRepository(self.storage)
        self.root = Path(root or Path.home() / "tmp" / "video-jobs")
        self.root.mkdir(parents=True, exist_ok=True)
        self._tasks = {}
        self._guard = threading.Lock()
        self._render_lock = threading.Lock()
        self._notifications = {}
        self._notification_sends = {}

    def resume_pending(self):
        for identity in self.root.glob("*/identity.json"):
            try:
                value = json.loads(identity.read_text())
                job_name(value["scope"], value["id"])
                self.ensure(value["scope"], value["id"])
            except Exception:
                logger.warning("Could not reconcile a persisted video job identity")

    def create(self, scope, settings, snapshot, *, seedance=False, inspect_first=False):
        settings = render_settings(**{key: settings[key] for key in (
            "duration_seconds", "fps", "resolution", "mode", "engine", "samples")})
        if seedance and settings["mode"] != "clay":
            raise ValueError("Render a clay preview before Seedance finishing.")
        if inspect_first and (
            seedance or settings["mode"] not in ("clay", "standard") or settings["engine"] != "BLENDER_EEVEE_NEXT"
            or settings["fps"] != 25 or settings["resolution"] != "480p" or settings["duration_seconds"] not in (5, 10, 15)
        ):
            raise ValueError("Devfest inspection requires 5, 10 or 15 seconds in clay/standard mode at 480p/25 fps.")
        job_id = uuid.uuid4().hex
        job_name(scope, job_id)
        with self._guard:
            if any(task.is_alive() for task in self._tasks.values()):
                raise ValueError("A video job is already running in this session.")
        if shutil.disk_usage(self.root).free < 2 * 1024 ** 3:
            raise ValueError("Video rendering requires at least 2 GiB of free temporary space.")
        directory = self.root / job_id
        directory.mkdir()
        try:
            snapshot(directory / "scene.blend")
            if not (directory / "scene.blend").is_file():
                raise ValueError("Blender did not create the scene snapshot.")
            (directory / "settings.json").write_text(json.dumps(settings))
            document = {"id": job_id, "scope": scope, "state": "awaiting_render_approval" if inspect_first else "queued", "progress": 0,
                        "created_at": time.time(), "updated_at": time.time(), "seedance": seedance, **settings}
            if inspect_first:
                if (directory / "scene.blend").stat().st_size > 200 * 1024 * 1024:
                    raise ValueError("The packed Blender animation exceeds the 200 MiB download limit.")
                document["scene_blob"] = f"videos/{scope}/{job_id}/scene.blend"
                document["inspection_required"] = True
                viewport = directory / "viewport.png"
                if not viewport.is_file() or not 0 < viewport.stat().st_size <= 16 * 1024 * 1024:
                    raise ValueError("Blender did not create a valid viewport preview for the animation.")
                with viewport.open("rb") as image:
                    if image.read(8) != b"\x89PNG\r\n\x1a\n":
                        raise ValueError("The animation viewport preview must be a PNG.")
                document["viewport_blob"] = f"videos/{scope}/{job_id}/viewport.png"
                self.storage.upload_file(document["scene_blob"], directory / "scene.blend", "application/x-blender")
                self.storage.upload_file(document["viewport_blob"], viewport, "image/png")
            self.storage.write_json(job_name(scope, job_id), document)
            (directory / "identity.json").write_text(json.dumps({"scope": scope, "id": job_id}))
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        notifier = video_notifier.get()
        if notifier:
            self._notifications[job_id] = notifier
        if not inspect_first:
            self.ensure(scope, job_id)
        return self.describe(document)

    def render(self, scope, job_id, *, resolution="480p"):
        document, etag = self.repo.read(scope, job_id)
        scene_blob = f"videos/{scope}/{job_id}/scene.blend"
        if (resolution not in ("480p", "1080p") or not document.get("inspection_required")
                or document.get("scene_blob") != scene_blob or document["seedance"]):
            raise ValueError("Render controls require an inspected, conversation-owned Blender animation.")
        state = document["state"]
        if document["resolution"] == resolution and document.get("render_approval"):
            if state in ("queued", "rendering", "encoding", "completed"):
                return self.status(scope, job_id)
        if not (
            (state == "awaiting_render_approval" and resolution == "480p")
            or (state == "completed" and document["resolution"] == "480p" and resolution == "1080p")
        ):
            raise ValueError("Inspect the .blend before rendering 480p, and approve the completed 480p video before 1080p.")
        settings = render_settings(
            document["duration_seconds"], document["fps"], resolution, document["mode"],
            document["engine"], document["samples"], allow_hd=resolution == "1080p",
        )
        required_gib = 4 if resolution == "1080p" else 2
        if shutil.disk_usage(self.root).free < required_gib * 1024 ** 3:
            raise ValueError(f"This render requires at least {required_gib} GiB of free temporary space.")
        self.repo.update(document, etag, "queued", **settings, progress=0,
                         render_approval={"resolution": resolution, "approved_at": time.time()})
        self.ensure(scope, job_id)
        return self.status(scope, job_id)

    def ensure(self, scope, job_id):
        job_name(scope, job_id)
        with self._guard:
            if job_id in self._tasks and self._tasks[job_id].is_alive():
                return
            thread = threading.Thread(target=self._run, args=(scope, job_id), daemon=True, name=f"video-{job_id[:8]}")
            self._tasks[job_id] = thread
            thread.start()

    def status(self, scope, job_id):
        document, _ = self.repo.read(scope, job_id)
        if document["state"] not in FINAL_STATES:
            self.ensure(scope, job_id)
        directory = self.root / job_id
        self._read_progress(document, directory)
        return self.describe(document)

    @staticmethod
    def _read_progress(document, directory):
        if document["state"] == "rendering" and (directory / "progress").exists():
            try:
                with (directory / "progress").open() as progress:
                    document["progress"] = max(0, min(90, int(progress.read(32)) * 90 // document["frames"]))
            except (OSError, ValueError):
                pass

    def _publish(self, document, *, final=False):
        job_id = document["id"]
        notification = self._notifications.get(job_id)
        if not notification:
            return
        loop, callback = notification
        if loop.is_closed() or (not final and not getattr(callback, "live_progress", False)):
            return
        now = time.monotonic()
        fingerprint = (document["state"], document["progress"])
        previous = self._notification_sends.get(job_id)
        if not final and previous and (
            not previous[2].done() or now - previous[0] < 10 or fingerprint == previous[1]
        ):
            return

        async def deliver():
            if previous and not previous[2].done():
                await asyncio.gather(asyncio.wrap_future(previous[2]), return_exceptions=True)
            try:
                value = await asyncio.to_thread(self.describe, dict(document))
                await callback(value)
            except Exception:
                logger.warning("Video notification failed for %s; use Status to retrieve it", job_id)

        snapshot = dict(document)
        document = snapshot
        future = asyncio.run_coroutine_threadsafe(deliver(), loop)
        if final:
            self._notifications.pop(job_id, None)
            self._notification_sends.pop(job_id, None)
        else:
            self._notification_sends[job_id] = (now, fingerprint, future)

    def approve(self, scope, job_id, **settings):
        try:
            self.repo.approve(scope, job_id, **settings)
        except ResourceModifiedError:
            pass
        self.ensure(scope, job_id)
        return self.status(scope, job_id)

    def cancel(self, scope, job_id):
        document, etag = self.repo.read(scope, job_id)
        if "cancelled" not in TRANSITIONS.get(document["state"], set()):
            raise ValueError("This job can no longer be cancelled safely.")
        cancelled, _ = self.repo.update(document, etag, "cancelled")
        directory = self.root / job_id
        directory.mkdir(exist_ok=True)
        (directory / "cancel").touch()
        if document["state"] == "awaiting_render_approval":
            self._finalize(cancelled)
        return self.status(scope, job_id)

    def describe(self, document):
        result = {key: document[key] for key in ("id", "state", "progress", "mode", "duration_seconds", "fps", "resolution")}
        result["seedance_enabled"] = bool(os.getenv("WAVESPEED_API_KEY"))
        result["estimate_usd"] = round(document["duration_seconds"] * 2 * RATES["720p"], 2)
        for field in ("preview", "poster", "output", "viewport"):
            if document.get(field + "_blob"):
                result[field + "_url"] = self.storage.signed_url(document[field + "_blob"])
        if document.get("error"):
            result["error"] = document["error"]
        if document.get("inspection_required"):
            result["inspection_required"] = True
            result["scene_url"] = self.storage.signed_url(document["scene_blob"])
            result["hd_available"] = document["state"] == "completed" and document["resolution"] == "480p"
        return result

    def _run(self, scope, job_id):
        lease = None
        stop = threading.Event()
        lost = threading.Event()
        document = None
        try:
            document, etag = self.repo.read(scope, job_id)
            if document["state"] in FINAL_STATES:
                return
            lock_blob = self.storage.blob(job_name(scope, job_id) + ".lock")
            try:
                lock_blob.upload_blob(b"", overwrite=False)
            except ResourceExistsError:
                pass
            lease = lock_blob.acquire_lease(lease_duration=60)
            def renew():
                while not stop.wait(15):
                    try:
                        lease.renew()
                    except Exception:
                        lost.set()
                        return
            keeper = threading.Thread(target=renew, daemon=True)
            keeper.start()
            document, etag = self.repo.read(scope, job_id)
            directory = self.root / job_id
            directory.mkdir(exist_ok=True)
            if document["state"] in ("queued", "rendering", "encoding"):
                with self._render_lock:
                    document, etag = self._render(document, etag, directory, lost)
            if document["state"] in ("wavespeed_uploading", "wavespeed_submitting", "wavespeed_processing"):
                document, etag = self._finish(document, etag, directory, lost)
            self._finalize(document)
        except ResourceModifiedError:
            logger.info("Video job %s changed concurrently; stopping this worker", job_id)
        except Exception as error:
            logger.warning("Video job %s failed (%s)", job_id, type(error).__name__)
            if document and lease and not lost.is_set():
                try:
                    current, current_etag = self.repo.read(scope, job_id)
                    state = "submission_unknown" if current["state"] == "wavespeed_submitting" else "failed"
                    message = ("Submission outcome is unknown. Reconcile this job in WaveSpeed before creating another paid request."
                               if state == "submission_unknown" else "Video processing failed. Check server diagnostics and media/render limits.")
                    if state in TRANSITIONS.get(current["state"], set()):
                        current, _ = self.repo.update(current, current_etag, state, error=message)
                    self._finalize(current)
                except Exception:
                    logger.warning("Could not persist failure for job %s", job_id)
        finally:
            stop.set()
            if lease:
                try:
                    lease.release()
                except Exception:
                    pass

    def _finalize(self, document):
        if document["state"] not in FINAL_STATES:
            return
        job_id = document["id"]
        directory = self.root / job_id
        shutil.rmtree(directory / "frames", ignore_errors=True)
        (directory / "scene.blend").unlink(missing_ok=True)
        (directory / "preview.mp4").unlink(missing_ok=True)
        (directory / "final.mp4").unlink(missing_ok=True)
        self._publish(document, final=True)

    def _render(self, document, etag, directory, lost):
        if document.get("inspection_required"):
            if (document.get("scene_blob") != f"videos/{document['scope']}/{document['id']}/scene.blend"
                    or document.get("render_approval", {}).get("resolution") != document["resolution"]):
                raise ValueError("The inspected animation has no matching render approval.")
            settings = render_settings(**{key: document[key] for key in (
                "duration_seconds", "fps", "resolution", "mode", "engine", "samples")},
                allow_hd=document["resolution"] == "1080p")
            self.storage.download(document["scene_blob"], directory / "scene.blend")
            (directory / "settings.json").write_text(json.dumps(settings))
            if document["state"] == "queued":
                shutil.rmtree(directory / "frames", ignore_errors=True)
                (directory / "progress").unlink(missing_ok=True)
        if not (directory / "scene.blend").is_file():
            raise ValueError("The saved render snapshot is unavailable; create a new preview.")
        if document["state"] == "queued":
            document, etag = self.repo.update(document, etag, "rendering")
        if document["state"] == "rendering":
            command = [os.getenv("BLENDER_PATH", "blender"), "--background", "--disable-autoexec",
                       str(directory / "scene.blend"), "--threads", "2", "--python-exit-code", "1",
                       "--python", str(Path(__file__).with_name("blender_video.py")), "--", str(directory)]
            with (directory / "render.log").open("wb") as output:
                process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
                deadline = time.monotonic() + int(os.getenv("VIDEO_RENDER_TIMEOUT_SECONDS", "3600"))
                try:
                    while process.poll() is None:
                        if lost.wait(1) or time.monotonic() > deadline or (directory / "cancel").exists():
                            raise ValueError("Render cancelled, lease lost or render deadline exceeded.")
                        self._read_progress(document, directory)
                        self._publish(document)
                    if process.returncode:
                        raise ValueError("Blender snapshot rendering failed.")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
            document, etag = self.repo.update(document, etag, "encoding", progress=90)
            self._publish(document)
        if lost.is_set():
            raise ValueError("Job lease was lost.")
        ffmpeg = os.getenv("FFMPEG_PATH", "ffmpeg")
        run_media([ffmpeg, "-v", "error", "-nostdin", "-y", "-framerate", str(document["fps"]),
                   "-i", str(directory / "frames" / "%06d.png"), "-frames:v", str(document["frames"]),
                   "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                   str(directory / "preview.mp4")], timeout=180)
        if (directory / "preview.mp4").stat().st_size > 200 * 1024 * 1024:
            raise ValueError("Rendered output exceeds 200 MiB.")
        prefix = f"videos/{document['scope']}/{document['id']}"
        output_blob = prefix + (f"/{document['resolution']}.mp4" if document.get("inspection_required") else "/preview.mp4")
        self.storage.upload_file(output_blob, directory / "preview.mp4", "video/mp4")
        self.storage.upload_file(prefix + "/poster.png", directory / "frames" / "000001.png", "image/png")
        state = "awaiting_seedance_approval" if document["seedance"] else "completed"
        changes = {"preview_blob": output_blob, "poster_blob": prefix + "/poster.png", "progress": 100}
        if state == "completed":
            changes["output_blob"] = changes["preview_blob"]
        if document.get("inspection_required") and document["resolution"] == "1080p":
            changes["preview_blob"] = document["preview_blob"]
        return self.repo.update(document, etag, state, **changes)

    def _finish(self, document, etag, directory, lost):
        self._publish(document)
        if document["state"] == "wavespeed_submitting":
            return self.repo.update(document, etag, "submission_unknown", error="A restart interrupted submission. Reconcile in WaveSpeed; this job will not submit again.")
        client = WaveSpeedClient()
        try:
            if document["state"] == "wavespeed_uploading":
                source = directory / "preview.mp4"
                self.storage.download(document["preview_blob"], source)
                video = client.upload(source)
                if lost.is_set():
                    raise ValueError("Job lease was lost.")
                document, etag = self.repo.update(document, etag, "wavespeed_submitting")
                approval = document["approval"]
                prediction = client.submit(video, approval["prompt"], approval["resolution"], approval["generate_audio"])
                document, etag = self.repo.update(document, etag, "wavespeed_processing", prediction_id=prediction,
                                                  provider_deadline=time.time() + 3600)
            delay = 2
            while time.time() < document["provider_deadline"]:
                if lost.wait(delay) or (directory / "cancel").exists():
                    return document, etag
                self._publish(document)
                try:
                    result = client.poll(document["prediction_id"])
                except ValueError:
                    delay = min(10, delay + 2)
                    continue
                state = result.get("status")
                if state in TERMINAL_FAILURES:
                    raise ValueError("WaveSpeed prediction failed.")
                if state == "completed":
                    outputs = result.get("outputs", [])
                    if not outputs or not isinstance(outputs[0], str):
                        raise ValueError("WaveSpeed returned no downloadable video.")
                    target = directory / "final.mp4"
                    client.download(outputs[0], target)
                    probe = json.loads(run_media([
                        os.getenv("FFPROBE_PATH", "ffprobe"), "-v", "error", "-protocol_whitelist", "file,pipe",
                        "-show_entries", "stream=codec_type,width,height:format=format_name,duration", "-of", "json", str(target),
                    ]).stdout)
                    streams = [stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"]
                    if len(streams) != 1 or "mp4" not in probe.get("format", {}).get("format_name", ""):
                        raise ValueError("WaveSpeed output is not an MP4 video.")
                    if not 3.9 <= float(probe["format"].get("duration", 0)) <= 30.1:
                        raise ValueError("WaveSpeed output has an unexpected duration.")
                    blob = f"videos/{document['scope']}/{document['id']}/final.mp4"
                    self.storage.upload_file(blob, target, "video/mp4")
                    return self.repo.update(document, etag, "completed", output_blob=blob, progress=100)
                delay = min(10, delay + 1)
            raise ValueError("WaveSpeed processing exceeded one hour.")
        finally:
            client.close()


_jobs = None
_jobs_lock = threading.Lock()


def get_video_jobs():
    global _jobs
    with _jobs_lock:
        if _jobs is None:
            _jobs = VideoJobs()
    return _jobs


def resume_video_jobs():
    if (Path.home() / "tmp" / "video-jobs").is_dir():
        get_video_jobs().resume_pending()


def descriptor(value: dict) -> str:
    return "```videojob\n" + json.dumps(value) + "\n```"