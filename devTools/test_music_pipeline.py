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
from azure.core.exceptions import ClientAuthenticationError
import music
import voice_pipeline as vp
from agent_framework import Agent, AgentResponse, AgentResponseUpdate, ChatResponse, Content, Message, ResponseStream
from media_control import MediaMiddleware, apply_camera_path, decode_envelope, start_animation_render
from media_analysis import render_settings
from video_jobs import video_scope

ENV = {"MUSIC_AUDIO_ENDPOINT": "https://test.cognitiveservices.azure.com", "MUSIC_MODEL_DEPLOYMENT_NAME": "audio-test",
       "ACRCLOUD_HOST": "", "ACRCLOUD_ACCESS_KEY": "", "ACRCLOUD_ACCESS_SECRET": ""}
ACR_ENV = {"ACRCLOUD_HOST": "identify-eu-west-1.acrcloud.com",
           "ACRCLOUD_ACCESS_KEY": "test-access", "ACRCLOUD_ACCESS_SECRET": "test-secret"}
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

    def test_audio_model_title_is_emitted_as_catalogue_metadata(self):
        candidate = music.MusicCandidate(film=None, track=None, confidence="unknown",
                                         evidence="Une melodie incertaine.", title="Une fanfare encore à découvrir")
        reply = music.recognition_reply(candidate)
        self.assertIn('```workspacetitle', reply)
        self.assertIn(json.dumps({"title": candidate.title}, ensure_ascii=False), reply)
        self.assertNotIn("Confirme", reply)
        with self.assertRaises(ValueError):
            music.MusicCandidate(film=None, track=None, confidence="unknown", evidence="Noise", title="x" * 91)


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


class ComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def test_acrcloud_signed_multipart_contains_exact_same_wav(self):
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(200, json={"status": {"code": 0}, "metadata": {"music": [
                {"title": "Concerning Hobbits", "artists": [{"name": "Howard Shore"}],
                 "album": {"name": "The Fellowship of the Ring"}, "score": 98},
                {"title": "Another suggestion"},
            ]}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.dict(os.environ, ACR_ENV), patch.object(music.httpx, "AsyncClient", return_value=client), patch.object(music.time, "time", return_value=123456):
            result = await music.recognize_acrcloud(PCM)
        self.assertEqual(result.title, "Concerning Hobbits")
        self.assertEqual(str(calls[0].url), "https://identify-eu-west-1.acrcloud.com/v1/identify")
        self.assertIn("multipart/form-data", calls[0].headers["content-type"])
        wav = music.pcm_to_wav(PCM)
        self.assertIn(wav, calls[0].content)
        signature = base64.b64encode(hmac.new(b"test-secret", b"POST\n/v1/identify\ntest-access\naudio\n1\n123456", hashlib.sha1).digest())
        for name, value in (("signature", signature), ("sample_bytes", str(len(wav)).encode()),
                            ("data_type", b"audio"), ("timestamp", b"123456"), ("access_key", b"test-access")):
            self.assertIn(f'name="{name}"\r\n\r\n'.encode() + value + b"\r\n", calls[0].content)
        self.assertNotIn(b"test-secret", calls[0].content)

    async def test_acrcloud_no_match_and_humming_metadata(self):
        for payload, expected in (
            ({"status": {"code": 1001}}, None),
            ({"status": {"code": 0}, "metadata": {"humming": [{"title": "A hummed theme", "score": "0.88"}]}}, "A hummed theme"),
        ):
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
            with patch.dict(os.environ, ACR_ENV), patch.object(music.httpx, "AsyncClient", return_value=client):
                result = await music.recognize_acrcloud(PCM)
            self.assertEqual(result.title if result else None, expected)

    async def test_acrcloud_errors_are_not_no_match_or_secret_dumps(self):
        for payload in ({"status": {"code": 3014, "msg": "test-secret"}},
                        {"status": {"code": 0}}, {"status": {"code": 0}, "metadata": {"music": [{"title": 123}]}}):
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
            with patch.dict(os.environ, ACR_ENV), patch.object(music.httpx, "AsyncClient", return_value=client):
                result = await music._music_provider(PCM, "acrcloud")
            self.assertIsNone(result.candidate)
            self.assertTrue(result.error)
            self.assertNotIn("test-secret", result.error)

    async def test_acrcloud_invalid_and_partial_config_never_make_request(self):
        for host in ("evil.example", "identify-eu-west-1.acrcloud.com/path",
                     "identify-eu-west-1.acrcloud.com:443", "user@identify-eu-west-1.acrcloud.com"):
            with patch.dict(os.environ, {**ACR_ENV, "ACRCLOUD_HOST": host}), patch.object(music.httpx, "AsyncClient") as client:
                with self.assertRaises(ValueError):
                    await music.recognize_acrcloud(PCM)
                client.assert_not_called()
        with patch.dict(os.environ, {**ACR_ENV, "ACRCLOUD_ACCESS_SECRET": ""}):
            self.assertTrue(music.acrcloud_enabled())
            result = await music._music_provider(PCM, "acrcloud")
        self.assertIn("Configure ACRCLOUD", result.error)

    async def test_both_providers_start_before_either_finishes_and_llm_preserves_titles(self):
        gpt_started, acr_started = asyncio.Event(), asyncio.Event()

        async def gpt(pcm):
            self.assertEqual(pcm, PCM)
            gpt_started.set()
            await acr_started.wait()
            return CANDIDATE

        async def acr(pcm):
            self.assertEqual(pcm, PCM)
            acr_started.set()
            await gpt_started.wait()
            return music.ACRCloudTrack(title="Concerning Hobbits", score=98)

        presenter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=json.dumps({
            "summary": "GPT propose un theme spatial; ACRCloud propose une autre piste. Choisis.",
            "acrcloud_film": "Le Seigneur des Anneaux", "acrcloud_evidence": "Association au film a confirmer.",
        }))))
        with patch.object(music, "recognize_music", side_effect=gpt), patch.object(music, "recognize_acrcloud", side_effect=acr), patch.object(music, "Agent", return_value=presenter) as wrapper:
            result = await asyncio.wait_for(music.recognize_music_comparison(PCM, SimpleNamespace(client="existing-client")), timeout=2)
        self.assertEqual(result.gpt_audio.candidate, CANDIDATE)
        self.assertEqual(result.acrcloud.candidate.track, "Concerning Hobbits")
        self.assertEqual(result.acrcloud.candidate.film, "Le Seigneur des Anneaux")
        self.assertEqual(result.acrcloud.metadata.score, 98)
        self.assertEqual(wrapper.call_args.kwargs["client"], "existing-client")
        self.assertEqual(wrapper.call_args.kwargs["default_options"]["tool_choice"], "none")
        self.assertNotIn("tools", wrapper.call_args.kwargs)
        self.assertNotIn("test-secret", presenter.run.call_args.args[0])
        self.assertIn("Choisis", music.comparison_reply(result))

    async def test_failed_provider_and_failed_presenter_keep_other_result(self):
        for source in ("gpt_audio", "acrcloud"):
            with patch.object(music, "recognize_music", AsyncMock(side_effect=ValueError("bad") if source == "gpt_audio" else None, return_value=CANDIDATE)), \
                 patch.object(music, "recognize_acrcloud", AsyncMock(side_effect=ValueError("bad") if source == "acrcloud" else None, return_value=music.ACRCloudTrack(title="A track"))), \
                 patch.object(music, "Agent", return_value=SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text="invalid JSON")))):
                result = await music.recognize_music_comparison(PCM, SimpleNamespace(client=None))
            self.assertTrue(getattr(result, source).error)
            other = result.acrcloud if source == "gpt_audio" else result.gpt_audio
            self.assertIsNotNone(other.candidate)
            self.assertTrue(result.summary_error)
            self.assertIn("Indisponible", music.comparison_reply(result))

    async def test_comparison_cancellation_cancels_both_provider_requests(self):
        entered = [asyncio.Event(), asyncio.Event()]
        cancelled = [False, False]

        async def wait(index):
            entered[index].set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled[index] = True

        async def gpt(pcm):
            await wait(0)

        async def acr(pcm):
            await wait(1)

        with patch.object(music, "recognize_music", side_effect=gpt), patch.object(music, "recognize_acrcloud", side_effect=acr):
            task = asyncio.create_task(music.recognize_music_comparison(PCM, SimpleNamespace(client=None)))
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(cancelled, [True, True])

    async def test_azure_authentication_failure_does_not_hide_acrcloud(self):
        with patch.object(music, "recognize_music", AsyncMock(side_effect=ClientAuthenticationError("private-token"))), \
             patch.object(music, "recognize_acrcloud", AsyncMock(return_value=music.ACRCloudTrack(title="A catalogue track"))), \
             patch.object(music, "Agent", return_value=SimpleNamespace(run=AsyncMock(side_effect=ClientAuthenticationError("private-token")))):
            result = await music.recognize_music_comparison(PCM, SimpleNamespace(client=None))
        self.assertIn("Authentification Azure", result.gpt_audio.error)
        self.assertEqual(result.acrcloud.candidate.track, "A catalogue track")
        self.assertTrue(result.summary_error)
        self.assertNotIn("private-token", result.model_dump_json())

    async def test_real_framework_presenter_is_tool_free_and_preserves_provider_data(self):
        calls = []

        class LocalTextClient:
            async def get_response(self, messages, *, options=None, **kwargs):
                calls.append((messages, options))
                text = json.dumps({"summary": "Deux propositions a choisir.", "acrcloud_film": None,
                                   "acrcloud_evidence": "Film inconnu.", "title": "Deux pistes musicales a comparer"})
                return ChatResponse(messages=[Message(role="assistant", contents=[Content.from_text(text)])])

        agent = Agent(client=LocalTextClient(), instructions="Original Blender instructions")
        with patch.object(music, "recognize_music", AsyncMock(return_value=CANDIDATE)), \
             patch.object(music, "recognize_acrcloud", AsyncMock(return_value=music.ACRCloudTrack(title="A catalogue track", score="0.88"))):
            result = await music.recognize_music_comparison(PCM, agent)
        self.assertIsNone(result.summary_error)
        self.assertEqual(result.acrcloud.candidate.track, "A catalogue track")
        self.assertIsNone(result.acrcloud.candidate.film)
        self.assertEqual(result.acrcloud.metadata.score, "0.88")
        self.assertEqual(calls[0][1]["tool_choice"], "none")
        self.assertFalse(calls[0][1]["store"])
        self.assertNotIn("Original Blender instructions", str(calls[0][0]))
        self.assertIn("```workspacetitle", music.comparison_reply(result))

    def test_track_only_match_can_be_selected_without_inventing_a_film(self):
        candidate = music.MusicCandidate(film=None, track="A known melody", confidence="low", evidence="Catalogue match.")
        request = music.MusicRequest(candidate=candidate, source="acrcloud", action="generate")
        self.assertIn("do not invent a film", music.music_instructions(request))


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

    async def test_comparison_commits_both_results_without_selecting_or_creating(self):
        comparison = music.MusicComparison(
            gpt_audio=music.MusicProviderResult(candidate=CANDIDATE),
            acrcloud=music.MusicProviderResult(error="Quota depasse."),
            summary="Choisis une proposition.",
        )
        with patch.dict(os.environ, {**ENV, **ACR_ENV}), patch.object(music, "recognize_music_comparison", AsyncMock(return_value=comparison)) as recognize, patch.object(self.session, "_run_turn", AsyncMock()) as blender, patch.object(vp, "_is_hosted", return_value=True):
            await self.session.on_control({"type": "start", "mode": "music"})
            await self.session.on_audio(PCM)
            await self.session.on_control({"type": "commit"})
            await self.session._turn_task
        recognize.assert_awaited_once_with(PCM, self.session._agent)
        blender.assert_not_awaited()
        frame = next(frame for frame in self.frames if frame["type"] == "music_comparison")
        self.assertEqual(frame["comparison"], comparison.model_dump())
        self.assertNotIn("selected", frame)
        self.assertIn("GPT Audio", self.frames[-1]["reply"])
        self.assertIn("ACRCloud", self.frames[-1]["reply"])
        self.assertTrue(self.frames[-1]["history_commit_required"])

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
        request = music.MusicRequest(candidate=CANDIDATE, action="generate")
        request._static_preview_ready = True
        token = music.music_scene.set(request)
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
                request._static_preview_ready = True
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


class CinematicSceneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import main
        self.main = main
        self.request = music.MusicRequest(candidate=CANDIDATE, action="generate")
        self.token = music.music_scene.set(self.request)

    def tearDown(self):
        music.music_scene.reset(self.token)

    def test_cinematic_brief_orders_concept_assets_static_preview_then_animation(self):
        prompt = music.music_instructions(self.request)
        stages = ("1. CONCEPT:", "2. ASSET SCOUTING:", "3. STATIC SCENE:", "4. STATIC PREVIEW:", "5. ANIMATION:")
        self.assertEqual([prompt.index(stage) for stage in stages], sorted(prompt.index(stage) for stage in stages))
        for requirement in ("AT MOST 3", "foreground/midground/background", "rim/fill",
                            "recognizable even in Clay", "Do NOT add new animation",
                            "preview_static_scene ONCE", "EXACTLY 15 seconds", "awaiting_render_approval"):
            self.assertIn(requirement, prompt)
        self.assertIn("No music generation or reuse", prompt)
        self.assertIn("not a bare platform", prompt)

    def test_budget_is_three_total_across_models_and_textures_and_resets_next_turn(self):
        with patch.object(self.main.httpx, "Client") as factory:
            client = factory.return_value.__enter__.return_value
            client.post.return_value.json.return_value = {"Result": {}}
            client.get.return_value.json.return_value = {}
            self.assertEqual(self.main.list_available_models(query="dinosaur"), "[]")
            self.assertEqual(self.main.list_available_textures(query="jungle"), "[]")
            self.assertEqual(self.main.list_available_models(query="jeep"), "[]")
            self.assertIn("three asset searches", self.main.list_available_models(query="gate"))
            self.assertEqual(factory.call_count, 3)
            self.assertEqual(self.request._asset_searches, 3)
            self.assertNotIn("_asset_searches", self.request.model_dump())
            token = music.music_scene.set(music.MusicRequest(candidate=CANDIDATE, action="generate"))
            try:
                self.assertEqual(self.main.list_available_models(query="dinosaur"), "[]")
            finally:
                music.music_scene.reset(token)
            self.assertEqual(factory.call_count, 4)
            token = music.music_scene.set(None)
            try:
                for _ in range(4):
                    self.main.list_available_models(query="chair")
            finally:
                music.music_scene.reset(token)
            self.assertEqual(factory.call_count, 8, "Ordinary browsing has no music budget")

    def test_search_failures_consume_budget_and_late_searches_are_blocked(self):
        with patch.object(self.main.httpx, "Client", side_effect=httpx.ConnectError("offline")) as factory:
            for _ in range(3):
                self.assertTrue(self.main.list_available_models(query="dinosaur").startswith("ERROR:"))
            self.assertIn("three asset searches", self.main.list_available_models(query="jeep"))
            self.assertEqual(factory.call_count, 3)
        self.request._static_preview_ready = True
        with patch.object(self.main.httpx, "Client") as factory:
            self.assertIn("already previewed", self.main.list_available_textures(query="rock"))
            factory.assert_not_called()

    async def test_static_preview_is_required_and_a_failed_capture_does_not_unlock_animation(self):
        with patch("media_control.get_blender_connection") as blender:
            with self.assertRaisesRegex(ValueError, "preview_static_scene"):
                apply_camera_path([], duration_seconds=15, fps=25)
            with self.assertRaisesRegex(ValueError, "preview_static_scene"):
                await start_animation_render(duration_seconds=15, fps=25, mode="standard")
            blender.assert_not_called()
        with patch.object(self.main, "get_viewport_screenshot", return_value="Screenshot error: capture failed"):
            with self.assertRaisesRegex(ValueError, "preview failed"):
                self.main.preview_static_scene()
        self.assertFalse(self.request._static_preview_ready)
        image = "Screenshot captured (800x450 pixels).\n\n![screenshot](https://teststorage.blob.core.windows.net/static.png)"
        with patch.object(self.main, "get_viewport_screenshot", return_value=image) as capture:
            self.assertEqual(self.main.preview_static_scene(), image)
            capture.assert_called_once_with(max_size=800)
        self.request.require_static_preview()
        keys = [{"time": 0, "position": [6, -8, 4], "target": [0, 0, 1], "lens": 35},
                {"time": 15, "position": [4, -6, 3], "target": [0, 0, 1], "lens": 40}]
        with patch("media_control.get_blender_connection") as blender:
            blender.return_value.send_command.return_value = {"success": True}
            self.assertIn("true", apply_camera_path(keys, duration_seconds=15, fps=25))
            blender.return_value.send_command.assert_called_once()

    def test_model_import_cannot_run_without_thumbnail_but_ordinary_import_is_unchanged(self):
        url = "https://models.example/dinosaur.glb"
        with patch.object(self.main, "get_blender_connection") as blender:
            self.assertIn("thumbnail", self.main.download_model(model_url=url, name="dinosaur"))
            blender.assert_not_called()
            self.request._previewed_model_urls.add(url)
            blender.return_value.send_command.return_value = {"success": True, "imported_objects": ["dinosaur"]}
            self.assertIn("preview_static_scene", self.main.download_model(model_url=url, name="dinosaur"))
            token = music.music_scene.set(None)
            try:
                self.assertIn("single viewport screenshot", self.main.download_model(model_url=url, name="dinosaur"))
            finally:
                music.music_scene.reset(token)
            self.assertEqual(blender.return_value.send_command.call_count, 2)

    async def test_thumbnail_and_static_screenshot_are_streamed_before_import_and_animation(self):
        model = {"name": "Original dinosaur", "imageUrl": "https://models.example/dinosaur.png",
                 "modelUrl": "https://models.example/dinosaur.glb"}
        call = Content.from_function_call
        result = Content.from_function_result

        def update(content):
            return AgentResponseUpdate(role="assistant", contents=[content])

        async def source():
            yield update(Content.from_text("Je propose une aventure de dinosaures dans une jungle miniature."))
            yield update(call("search", "list_available_models", arguments={"query": "dinosaur"}))
            yield update(result("search", result=json.dumps([model])))
            yield update(result("search", result=json.dumps([model])))
            yield update(call("import", "download_model", arguments=json.dumps({"model_url": model["modelUrl"], "name": "dinosaur"})))
            self.assertIn(model["modelUrl"], self.request._previewed_model_urls)
            yield update(result("import", result="Imported dinosaur."))
            yield update(call("static", "execute_blender_code", arguments={"code": "build_static_scene()"}))
            yield update(result("static", result="Static scene built."))
            yield update(call("preview", "preview_static_scene", arguments={}))
            self.request._static_preview_ready = True
            yield update(result("preview", result="Screenshot captured.\n\n![screenshot](https://teststorage.blob.core.windows.net/static.png)"))
            yield update(result("preview", result="Screenshot captured.\n\n![screenshot](https://teststorage.blob.core.windows.net/static.png)"))
            yield update(call("animate", "execute_blender_code", arguments={"code": "animate_objects()"}))
            yield update(call("camera", "apply_camera_path", arguments={}))
            yield update(call("snapshot", "start_animation_render", arguments={}))

        context = SimpleNamespace(stream=True, result=ResponseStream(source(), finalizer=AgentResponse.from_updates), session=None)
        await self.main.ToolStatusMiddleware().process(context, AsyncMock())
        updates = [entry async for entry in context.result]
        text = "\n".join(entry.text for entry in updates if entry.text)
        self.assertEqual(text.count(model["imageUrl"]), 1)
        self.assertEqual(text.count("![screenshot]"), 1)
        self.assertIn("Construction de la scène statique", text)
        self.assertIn("Conception et animation des objets", text)
        self.assertIn("fichier Blender animé à inspecter", text)
        thumbnail = next(i for i, entry in enumerate(updates) if model["imageUrl"] in entry.text)
        import_call = next(i for i, entry in enumerate(updates) if any(c.type == "function_call" and c.name == "download_model" for c in entry.contents))
        screenshot = next(i for i, entry in enumerate(updates) if "![screenshot]" in entry.text)
        animation = next(i for i, entry in enumerate(updates) if any(c.type == "function_call" and c.call_id == "animate" for c in entry.contents))
        self.assertLess(thumbnail, import_call)
        self.assertLess(screenshot, animation)


if __name__ == "__main__":
    unittest.main()
