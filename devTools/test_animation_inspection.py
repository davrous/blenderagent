"""Offline inspect-before-render and native HD controls; no Azure or Blender calls."""

import copy
import base64
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.core.exceptions import ResourceExistsError, ResourceModifiedError
from media_analysis import render_settings, validate_camera_path
from music import MusicCandidate, MusicRequest, music_instructions
from video_jobs import VideoJobs


class MemoryStorage:
    def __init__(self):
        self.manifests = {}
        self.files = {}

    def write_json(self, name, value, *, etag=None):
        previous = self.manifests.get(name)
        if previous and etag is None:
            raise ResourceExistsError("Exists")
        if etag is not None and (not previous or previous[1] != etag):
            raise ResourceModifiedError("Stale manifest")
        version = str(int(previous[1]) + 1 if previous else 1)
        self.manifests[name] = (copy.deepcopy(value), version)
        return version

    def read_json(self, name):
        return copy.deepcopy(self.manifests[name])

    def upload_file(self, name, source, _media_type):
        self.files[name] = Path(source).read_bytes()

    def download(self, name, destination):
        destination.write_bytes(self.files[name])

    def signed_url(self, name):
        return f"https://teststorage.blob.core.windows.net/screenshots/{name}"


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.storage = MemoryStorage()
        self.jobs = VideoJobs(storage=self.storage, root=self.temporary.name)
        self.jobs.ensure = Mock()
        self.scope = "a" * 32
        self.settings = render_settings(15, 25, "480p", "standard", samples=8)

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self):
        def snapshot(path):
            path.write_bytes(b"inspected-animation")
            path.with_name("viewport.png").write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="))
        value = self.jobs.create(self.scope, self.settings, snapshot, inspect_first=True)
        return value["id"]

    def document(self, job_id):
        return self.jobs.repo.read(self.scope, job_id)[0]

    def complete_preview(self, job_id):
        document, etag = self.jobs.repo.read(self.scope, job_id)
        document, etag = self.jobs.repo.update(document, etag, "rendering")
        document, etag = self.jobs.repo.update(document, etag, "encoding")
        return self.jobs.repo.update(document, etag, "completed", progress=100,
                                     preview_blob=f"videos/{self.scope}/{job_id}/480p.mp4",
                                     output_blob=f"videos/{self.scope}/{job_id}/480p.mp4")[0]

    def test_preparation_publishes_exact_blend_without_starting_render(self):
        job_id = self.prepare()
        self.jobs.ensure.assert_not_called()
        status = self.jobs.status(self.scope, job_id)
        self.jobs.ensure.assert_not_called()
        self.assertEqual((status["state"], status["fps"], status["resolution"]), ("awaiting_render_approval", 25, "480p"))
        self.assertTrue(status["scene_url"].endswith("/scene.blend"))
        self.assertTrue(status["viewport_url"].endswith("/viewport.png"))
        self.assertTrue(self.storage.files[self.document(job_id)["viewport_blob"]].startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual((status["duration_seconds"], self.document(job_id)["frames"]), (15, 375))
        self.assertFalse(status["hd_available"])
        self.assertEqual(self.storage.files[self.document(job_id)["scene_blob"]], b"inspected-animation")

    def test_hd_cannot_start_before_inspection_and_completed_preview(self):
        job_id = self.prepare()
        with self.assertRaises(ValueError):
            self.jobs.render(self.scope, job_id, resolution="1080p")
        preview = self.jobs.render(self.scope, job_id)
        self.assertEqual((preview["state"], preview["fps"], preview["resolution"]), ("queued", 25, "480p"))
        self.assertEqual(self.jobs.render(self.scope, job_id)["state"], "queued")
        with self.assertRaises(ValueError):
            self.jobs.render(self.scope, job_id, resolution="1080p")
        self.complete_preview(job_id)
        self.assertTrue(self.jobs.status(self.scope, job_id)["hd_available"])
        original_scene = self.document(job_id)["scene_blob"]
        hd = self.jobs.render(self.scope, job_id, resolution="1080p")
        self.assertEqual((hd["id"], hd["state"], hd["resolution"], hd["fps"]), (job_id, "queued", "1080p", 25))
        document = self.document(job_id)
        self.assertEqual((document["width"], document["height"], document["frames"]), (1920, 1080, 375))
        self.assertEqual(document["scene_blob"], original_scene)
        self.assertTrue(document["preview_blob"].endswith("/480p.mp4"))
        self.assertEqual(self.jobs.render(self.scope, job_id, resolution="1080p")["state"], "queued")

    def test_recovered_worker_renders_the_frozen_scene_and_keeps_preview(self):
        job_id = self.prepare()
        self.jobs.render(self.scope, job_id)
        self.complete_preview(job_id)
        self.jobs.render(self.scope, job_id, resolution="1080p")
        directory = Path(self.temporary.name) / job_id
        (directory / "scene.blend").write_bytes(b"changed-after-inspection")
        (directory / "frames").mkdir()
        (directory / "frames" / "000001.png").write_bytes(b"old-480p-frame")
        document, etag = self.jobs.repo.read(self.scope, job_id)

        def process(_command, **_kwargs):
            self.assertEqual((directory / "scene.blend").read_bytes(), b"inspected-animation")
            self.assertEqual(json.loads((directory / "settings.json").read_text())["width"], 1920)
            self.assertFalse((directory / "frames").exists())
            (directory / "frames").mkdir()
            (directory / "frames" / "000001.png").write_bytes(b"native-hd-frame")
            return SimpleNamespace(poll=lambda: 0, returncode=0)

        with patch("video_jobs.subprocess.Popen", side_effect=process), patch("video_jobs.run_media", side_effect=lambda command, **_kw: Path(command[-1]).write_bytes(b"hd-video")):
            completed, _ = self.jobs._render(document, etag, directory, threading.Event())
        self.assertEqual(completed["state"], "completed")
        self.assertTrue(completed["preview_blob"].endswith("/480p.mp4"))
        self.assertTrue(completed["output_blob"].endswith("/1080p.mp4"))
        self.assertFalse(self.jobs.describe(completed)["hd_available"])

    def test_worker_cannot_render_without_matching_approval(self):
        job_id = self.prepare()
        document, etag = self.jobs.repo.read(self.scope, job_id)
        document["state"] = "queued"
        with self.assertRaisesRegex(ValueError, "approval"):
            self.jobs._render(document, etag, Path(self.temporary.name) / job_id, threading.Event())

    def test_wrong_scope_other_jobs_and_paid_approval_are_rejected(self):
        job_id = self.prepare()
        with self.assertRaises((KeyError, ValueError)):
            self.jobs.render("b" * 32, job_id)
        self.assertEqual(self.jobs.repo.approve(self.scope, job_id, "Paid finish")["state"], "awaiting_render_approval")
        value = self.jobs.create(self.scope, render_settings(), lambda path: path.write_bytes(b"regular-scene"))
        self.assertEqual(value["state"], "queued")
        with self.assertRaises(ValueError):
            self.jobs.render(self.scope, value["id"])

    def test_cancel_before_render_cleans_local_snapshot_but_keeps_download(self):
        job_id = self.prepare()
        cancelled = self.jobs.cancel(self.scope, job_id)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertFalse((Path(self.temporary.name) / job_id / "scene.blend").exists())
        self.assertIn(self.document(job_id)["scene_blob"], self.storage.files)
        self.jobs.ensure.assert_not_called()

    def test_workload_limits_and_exact_25_fps_timeline(self):
        for duration in (15, 18, 20):
            preview = render_settings(duration, 25, "480p", "standard")
            self.assertEqual(preview["frames"], duration * 25)
            hd = render_settings(duration, 25, "1080p", "standard", allow_hd=True)
            self.assertEqual((hd["width"], hd["height"]), (1920, 1080))
            keys = validate_camera_path([
                {"time": 0, "position": [0, -5, 2], "target": [0, 0, 0]},
                {"time": duration, "position": [5, 0, 2], "target": [0, 0, 0]},
            ], duration, 25)
            self.assertEqual([key["frame"] for key in keys], [1, duration * 25])
        for options in ({}, {"allow_hd": False}, {"allow_hd": True, "fps": 24}, {"allow_hd": True, "duration_seconds": 30}):
            settings = dict(duration_seconds=18, fps=25, resolution="1080p", mode="standard")
            settings.update(options)
            with self.assertRaises(ValueError):
                render_settings(**settings)
        self.assertEqual(render_settings()["frames"], 120)

    def test_prompt_requests_caricatures_and_inspection_not_automatic_render(self):
        candidate = MusicCandidate(film="Test film", track=None, confidence="low", evidence="Test")
        prompt = music_instructions(MusicRequest(candidate=candidate, action="generate"))
        for required in ("caricature", "EXACTLY 15 seconds", "375 frames", "25 fps", "480p", "awaiting_render_approval", "download", "1080p", "SAME frozen animation", "captures ONE viewport screenshot automatically"):
            self.assertIn(required, prompt)

    def test_missing_viewport_is_an_error_not_a_card_without_preview(self):
        with self.assertRaisesRegex(ValueError, "viewport preview"):
            self.jobs.create(self.scope, self.settings, lambda path: path.write_bytes(b"scene"), inspect_first=True)
        self.assertEqual(self.storage.manifests, {})
        self.jobs.ensure.assert_not_called()

    def test_new_inspection_cannot_prepare_an_18_second_animation(self):
        with self.assertRaisesRegex(ValueError, "5, 10 or 15"):
            self.jobs.create(self.scope, render_settings(18, 25, "480p", "standard"), Mock(), inspect_first=True)

    def test_existing_18_second_snapshot_is_not_shortened_by_render_controls(self):
        job_id = self.prepare()
        document, etag = self.jobs.repo.read(self.scope, job_id)
        document.update(duration_seconds=18, frames=450)
        self.storage.write_json(f"video-jobs/{self.scope}/{job_id}.json", document, etag=etag)
        self.assertEqual(self.jobs.render(self.scope, job_id)["duration_seconds"], 18)
        self.complete_preview(job_id)
        self.assertEqual(self.jobs.render(self.scope, job_id, resolution="1080p")["duration_seconds"], 18)
        self.assertEqual(self.document(job_id)["frames"], 450)

    def test_all_six_generation_profiles_prepare_and_upgrade_without_losing_style(self):
        for duration in (5, 10, 15):
            for mode in ("clay", "standard"):
                self.settings = render_settings(duration, 25, "480p", mode)
                job_id = self.prepare()
                prepared = self.jobs.status(self.scope, job_id)
                self.assertEqual((prepared["duration_seconds"], prepared["mode"]), (duration, mode))
                self.assertEqual(self.document(job_id)["frames"], duration * 25)
                self.jobs.render(self.scope, job_id)
                self.complete_preview(job_id)
                hd = self.jobs.render(self.scope, job_id, resolution="1080p")
                self.assertEqual((hd["duration_seconds"], hd["mode"], hd["resolution"]), (duration, mode, "1080p"))
                self.assertEqual(self.document(job_id)["frames"], duration * 25)

    def test_invalid_user_generation_options_are_rejected(self):
        candidate = MusicCandidate(film="Test film", track=None, confidence="low", evidence="Test")
        for options in ({"duration_seconds": 0}, {"duration_seconds": 7}, {"duration_seconds": 20}, {"render_mode": "cycles"}):
            with self.assertRaises(ValueError):
                MusicRequest(candidate=candidate, action="generate", **options)


if __name__ == "__main__":
    unittest.main()
