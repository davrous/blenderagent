"""Signed workspace deletion and isolated LLM-title instructions."""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from agent_framework import AgentResponse, AgentResponseUpdate, Content, Message, ResponseStream
from media_control import MediaMiddleware, decode_envelope
from artifact_storage import ArtifactStorage, WorkspaceDeletedError, workspace_artifact_name
from azure.core.exceptions import ResourceNotFoundError


SECRET = "workspace-control-test-" * 3
IDENTITY = "af43df8a-6e27-48b4-9647-18ad8c9b0f3a"


def signed(value):
    part = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    signature = hmac.new(SECRET.encode(), part.encode(), hashlib.sha256).hexdigest()
    return f"BLENDER_MEDIA_V1:{part}.{signature}"


class WorkspaceControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletion_waits_for_scene_lock_and_never_calls_model(self):
        manager = Mock()
        lock = asyncio.Lock()
        await lock.acquire()
        inner = SimpleNamespace(_scene_manager=manager, _scene_lock=lock, process=AsyncMock())
        value = {"scope": "a" * 32, "text": "Delete local checkpoint.", "workspace_delete": IDENTITY}
        context = SimpleNamespace(messages=[Message(role="user", contents=[Content.from_text(signed(value))])], stream=False)
        with patch.dict(os.environ, {"MEDIA_CONTROL_SECRET": SECRET}):
            pending = asyncio.create_task(MediaMiddleware(inner).process(context, AsyncMock()))
            await asyncio.sleep(0)
            manager.delete_workspace_scene.assert_not_called()
            lock.release()
            await pending
        manager.delete_workspace_scene.assert_called_once_with(IDENTITY)
        inner.process.assert_not_awaited()
        self.assertEqual(json.loads(context.result.text), {"workspace_deleted": IDENTITY})

    async def test_delete_failure_is_not_a_success_response(self):
        manager = Mock()
        manager.delete_workspace_scene.side_effect = OSError("Checkpoint is locked")
        inner = SimpleNamespace(_scene_manager=manager, _scene_lock=asyncio.Lock(), process=AsyncMock())
        value = {"scope": "a" * 32, "text": "Delete.", "workspace_delete": IDENTITY}
        context = SimpleNamespace(messages=[Message(role="user", contents=[Content.from_text(signed(value))])], stream=False)
        with patch.dict(os.environ, {"MEDIA_CONTROL_SECRET": SECRET}):
            with self.assertRaises(OSError):
                await MediaMiddleware(inner).process(context, AsyncMock())
        inner.process.assert_not_awaited()
        self.assertFalse(inner._scene_lock.locked())

    def test_delete_rejects_invalid_identity_and_combined_controls(self):
        base = {"scope": "a" * 32, "text": "Delete.", "workspace_delete": IDENTITY}
        for value in [{**base, "workspace_delete": "../scene"}, {**base, "action": {}}, {**base, "workspace": {"resume": False}}]:
            with self.assertRaises(ValueError):
                decode_envelope(signed(value), SECRET)

    async def test_title_instruction_only_applies_to_owned_workspace_turn(self):
        value = {"scope": "a" * 32, "text": "Create a duck.", "workspace": {"resume": False}}
        original = [Message(role="user", contents=[Content.from_text(signed(value))])]
        context = SimpleNamespace(messages=original, options={}, stream=False)

        async def process(ctx, _next):
            self.assertIn("workspacetitle", ctx.messages[-1].text)
            self.assertIn("French title", ctx.messages[-1].text)

        inner = SimpleNamespace(process=process)
        with patch.dict(os.environ, {"MEDIA_CONTROL_SECRET": SECRET}):
            await MediaMiddleware(inner).process(context, AsyncMock())
        self.assertIs(context.messages, original)

    async def test_title_instruction_survives_stream_and_is_cleaned_up(self):
        value = {"scope": "a" * 32, "text": "Create a duck.", "workspace": {"resume": False}}
        original = [Message(role="user", contents=[Content.from_text(signed(value))])]
        context = SimpleNamespace(messages=original, options={}, stream=True, result=None)

        async def process(ctx, _next):
            async def updates():
                self.assertIn("workspacetitle", ctx.messages[-1].text)
                yield AgentResponseUpdate(role="assistant", contents=[Content.from_text("A duck")])
                raise RuntimeError("Stream interrupted")
            ctx.result = ResponseStream(updates(), finalizer=AgentResponse.from_updates)

        with patch.dict(os.environ, {"MEDIA_CONTROL_SECRET": SECRET}):
            await MediaMiddleware(SimpleNamespace(process=process)).process(context, AsyncMock())
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            async for _ in context.result:
                pass
        self.assertIs(context.messages, original)


class WorkspaceArtifactTests(unittest.TestCase):
    def test_outputs_use_a_scoped_basename_and_keep_legacy_names(self):
        self.assertEqual(workspace_artifact_name("scenes/scene_unique.blend", "a" * 32),
                         "workspace-artifacts/" + "a" * 32 + "/scene_unique.blend")
        self.assertEqual(workspace_artifact_name("scenes/scene_unique.blend", None), "scenes/scene_unique.blend")

    def storage(self, marker, output):
        service = SimpleNamespace(get_blob_client=lambda _container, name: marker if name.endswith("/deleted.json") else output)
        return ArtifactStorage("teststorage", service=service)

    def test_deleted_scope_blocks_artifact_and_job_manifest_writes(self):
        marker = Mock()
        output = Mock()
        storage = self.storage(marker, output)
        for name in ["workspace-artifacts/" + "a" * 32 + "/view.png", "video-jobs/" + "a" * 32 + "/job.json"]:
            with self.assertRaises(WorkspaceDeletedError):
                storage.upload(name, b"image", "image/png")
            with self.assertRaises(WorkspaceDeletedError):
                storage.read_json(name)
        output.upload_blob.assert_not_called()
        output.download_blob.assert_not_called()

    def test_deletion_during_upload_removes_the_exact_late_output(self):
        marker = Mock()
        marker.get_blob_properties.side_effect = [ResourceNotFoundError("Missing marker"), object()]
        output = Mock()
        output.upload_blob.return_value = {"etag": "uploaded-version"}
        storage = self.storage(marker, output)
        with self.assertRaises(WorkspaceDeletedError):
            storage.upload("music-audio/" + "a" * 32 + "/take.wav", b"audio", "audio/wav")
        self.assertEqual(output.delete_blob.call_args.kwargs["etag"], "uploaded-version")
        self.assertEqual(output.delete_blob.call_args.kwargs["delete_snapshots"], "include")

    def test_storage_error_is_not_treated_as_absent_deletion_marker(self):
        marker = Mock()
        marker.get_blob_properties.side_effect = OSError("Storage unavailable")
        output = Mock()
        with self.assertRaises(OSError):
            self.storage(marker, output).upload("videos/" + "a" * 32 + "/movie.mp4", b"movie", "video/mp4")
        output.upload_blob.assert_not_called()


if __name__ == "__main__":
    unittest.main()
