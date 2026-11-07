"""Offline Devfest music contracts: .venv/bin/python devTools/test_music_pipeline.py."""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import sys
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import music
import voice_pipeline as vp
from agent_framework import AgentResponse, AgentResponseUpdate, Content, Message, ResponseStream
from media_control import MediaMiddleware, decode_envelope, start_animation_render
from media_analysis import render_settings
from video_jobs import video_scope

ENV = {"MUSIC_AUDIO_ENDPOINT": "https://test.cognitiveservices.azure.com", "MUSIC_MODEL_DEPLOYMENT_NAME": "audio-test"}
CANDIDATE = music.MusicCandidate(film="An original space adventure", track="Space theme", confidence="low", evidence="Une melodie de fanfare.")
PCM = b"\x01\x00" * music.SAMPLE_RATE * 2


class AudioTests(unittest.TestCase):
    def test_wav_preserves_melody_and_exact_boundaries(self):
        for seconds in (2, 30):
            pcm = b"\x01\x00" * music.SAMPLE_RATE * seconds
            with wave.open(io.BytesIO(music.pcm_to_wav(pcm))) as audio:
                self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth()), (24000, 1, 2))
                self.assertEqual(audio.getnframes(), seconds * 24000)
                self.assertEqual(audio.readframes(audio.getnframes()), pcm)
        for pcm in (b"", PCM[:-2], PCM + b"x", b"\x00" * (music.MAX_PCM_BYTES + 2)):
            with self.assertRaises(ValueError):
                music.pcm_to_wav(pcm)

    def test_unknown_result_has_no_creation_offer(self):
        candidate = music.MusicCandidate(film=None, track=None, confidence="unknown", evidence="Pas de melodie identifiable.")
        self.assertNotIn("Devfest", music.recognition_reply(candidate))
        with self.assertRaises(ValueError):
            music.MusicRequest(candidate=candidate, action="generate")
        with self.assertRaises(ValueError):
            music.MusicCandidate(film=None, track=None, confidence="high", evidence="Noise")


class RecognitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_wav_contract_no_transcription_or_tools(self):
        calls = []

        def respond(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": CANDIDATE.model_dump_json()}}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        credential = AsyncMock()
        credential.__aenter__.return_value = credential
        credential.get_token.return_value = SimpleNamespace(token="test-only-token")
        with patch.dict(os.environ, ENV), patch.object(music, "DefaultAzureCredential", return_value=credential), patch.object(music.httpx, "AsyncClient", return_value=client):
            result = await music.recognize_music(PCM)
        self.assertEqual(result, CANDIDATE)
        body = calls[0]
        self.assertEqual(body["modalities"], ["text"])
        self.assertNotIn("tools", body)
        self.assertNotIn("response_format", body)
        audio = body["messages"][1]["content"][1]["input_audio"]
        self.assertEqual(audio["format"], "wav")
        self.assertEqual(base64.b64decode(audio["data"]), music.pcm_to_wav(PCM))
        credential.get_token.assert_awaited_once_with("https://cognitiveservices.azure.com/.default")

    async def test_missing_configuration_fails_without_calling_model(self):
        with patch.dict(os.environ, {"MUSIC_AUDIO_ENDPOINT": "", "MUSIC_MODEL_DEPLOYMENT_NAME": ""}):
            with self.assertRaisesRegex(ValueError, "requires MUSIC"):
                await music.recognize_music(PCM)


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.frames = []

        async def send(frame):
            self.frames.append(frame)

        self.session = vp.VoiceSession(None, send, AsyncMock(), session_id="browser-test", foundry_conversation_id="conv_shared")
        self.session._previous_response_id = "resp_typed"

    async def asyncTearDown(self):
        await self.session.close()
        if self.session._turn_task:
            try:
                await self.session._turn_task
            except asyncio.CancelledError:
                pass

    async def test_music_bypasses_stt_blender_and_tts_and_commits_shared_history(self):
        with patch.dict(os.environ, ENV), patch.object(music, "recognize_music", AsyncMock(return_value=CANDIDATE)) as recognize, patch.object(self.session, "_start_capture", AsyncMock()) as stt, patch.object(self.session, "_run_turn", AsyncMock()) as blender, patch.object(vp, "_is_hosted", return_value=True):
            await self.session.on_control({"type": "start", "mode": "music"})
            await self.session.on_audio(PCM)
            await self.session.on_control({"type": "commit"})
            await self.session._turn_task
        recognize.assert_awaited_once_with(PCM)
        stt.assert_not_awaited()
        blender.assert_not_awaited()
        self.assertIsNone(self.session._tts_task)
        self.assertEqual(self.session._music_pcm, b"")
        self.assertEqual(self.session._previous_response_id, "resp_typed")
        self.assertEqual([frame["type"] for frame in self.frames], ["listening", "stt", "delta", "delta", "music_result", "done"])
        self.assertIn("🎵", self.frames[2]["text"])
        self.assertTrue(self.frames[-1]["history_commit_required"])
        self.assertTrue(self.frames[-1]["history_response_id"].startswith("music-"))
        self.assertIsNone(self.frames[-1]["response_id"])

    async def test_normal_microphone_mode_unchanged(self):
        with patch.object(self.session, "_start_capture", AsyncMock()) as stt:
            await self.session.on_control({"type": "start"})
            stt.assert_awaited_once()
        self.assertEqual(self.session._capture_mode, "voice")

    async def test_short_oversize_and_invalid_pcm_are_explicit_errors(self):
        with patch.dict(os.environ, ENV):
            await self.session.on_control({"type": "start", "mode": "music"})
            await self.session.on_audio(PCM[:-2])
            await self.session.on_control({"type": "commit"})
            self.assertEqual(self.frames[-1]["type"], "error")
            self.assertIsNone(self.session._turn_task)
            for chunk in (b"x", b"x" * (music.MAX_PCM_BYTES + 2)):
                await self.session.on_control({"type": "start", "mode": "music"})
                await self.session.on_audio(chunk)
                self.assertEqual(self.frames[-1]["type"], "error")
                self.assertFalse(self.session._capturing)
                self.assertEqual(self.session._music_pcm, b"")

    async def test_cancel_during_model_call_never_produces_a_guess(self):
        entered = asyncio.Event()

        async def wait_for_model(_pcm):
            entered.set()
            await asyncio.Event().wait()

        with patch.dict(os.environ, ENV), patch.object(music, "recognize_music", side_effect=wait_for_model):
            await self.session.on_control({"type": "start", "mode": "music"})
            await self.session.on_audio(PCM)
            await self.session.on_control({"type": "commit"})
            await entered.wait()
            await self.session.on_control({"type": "cancel"})
            with self.assertRaises(asyncio.CancelledError):
                await self.session._turn_task
        self.assertNotIn("music_result", [frame["type"] for frame in self.frames])

    async def test_provider_failure_is_not_a_success_shaped_reply(self):
        with patch.dict(os.environ, ENV), patch.object(music, "recognize_music", AsyncMock(side_effect=ValueError("audio provider rejected input"))):
            await self.session.on_control({"type": "start", "mode": "music"})
            await self.session.on_audio(PCM)
            await self.session.on_control({"type": "commit"})
            await self.session._turn_task
        self.assertEqual(self.frames[-1]["type"], "error")
        self.assertNotIn("done", [frame["type"] for frame in self.frames])


class ScopedSceneTests(unittest.IsolatedAsyncioTestCase):
    def envelope(self, action):
        value = {"scope": "a" * 32, "text": "User response", "music": {"candidate": CANDIDATE.model_dump(), "action": action}}
        part = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        secret = "test-music-secret-" * 4
        signature = hmac.new(secret.encode(), part.encode(), hashlib.sha256).hexdigest()
        return f"BLENDER_MEDIA_V1:{part}.{signature}", secret

    async def test_discussion_disables_tools_and_does_not_leak_instructions(self):
        text, secret = self.envelope("discuss")
        original = [Message(role="user", contents=[Content.from_text(text)])]
        context = SimpleNamespace(messages=original, options={"existing": True}, stream=False)
        observed = []

        async def process(ctx, _next):
            observed.append(ctx.options["tool_choice"])
            self.assertFalse(music.music_scene.get())
            self.assertIn("not confirmed", ctx.messages[-1].text)

        with patch.dict(os.environ, {"MEDIA_CONTROL_SECRET": secret}):
            await MediaMiddleware(SimpleNamespace(process=process)).process(context, AsyncMock())
        self.assertEqual(observed, ["none"])
        self.assertIs(context.messages, original)
        self.assertEqual(context.options, {"existing": True})
        self.assertFalse(music.music_scene.get())
        self.assertIsNone(video_scope.get())

    async def test_generation_scope_survives_stream_and_is_reset_on_failure(self):
        text, secret = self.envelope("generate")
        original = [Message(role="user", contents=[Content.from_text(text)])]
        context = SimpleNamespace(messages=original, options={}, stream=True, result=None)

        async def process(ctx, _next):
            async def updates():
                self.assertTrue(music.music_scene.get())
                self.assertEqual(video_scope.get(), "a" * 32)
                self.assertIn("EXACTLY 15 seconds", ctx.messages[-1].text)
                yield AgentResponseUpdate(role="assistant", contents=[Content.from_text("Creating")])
                raise RuntimeError("stream failed")
            ctx.result = ResponseStream(updates(), finalizer=AgentResponse.from_updates)

        with patch.dict(os.environ, {"MEDIA_CONTROL_SECRET": secret}):
            await MediaMiddleware(SimpleNamespace(process=process)).process(context, AsyncMock())
        self.assertFalse(music.music_scene.get())
        with self.assertRaisesRegex(RuntimeError, "stream failed"):
            async for _ in context.result:
                pass
        self.assertIs(context.messages, original)
        self.assertFalse(music.music_scene.get())
        self.assertIsNone(video_scope.get())

    async def test_old_music_envelope_cannot_enable_a_normal_turn(self):
        text, secret = self.envelope("generate")
        context = SimpleNamespace(messages=[
            Message(role="user", contents=[Content.from_text(text)]),
            Message(role="user", contents=[Content.from_text("Move my cube")]),
        ], stream=False)

        async def process(ctx, _next):
            self.assertFalse(music.music_scene.get())
            self.assertEqual(len(ctx.messages), 2)
        inner = SimpleNamespace(process=process, _get_conversation_id=lambda _ctx: None)
        with patch.dict(os.environ, {"MEDIA_CONTROL_SECRET": secret}):
            await MediaMiddleware(inner).process(context, AsyncMock())

    async def test_render_enforces_duration_style_and_no_paid_finishing(self):
        token = music.music_scene.set(music.MusicRequest(candidate=CANDIDATE, action="generate"))
        try:
            for settings in ({"duration_seconds": 14}, {"duration_seconds": 18}, {"duration_seconds": 21}, {"duration_seconds": 15, "mode": "clay"}, {"duration_seconds": 15, "engine": "CYCLES"}, {"duration_seconds": 15, "seedance": True}, {"duration_seconds": 15, "fps": 12}, {"duration_seconds": 15, "resolution": "720p"}):
                with self.assertRaises(ValueError):
                    await start_animation_render(**settings)
            scope = video_scope.set("a" * 32)
            jobs = SimpleNamespace(create=lambda _scope, settings, _snapshot, **kwargs: dict(id="b" * 32, **settings, **kwargs))
            try:
                with patch("media_control.get_video_jobs", return_value=jobs), patch("media_control.descriptor", side_effect=lambda value: value):
                    value = await start_animation_render(duration_seconds=15, fps=25, resolution="480p", mode="standard")
                self.assertEqual((value["duration_seconds"], value["frames"], value["seedance"], value["inspect_first"]), (15, 375, False, True))
            finally:
                video_scope.reset(scope)
        finally:
            music.music_scene.reset(token)
        self.assertEqual(render_settings(5)["duration_seconds"], 5)

    async def test_all_selected_durations_and_modes_reach_tools_and_prompt(self):
        for duration in (5, 10, 15):
            for mode in ("clay", "standard"):
                request = music.MusicRequest(candidate=CANDIDATE, action="generate",
                                             duration_seconds=duration, render_mode=mode)
                token = music.music_scene.set(request)
                scope = video_scope.set("a" * 32)
                jobs = SimpleNamespace(create=lambda _scope, settings, _snapshot, **kwargs: dict(settings, **kwargs))
                try:
                    prompt = music.music_instructions(request)
                    self.assertIn(f"EXACTLY {duration} seconds", prompt)
                    self.assertIn(f"mode='{mode}'", prompt)
                    with patch("media_control.get_video_jobs", return_value=jobs), patch("media_control.descriptor", side_effect=lambda value: value):
                        result = await start_animation_render(duration_seconds=duration, fps=25, mode=mode)
                        self.assertEqual((result["frames"], result["mode"], result["inspect_first"]), (duration * 25, mode, True))
                        with self.assertRaises(ValueError):
                            await start_animation_render(duration_seconds=duration, fps=25, mode="standard" if mode == "clay" else "clay")
                finally:
                    video_scope.reset(scope)
                    music.music_scene.reset(token)

    def test_signed_music_cannot_authorize_paid_actions(self):
        text, secret = self.envelope("generate")
        value = decode_envelope(text, secret)
        value["action"] = {"type": "approve", "job_id": "b" * 32}
        part = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        signature = hmac.new(secret.encode(), part.encode(), hashlib.sha256).hexdigest()
        with self.assertRaises(ValueError):
            decode_envelope(f"BLENDER_MEDIA_V1:{part}.{signature}", secret)


if __name__ == "__main__":
    unittest.main()
