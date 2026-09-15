"""Offline video pipeline contracts: .venv/Scripts/python devTools/test_video_pipeline.py."""

import json
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError
from artifact_storage import ArtifactStorage, conversation_scope, validate_blob_name
from media_analysis import render_settings, validate_camera_path
from unittest.mock import patch
import httpx
from wavespeed_client import WaveSpeedClient
from video_jobs import JobRepository, VideoJobs, job_name
from pathlib import Path
import tempfile
from media_control import decode_envelope, PREFIX
import base64
import hashlib
import hmac


class ControlTests(unittest.TestCase):
    def test_signed_scope_and_reference_ownership(self):
        secret = "test-secret-" * 4
        def encode(value):
            part = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
            return PREFIX + part + "." + hmac.new(secret.encode(), part.encode(), hashlib.sha256).hexdigest()
        value = {"scope": "a" * 32, "text": "Build this", "references": ["references/" + "a" * 32 + "/" + "b" * 32 + ".png"]}
        self.assertEqual(decode_envelope(encode(value), secret), value)
        with self.assertRaises(ValueError):
            decode_envelope(encode(value), "wrong-secret" * 4)
        with self.assertRaises(ValueError):
            decode_envelope(encode(dict(value, scope="c" * 32)), secret)


class JobTests(unittest.TestCase):
    def test_restart_reconciles_persisted_job_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            jobs = VideoJobs(storage=SimpleNamespace(), root=temporary)
            directory = Path(temporary) / ("b" * 32)
            directory.mkdir()
            (directory / "identity.json").write_text(json.dumps({"scope": "a" * 32, "id": "b" * 32}))
            with patch.object(jobs, "ensure") as ensure:
                jobs.resume_pending()
            ensure.assert_called_once_with("a" * 32, "b" * 32)

    def test_rejected_cancel_does_not_leave_stop_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            jobs = VideoJobs(storage=SimpleNamespace(), root=temporary)
            jobs.repo = SimpleNamespace(
                read=lambda scope, job_id: ({"state": "wavespeed_uploading"}, "old"),
                update=lambda *args: (_ for _ in ()).throw(ResourceModifiedError("raced")),
            )
            with self.assertRaises(ResourceModifiedError):
                jobs.cancel("a" * 32, "b" * 32)
            self.assertFalse((Path(temporary) / ("b" * 32) / "cancel").exists())

    def test_failed_job_removes_large_temporary_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            jobs = VideoJobs(storage=SimpleNamespace(), root=temporary)
            directory = Path(temporary) / ("b" * 32)
            (directory / "frames").mkdir(parents=True)
            (directory / "frames" / "000001.png").write_bytes(b"frame")
            (directory / "scene.blend").write_bytes(b"scene")
            jobs._finalize({"id": "b" * 32, "state": "failed"})
            self.assertFalse((directory / "frames").exists())
            self.assertFalse((directory / "scene.blend").exists())

    def test_approval_is_atomic_and_cannot_repeat(self):
        blob = MemoryBlob()
        storage = ArtifactStorage("teststorage", service=SimpleNamespace(get_blob_client=lambda container, name: blob))
        repository = JobRepository(storage)
        scope, job_id = "a" * 32, "b" * 32
        document = dict(scope=scope, id=job_id, state="awaiting_seedance_approval", duration_seconds=5)
        etag = storage.write_json(job_name(scope, job_id), document)
        with patch.dict(os.environ, {"WAVESPEED_API_KEY": "test-only"}):
            approved = repository.approve(scope, job_id, "Finish this preview")
            repeated = repository.approve(scope, job_id, "Different prompt")
        self.assertEqual(approved["approval"], repeated["approval"])
        self.assertEqual(approved["approval"]["estimate_usd"], 2.2)
        with self.assertRaises(ResourceModifiedError):
            repository.update(document, etag, "wavespeed_uploading")
        with self.assertRaises(ValueError):
            repository.read("c" * 32, job_id)

    def test_paid_transition_requires_preview(self):
        repository = JobRepository(None)
        with self.assertRaises(ValueError):
            repository.update({"state": "queued"}, "etag", "wavespeed_submitting")


class WaveSpeedTests(unittest.TestCase):
    def test_submit_contract_and_no_retry(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(503, json={"message": "secret"})
        with patch.dict(os.environ, {"WAVESPEED_API_KEY": "test-only"}):
            client = WaveSpeedClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
            with self.assertRaisesRegex(ValueError, "HTTP 503"):
                client.submit("https://example.com/video.mp4", "Studio scene", "720p", False)
            client.close()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/api/v3/bytedance/seedance-2.5/video-edit")
        self.assertFalse(json.loads(requests[0].content)["generate_audio"])


class MediaTests(unittest.TestCase):
    def test_budget(self):
        self.assertEqual(render_settings()["frames"], 120)
        for kwargs in ({"duration_seconds": float("nan")}, {"fps": True}, {"resolution": "4k"},
                       {"duration_seconds": 30, "resolution": "720p"}, {"mode": "unknown"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                render_settings(**kwargs)

    def test_camera(self):
        keys = [dict(time=0, position=[0, -5, 2], target=[0, 0, 0]),
                dict(time=5, position=[5, 0, 2], target=[0, 0, 0])]
        self.assertEqual([key["frame"] for key in validate_camera_path(keys, 5)], [1, 120])
        for bad in ([keys[1], keys[0]], [keys[0], dict(keys[1], lens=float("nan"))],
                    [keys[0], dict(keys[1], position=[0, 0, 0])]):
            with self.assertRaises(ValueError):
                validate_camera_path(bad, 5)


class MemoryBlob:
    def __init__(self):
        self.payload = None
        self.version = 0

    def upload_blob(self, payload, *, overwrite, **kwargs):
        if self.payload is not None and not overwrite:
            raise ResourceExistsError("Exists")
        if overwrite:
            if kwargs.get("match_condition") != MatchConditions.IfNotModified:
                raise AssertionError("Missing conditional write")
            if kwargs["etag"] != str(self.version):
                raise ResourceModifiedError("Stale manifest")
        self.version += 1
        self.payload = payload
        return {"etag": str(self.version)}

    def download_blob(self):
        return SimpleNamespace(size=len(self.payload), readall=lambda: self.payload,
                               properties=SimpleNamespace(etag=str(self.version)))


class StorageTests(unittest.TestCase):
    def test_manifest_compare_and_swap(self):
        blob = MemoryBlob()
        service = SimpleNamespace(get_blob_client=lambda container, name: blob)
        storage = ArtifactStorage("teststorage", service=service)
        name = "video-jobs/conversation/job.json"
        first = storage.write_json(name, {"state": "queued"})
        with self.assertRaises(ResourceExistsError):
            storage.write_json(name, {"state": "queued"})
        storage.write_json(name, {"state": "rendering"}, etag=first)
        with self.assertRaises(ResourceModifiedError):
            storage.write_json(name, {"state": "cancelled"}, etag=first)
        self.assertEqual(storage.read_json(name)[0]["state"], "rendering")
        self.assertEqual(json.loads(blob.payload)["state"], "rendering")

    def test_artifact_names_and_scope(self):
        for name in ("../secret", "/absolute", "a//b", "x?sig=secret", "x\\y", "a/./b"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_blob_name(name)
        self.assertEqual(len(conversation_scope("conversation")), 32)
        self.assertNotEqual(conversation_scope("one"), conversation_scope("two"))


if __name__ == "__main__":
    unittest.main()