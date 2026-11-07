"""Offline workspace scene switching, Blob recovery and input audio preservation."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.core.exceptions import ResourceNotFoundError
import scene_manager as sm
from music import archive_music_audio, pcm_to_wav


class MemoryScenes:
    def __init__(self):
        self.files = {}
    def upload_file(self, name, path, _type):
        self.files[name] = Path(path).read_bytes()
    def download(self, name, path):
        if name not in self.files:
            raise ResourceNotFoundError("Missing checkpoint")
        path.write_bytes(self.files[name])


class WorkspaceSceneTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.patches = [
            patch.object(sm, "_SCENE_DIR", str(root)),
            patch.object(sm, "_SCENE_FILE", str(root / "scene.blend")),
            patch.object(sm, "_SESSION_STATE_FILE", str(root / "state.json")),
            patch.object(sm, "current_epoch", return_value=1),
            patch.object(sm, "_workspace_storage", MemoryScenes()),
        ]
        for item in self.patches:
            item.start()
        self.current = {}
        self.manager = sm.SceneManager()
        self.manager._reset_scene = lambda: self.current.clear()
        self.manager._save_blend_file = lambda path, **_kw: Path(path).write_text(json.dumps(self.current))
        self.manager._load_blend_file = lambda path: self.current.update(json.loads(Path(path).read_text()))

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def use(self, identity, scope, resume):
        token = sm.workspace_scene.set((scope, resume))
        try:
            self.current.clear()
            self.manager.activate_scene(identity)
        finally:
            sm.workspace_scene.reset(token)

    def save(self, identity, scope):
        token = sm.workspace_scene.set((scope, False))
        try:
            self.manager.save_scene(identity)
        finally:
            sm.workspace_scene.reset(token)

    def test_new_scene_is_empty_and_a_b_a_preserves_each_work(self):
        self.use("workspace-a", "a" * 32, False)
        self.assertEqual(self.current, {})
        self.current["cube"] = "red"
        self.save("workspace-a", "a" * 32)
        self.use("workspace-b", "b" * 32, False)
        self.assertEqual(self.current, {})
        self.current["sphere"] = "blue"
        self.save("workspace-b", "b" * 32)
        self.use("workspace-a", "a" * 32, True)
        self.assertEqual(self.current, {"cube": "red"})
        self.use("workspace-b", "b" * 32, True)
        self.assertEqual(self.current, {"sphere": "blue"})

    def test_blob_checkpoint_survives_loss_of_local_files(self):
        self.use("workspace-a", "a" * 32, False)
        self.current["duck"] = "animated"
        self.save("workspace-a", "a" * 32)
        Path(sm._SCENE_FILE).unlink()
        self.manager._new_scene = True
        self.use("workspace-a", "a" * 32, True)
        self.assertEqual(self.current, {"duck": "animated"})

    def test_missing_expected_checkpoint_refuses_empty_replacement(self):
        with self.assertRaisesRegex(RuntimeError, "refusing"):
            self.use("workspace-missing", "f" * 32, True)

    def test_unscoped_local_archive_still_switches_without_blob(self):
        self.manager.activate_scene("local-a")
        self.current["local"] = 1
        self.manager.save_scene("local-a")
        self.manager.activate_scene("local-b")
        self.current.clear()
        self.manager.save_scene("local-b")
        self.manager.activate_scene("local-a")
        self.assertEqual(self.current, {"local": 1})

    def test_load_error_does_not_reset_the_saved_work(self):
        reset = Mock()
        self.manager._reset_scene = reset
        with patch.object(sm, "get_blender_connection", return_value=Mock(send_command=Mock(side_effect=ValueError("Load failed")))):
            with self.assertRaises(ValueError):
                sm.SceneManager._load_blend_file(self.manager, "scene.blend")
        reset.assert_not_called()

    def test_music_audio_is_saved_as_exact_wav_with_private_scope(self):
        pcm = b"\x01\x00" * 24000 * 2
        storage = Mock()
        storage.signed_url.return_value = "https://teststorage.blob.core.windows.net/screenshots/music-audio/audio.wav"
        with patch("music.ArtifactStorage", return_value=storage):
            audio = archive_music_audio(pcm, "a" * 32)
        name, data, mime = storage.upload.call_args.args
        self.assertTrue(name.startswith("music-audio/" + "a" * 32 + "/"))
        self.assertEqual(data, pcm_to_wav(pcm))
        self.assertEqual(mime, "audio/wav")
        self.assertEqual(audio["duration_seconds"], 2)


if __name__ == "__main__":
    unittest.main()
