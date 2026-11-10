"""Offline high-resolution image routing, snapshot, worker and channel contracts."""

import asyncio
import copy
import json
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.core.exceptions import ResourceExistsError, ResourceModifiedError
from blender_image import image_settings, needs_async_image, png_dimensions, render_image_snapshot
from teams_media import MediaError, video_attachments
from video_jobs import JobRepository, VideoJobs, descriptor, job_name, video_notifier, video_scope


def png(width, height):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)


class Storage:
    def __init__(self):
        self.documents = {}
        self.files = {}
        self.uploads = []
        self.leases = []

    def write_json(self, name, value, *, etag=None):
        previous = self.documents.get(name)
        if previous is not None and etag is None:
            raise ResourceExistsError("Exists")
        if etag is not None and (previous is None or previous[1] != etag):
            raise ResourceModifiedError("Stale")
        next_etag = str(int(previous[1]) + 1 if previous else 1)
        self.documents[name] = (copy.deepcopy(value), next_etag)
        return next_etag

    def read_json(self, name):
        return copy.deepcopy(self.documents[name])

    def upload_file(self, name, source, media_type):
        self.files[name] = Path(source).read_bytes()
        self.uploads.append((name, media_type))

    def download(self, name, destination):
        Path(destination).write_bytes(self.files[name])

    def signed_url(self, name):
        return f"https://teststorage.blob.core.windows.net/screenshots/{name}"

    def blob(self, name):
        lease = SimpleNamespace(renew=Mock(), release=Mock())
        self.leases.append(lease)
        return SimpleNamespace(upload_blob=Mock(), acquire_lease=Mock(return_value=lease))


class SettingsTests(unittest.TestCase):
    def test_exact_threshold_and_dimensions(self):
        self.assertFalse(needs_async_image(1920, 1080))
        for width, height in ((1921, 1080), (1920, 1081), (2560, 1440), (3820, 2160), (3840, 2160), (1080, 1920)):
            self.assertTrue(needs_async_image(width, height))
            self.assertEqual(image_settings(width, height, 256, "CYCLES")["resolution"], f"{width}x{height}")
        for width, height, samples, engine in (
            (True, 1080, 16, "CYCLES"), (1920, 0, 16, "CYCLES"), (8193, 10, 16, "CYCLES"),
            (4001, 4000, 16, "CYCLES"), (1920, 1080, 513, "CYCLES"),
            (1920, 1080, True, "CYCLES"), (1920, 1080, 16, "invalid"),
        ):
            with self.assertRaises(ValueError):
                image_settings(width, height, samples, engine)

    def test_high_resolution_tools_submit_snapshot_not_socket_render(self):
        import main
        token = video_scope.set("a" * 32)
        try:
            jobs = SimpleNamespace(create_image=Mock())
            blender = Mock()
            blender.send_command.return_value = {"saved": True}

            def create(scope, width, height, samples, engine, snapshot):
                self.assertEqual(scope, "a" * 32)
                self.assertEqual(samples, 256)
                snapshot(Path("/tmp/scene.blend"))
                return {"id": "b" * 32, "kind": "image", "state": "queued", "width": width, "height": height}

            jobs.create_image.side_effect = create
            with patch.object(main, "get_blender_connection", return_value=blender), patch("video_jobs.get_video_jobs", return_value=jobs):
                for tool in (main.render_final, main.render_preview):
                    for width, height in ((2560, 1440), (3820, 2160), (3840, 2160)):
                        reply = tool(resolution_x=width, resolution_y=height, samples=256)
                        self.assertIn("```imagejob", reply)
                        self.assertIn(f'"width": {width}', reply)
            self.assertEqual(jobs.create_image.call_count, 6)
            self.assertTrue(all(call.args[0] == "snapshot_image" for call in blender.send_command.call_args_list))
        finally:
            video_scope.reset(token)

    def test_1080p_remains_synchronous(self):
        import main
        with tempfile.TemporaryDirectory() as temporary, patch.object(main, "_PERSISTENT_TMP", temporary), \
             patch.object(main, "get_blender_connection") as connection, \
             patch.object(main, "upload_image_to_blob", return_value="https://teststorage.blob.core.windows.net/render.png"), \
             patch("video_jobs.get_video_jobs") as jobs:
            def execute(command, parameters):
                self.assertEqual(command, "execute_code")
                code = parameters["code"]
                self.assertIn("bpy.ops.render.render(write_still=True)", code)
                destination = re.search(r"render.filepath = '([^']+)'", code)[1]
                Path(destination).write_bytes(png(1920, 1080))
                return {}
            connection.return_value.send_command.side_effect = execute
            for samples in (32, 1024):
                reply = main.render_final(resolution_x=1920, resolution_y=1080, samples=samples)
                self.assertIn("Render complete (1920x1080)", reply)
        jobs.assert_not_called()

    def test_high_resolution_without_scope_is_not_silently_rendered(self):
        import main
        token = video_scope.set(None)
        try:
            with patch.object(main, "get_blender_connection") as blender, self.assertRaises(ValueError):
                main.render_final(resolution_x=2560, resolution_y=1440)
            blender.assert_not_called()
        finally:
            video_scope.reset(token)


class ImageWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.storage = Storage()
        self.jobs = VideoJobs(storage=self.storage, root=self.temporary.name)
        self.jobs.ensure = Mock()
        self.scope = "a" * 32

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self, width=2560, height=1440):
        value = self.jobs.create_image(self.scope, width, height, 256, "CYCLES",
                                       lambda path: path.write_bytes(b"frozen-scene"))
        directory = Path(self.temporary.name) / value["id"]
        document, etag = self.jobs.repo.read(self.scope, value["id"])
        return value, directory, document, etag

    def process(self, directory, width=2560, height=1440, returncode=0):
        (directory / "result.png").write_bytes(png(width, height))
        return SimpleNamespace(poll=lambda: returncode, returncode=returncode)

    def test_snapshot_is_durable_and_job_has_no_video_or_paid_metadata(self):
        value, directory, document, etag = self.prepare()
        self.assertEqual(value["kind"], "image")
        self.assertEqual(value["state"], "queued")
        for field in ("duration_seconds", "fps", "seedance_enabled", "estimate_usd"):
            self.assertNotIn(field, value)
        self.assertEqual(self.storage.files[document["scene_blob"]], b"frozen-scene")
        self.assertTrue((directory / "identity.json").is_file())
        self.jobs.ensure.assert_called_once_with(self.scope, value["id"])
        self.assertIn("```imagejob", descriptor(value))
        with self.assertRaisesRegex(ValueError, "paid"):
            self.jobs.repo.approve(self.scope, value["id"], "Must not submit")
        with self.assertRaises(KeyError):
            self.jobs.repo.read("c" * 32, value["id"])

    def test_still_worker_uploads_exact_png_without_ffmpeg_or_live_scene(self):
        value, directory, document, etag = self.prepare()
        with patch("video_jobs.subprocess.Popen", side_effect=lambda *args, **kwargs: self.process(directory)) as process, \
             patch("video_jobs.run_media") as ffmpeg:
            completed, _ = self.jobs._render_image(document, etag, directory, threading.Event())
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["progress"], 100)
        self.assertEqual(self.storage.files[completed["output_blob"]], png(2560, 1440))
        self.assertEqual(process.call_count, 1)
        self.assertIn("--disable-autoexec", process.call_args.args[0])
        self.assertTrue(any("blender_image.py" in arg for arg in process.call_args.args[0]))
        ffmpeg.assert_not_called()
        self.assertTrue(self.jobs.describe(completed)["output_url"].endswith("/image.png"))

    def test_recovery_restores_snapshot_after_local_file_loss(self):
        value, directory, document, etag = self.prepare(3840, 2160)
        (directory / "scene.blend").unlink()
        document, etag = self.jobs.repo.update(document, etag, "rendering")
        with patch("video_jobs.subprocess.Popen", side_effect=lambda *args, **kwargs: self.process(directory, 3840, 2160)):
            completed, _ = self.jobs._render_image(document, etag, directory, threading.Event())
        self.assertEqual((directory / "scene.blend").read_bytes(), b"frozen-scene")
        self.assertEqual(completed["state"], "completed")
        with patch.object(self.jobs, "ensure") as ensure:
            self.jobs.resume_pending()
            ensure.assert_called_once_with(self.scope, value["id"])

    def test_publishing_recovery_does_not_render_twice(self):
        value, directory, document, etag = self.prepare()
        document, etag = self.jobs.repo.update(document, etag, "rendering")
        document, etag = self.jobs.repo.update(document, etag, "publishing", progress=95)
        (directory / "result.png").write_bytes(png(2560, 1440))
        with patch("video_jobs.subprocess.Popen") as process:
            completed, _ = self.jobs._render_image(document, etag, directory, threading.Event())
        process.assert_not_called()
        self.assertEqual(completed["state"], "completed")

    def test_progress_cancel_and_cleanup(self):
        value, directory, document, etag = self.prepare()
        document, etag = self.jobs.repo.update(document, etag, "rendering")
        (directory / "progress").write_text("42")
        self.assertEqual(self.jobs.status(self.scope, value["id"])["progress"], 42)
        (directory / "render.log").write_text("Fra:1 | Sample 128/256\n")
        self.assertEqual(self.jobs.status(self.scope, value["id"])["progress"], 45)
        cancelled = self.jobs.cancel(self.scope, value["id"])
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertTrue((directory / "cancel").is_file())
        (directory / "result.png").write_bytes(png(2560, 1440))
        self.jobs._finalize({**document, "state": "cancelled"})
        self.assertFalse((directory / "result.png").exists())
        self.assertFalse((directory / "scene.blend").exists())

    def test_cancel_stops_only_its_subprocess(self):
        _, directory, document, etag = self.prepare()
        process = Mock(returncode=None)
        process.poll.side_effect = [None, None]
        lost = SimpleNamespace(wait=lambda _: False)
        (directory / "cancel").touch()
        with patch("video_jobs.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(ValueError, "cancelled"):
                self.jobs._render_image(document, etag, directory, lost)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=10)

    def test_timeout_and_exit_code_are_diagnosable_without_retry(self):
        _, directory, document, etag = self.prepare()
        process = Mock(returncode=None)
        process.poll.side_effect = [None, None]
        lost = SimpleNamespace(wait=lambda _: False)
        with patch("video_jobs.subprocess.Popen", return_value=process) as launch, \
             patch.dict(os.environ, {"IMAGE_RENDER_TIMEOUT_SECONDS": "1"}), \
             patch("video_jobs.time.monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(ValueError, "IMAGE_RENDER_TIMEOUT"):
                self.jobs._render_image(document, etag, directory, lost)
        self.assertEqual(launch.call_count, 1)
        process.terminate.assert_called_once()
        document, etag = self.jobs.repo.read(self.scope, document["id"])
        with patch("video_jobs.subprocess.Popen", return_value=SimpleNamespace(poll=lambda: -9, returncode=-9)):
            with self.assertRaisesRegex(ValueError, "code -9"):
                self.jobs._render_image(document, etag, directory, threading.Event())

    def test_wrong_output_dimensions_never_become_completed(self):
        _, directory, document, etag = self.prepare()
        with patch("video_jobs.subprocess.Popen", side_effect=lambda *args, **kwargs: self.process(directory, 640, 480)):
            with self.assertRaisesRegex(ValueError, "dimensions"):
                self.jobs._render_image(document, etag, directory, threading.Event())
        self.assertFalse(any(name.endswith("/image.png") for name in self.storage.files))

    def test_snapshot_failure_releases_creation_guard(self):
        with self.assertRaisesRegex(ValueError, "snapshot failed"):
            self.jobs.create_image(self.scope, 2560, 1440, 32, "CYCLES",
                                   lambda path: (_ for _ in ()).throw(ValueError("snapshot failed")))
        self.assertFalse(self.jobs._creating)
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])
        self.prepare()
        self.jobs._tasks["running"] = SimpleNamespace(is_alive=lambda: True)
        with self.assertRaisesRegex(ValueError, "already running"):
            self.prepare()

    def test_worker_failure_is_persisted_and_lease_released(self):
        value, directory, _, _ = self.prepare()
        with patch("video_jobs.subprocess.Popen", return_value=SimpleNamespace(poll=lambda: -9, returncode=-9)):
            self.jobs._run(self.scope, value["id"])
        document, _ = self.jobs.repo.read(self.scope, value["id"])
        self.assertEqual(document["state"], "failed")
        self.assertIn("Image rendering failed", document["error"])
        self.storage.leases[0].release.assert_called_once()
        self.assertTrue((directory / "render.log").is_file())
        self.assertFalse((directory / "scene.blend").exists())


class BlenderSnapshotTests(unittest.TestCase):
    def test_renderer_uses_requested_size_current_frame_and_sample_progress(self):
        for width, height in ((2560, 1440), (3820, 2160), (3840, 2160)):
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                (directory / "settings.json").write_text(json.dumps(image_settings(width, height, 256, "CYCLES")))
                handlers = []
                scene = SimpleNamespace(camera="Camera", frame_current=73, render=SimpleNamespace(image_settings=SimpleNamespace()),
                                        cycles=SimpleNamespace(), eevee=SimpleNamespace())

                def render(write_still):
                    self.assertTrue(write_still)
                    self.assertEqual(scene.frame_current, 73)
                    self.assertEqual(scene.render.resolution_percentage, 100)
                    handlers[0]("Sample 64/256")
                    self.assertEqual((directory / "progress").read_text(), "22")
                    Path(scene.render.filepath).write_bytes(png(width, height))

                bpy = SimpleNamespace(context=SimpleNamespace(scene=scene),
                                      app=SimpleNamespace(handlers=SimpleNamespace(render_stats=handlers)),
                                      ops=SimpleNamespace(render=SimpleNamespace(render=render)))
                with patch.dict(sys.modules, {"bpy": bpy}):
                    render_image_snapshot(directory)
                self.assertEqual(png_dimensions(directory / "result.png"), (width, height))
                self.assertEqual(handlers, [])
                self.assertEqual(scene.cycles.samples, 256)
                self.assertEqual((directory / "progress").read_text(), "95")


class TeamsImageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scope = "a" * 32
        self.job_id = "b" * 32
        self.job = {"id": self.job_id, "kind": "image", "state": "rendering", "progress": 24,
                    **image_settings(2560, 1440, 256, "CYCLES")}

    def test_image_card_has_status_cancel_and_png_download_without_paid_video_controls(self):
        card = video_attachments(self.job, self.scope, SimpleNamespace)[0].content
        self.assertEqual(card["body"][0]["text"], "Image render")
        self.assertEqual([action["title"] for action in card["actions"]], ["Status", "Cancel"])
        self.job.update(state="completed", progress=100,
                        output_url=f"https://teststorage.blob.core.windows.net/screenshots/images/{self.scope}/{self.job_id}/image.png")
        attachments = video_attachments(self.job, self.scope, SimpleNamespace)
        self.assertEqual(attachments[1].content_type, "image/png")
        self.assertEqual([action["title"] for action in attachments[0].content["actions"]], ["Status", "Download PNG"])
        self.job["output_url"] = self.job["output_url"].replace(self.scope, "c" * 32)
        with self.assertRaises(MediaError):
            video_attachments(self.job, self.scope, SimpleNamespace)

    def test_streamed_imagejob_uses_verified_status_card_not_model_supplied_urls(self):
        from activity_bridge import _GalleryFilter
        stream = _GalleryFilter(scope=self.scope)
        text, cards = stream.feed(descriptor({**self.job, "output_url": "https://evil.example/fake.png"}))
        self.assertFalse(text)
        self.assertEqual((cards[0].scope, cards[0].job_id), (self.scope, self.job_id))
        self.assertNotIn("evil", str(cards[0]))

    async def test_live_card_progress_publishing_and_final_png_are_delivered_once(self):
        import activity_bridge as bridge

        sender = SimpleNamespace(_current=lambda: True, _update_video_card=AsyncMock(return_value=True),
                                 _send=AsyncMock(return_value=True))
        context = SimpleNamespace(activity=SimpleNamespace(channel_id=bridge.Channels.ms_teams,
                                                           is_agentic_request=lambda: False))
        slot = SimpleNamespace(epoch=0, video_cards={})
        with patch.object(bridge, "_ProactiveEmitter", return_value=sender):
            _, notify = bridge._capture_video_notifier(context, None, slot, 0, self.scope)
        await notify(self.job)
        await notify(self.job)
        self.assertEqual(sender._update_video_card.await_count, 1)
        await notify({**self.job, "state": "publishing", "progress": 95})
        self.assertEqual(sender._update_video_card.await_count, 2)
        completed = {**self.job, "state": "completed", "progress": 100,
                     "output_url": f"https://teststorage.blob.core.windows.net/screenshots/images/{self.scope}/{self.job_id}/image.png"}
        await notify(completed)
        await notify(completed)
        self.assertEqual(sender._update_video_card.await_count, 3)
        sender._send.assert_awaited_once()
        self.assertEqual(sender._send.call_args.args[0].attachments[0].content_type, "image/png")
        await notify(self.job)
        self.assertEqual(sender._update_video_card.await_count, 3, "Stale progress cannot replace a completed card")
        slot.epoch = 1
        await notify(completed, refresh=True)
        self.assertEqual(sender._update_video_card.await_count, 3, "/clear invalidates the old notifier")


if __name__ == "__main__":
    unittest.main()
