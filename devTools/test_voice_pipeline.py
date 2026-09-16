"""Smoke tests for voice history, identity propagation, and TTS lifecycle.

Runs without Azure credentials or Speech SDK calls:

    python devTools/test_voice_pipeline.py
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
from types import MethodType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_pipeline as vp  # noqa: E402
from conversation_telemetry import FoundryConversationTelemetryAgent  # noqa: E402


async def _noop(*_args, **_kwargs):
    return None


def _session() -> vp.VoiceSession:
    return vp.VoiceSession(
        agent=None,
        send_text=_noop,
        send_bytes=_noop,
        session_id="browser-conversation",
        call_id="call-1",
        user_id="user-1",
    )


def _check(label, actual, expected):
    result = "ok  " if actual == expected else "FAIL"
    print(f"[{result}] {label}: {actual!r}")
    if actual != expected:
        _check.failed = True


_check.failed = False


def test_hosted_conversation_payload():
    session = _session()
    session._agent_session_id = "session-foundry"
    session._foundry_conversation_id = "conv_shared"
    session._previous_response_id = "resp_typed"
    session._fallback_history = [{"role": "user", "content": "stale"}]

    original = vp._is_hosted
    vp._is_hosted = lambda: True
    try:
        body = session._build_agent_request("voice turn")
    finally:
        vp._is_hosted = original

    _check("hosted uses shared conversation", body.get("conversation"), "conv_shared")
    _check("hosted uses shared session", body.get("agent_session_id"), "session-foundry")
    _check("hosted sends only new input", body.get("input"), "voice turn")
    _check("conversation wins over response chain", "previous_response_id" in body, False)


def test_local_response_chain_payload():
    session = _session()
    session._previous_response_id = "resp_local"

    original = vp._is_hosted
    vp._is_hosted = lambda: False
    try:
        body = session._build_agent_request("voice turn")
    finally:
        vp._is_hosted = original

    _check("local advances response chain", body.get("previous_response_id"), "resp_local")
    _check("local binds browser session", body.get("agent_session_id"), "browser-conversation")


def test_hosted_fallback_payload():
    session = _session()
    session._agent_session_id = "session-foundry"
    session._previous_response_id = "resp_typed"
    session._fallback_history = [
        {"role": "user", "content": "voice one"},
        {"role": "assistant", "content": "reply one"},
    ]

    original = vp._is_hosted
    vp._is_hosted = lambda: True
    try:
        body = session._build_agent_request("voice two")
    finally:
        vp._is_hosted = original

    _check("fallback retains typed branch", body.get("previous_response_id"), "resp_typed")
    _check("fallback replays voice turns", len(body.get("input", [])), 3)


def test_loopback_identity_headers():
    headers = _session()._build_loopback_headers()
    _check("call id forwarded", headers.get(vp.FOUNDRY_CALL_ID_HEADER), "call-1")
    _check("user id forwarded", headers.get(vp.FOUNDRY_USER_ID_HEADER), "user-1")


async def test_voice_media_scope():
    from agent_framework import AgentResponse, AgentResponseUpdate, Content, Message, ResponseStream
    from media_control import MediaMiddleware, decode_envelope, encode_voice_input, require_scope
    from video_jobs import video_scope

    secret = "offline-voice-secret-" * 3
    scope = "a" * 32

    def signed(value):
        part = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        signature = hmac.new(secret.encode(), part.encode(), hashlib.sha256).hexdigest()
        return f"BLENDER_MEDIA_V1:{part}.{signature}"

    with patch.dict(os.environ, {"MEDIA_CONTROL_SECRET": secret}), patch.object(vp, "_is_hosted", return_value=False):
        session = _session()
        context_token = signed({"scope": scope, "text": "Voice request follows."})
        await session.on_control({"type": "context", "media_context": context_token})
        body = session._build_agent_request("Render the prepared animation")
        _check("voice request has verified typed-chat media scope", decode_envelope(body["input"]),
               {"scope": scope, "text": "Render the prepared animation"})
        _check("media context preserves sandbox affinity", body["agent_session_id"], "browser-conversation")

        class Inner:
            def _get_conversation_id(self, _context):
                return None

            async def process(self, context, _call_next):
                _check("loopback tools receive voice media scope", require_scope(), scope)
                _check("model receives only transcript", context.messages[0].contents[0].text, "Render the prepared animation")

        context = SimpleNamespace(messages=[Message(role="user", contents=[Content.from_text(body["input"])])], stream=False)
        await MediaMiddleware(Inner()).process(context, _noop)
        _check("media scope released after voice turn", video_scope.get(), None)

        class StreamingInner(Inner):
            async def process(self, context, _call_next):
                async def updates():
                    _check("streamed voice tools receive media scope", require_scope(), scope)
                    yield AgentResponseUpdate(contents=[Content.from_text("reply")], role="assistant")

                context.result = ResponseStream(updates(), finalizer=AgentResponse.from_updates)

        spoken_action = signed({"scope": scope, "text": "Approve", "action": {"type": "approve", "job_id": "b" * 32}})
        for transcript in ["Render the prepared animation", spoken_action]:
            request = session._build_agent_request(transcript)
            context = SimpleNamespace(messages=[Message(role="user", contents=[Content.from_text(request["input"])])], stream=True)
            with patch("media_control.control_action") as action:
                await MediaMiddleware(StreamingInner()).process(context, _noop)
                _check("stream initialization releases scope", video_scope.get(), None)
                updates = [update async for update in context.result]
                _check("streamed voice response is consumed", len(updates), 1)
                _check("streamed voice retains literal transcript", context.messages[0].contents[0].text, transcript)
                action.assert_not_called()
            _check("stream completion releases scope", video_scope.get(), None)

        with patch.object(vp, "_is_hosted", return_value=True):
            session._fallback_history = [{"role": "user", "content": "earlier turn"}]
            fallback = session._build_agent_request("Continue animation")["input"]
            _check("fallback voice history preserves media scope", decode_envelope(fallback[-1]["content"])["scope"], scope)
            session._foundry_conversation_id = "conv_shared"
            hosted = session._build_agent_request("Continue animation")
            _check("hosted voice preserves media scope", decode_envelope(hosted["input"])["scope"], scope)
            _check("hosted voice retains transcript continuity", hosted["conversation"], "conv_shared")

        nested = encode_voice_input(context_token, context_token)
        _check("spoken envelope remains plain transcript", decode_envelope(nested)["text"], context_token)
        for label, invalid in [
            ("tampered context", context_token + "0"),
            ("paid action context", signed({"scope": scope, "text": "Voice request follows.", "action": {"type": "approve", "job_id": "b" * 32}})),
            ("reference context", signed({"scope": scope, "text": "Voice request follows.", "references": []})),
            ("ordinary chat envelope", signed({"scope": scope, "text": "another request"})),
        ]:
            try:
                encode_voice_input("Render", invalid)
            except ValueError:
                rejected = True
            else:
                rejected = False
            _check(f"voice rejects {label}", rejected, True)

        await session.on_control({"type": "context"})
        _check("missing context clears previous media authority", session._media_context, None)
        try:
            session._build_agent_request(spoken_action)
        except ValueError:
            rejected = True
        else:
            rejected = False
        _check("unscoped voice cannot submit a signed control", rejected, True)


def test_conversation_trace_attributes():
    expected = {
        "gen_ai.conversation.id": "conv_shared",
        "azure.ai.agentserver.conversation_id": "conv_shared",
    }
    _check(
        "voice spans carry Foundry conversation",
        vp._conversation_span_attributes("conv_shared"),
        expected,
    )
    _check(
        "missing conversation adds no trace attributes",
        vp._conversation_span_attributes(None),
        {},
    )


def test_nested_agent_telemetry_conversation():
    from azure.ai.agentserver.core import (
        FoundryAgentRequestContext,
        reset_request_context,
        set_request_context,
    )

    agent = object.__new__(FoundryConversationTelemetryAgent)
    agent.bind_telemetry_conversation("session-foundry", "conv_shared")
    token = set_request_context(
        FoundryAgentRequestContext(
            call_id=None,
            user_id=None,
            session_id="session-foundry",
        )
    )
    try:
        _check(
            "nested agent spans resolve Foundry conversation",
            agent._get_otel_conversation_id(None),
            "conv_shared",
        )
    finally:
        reset_request_context(token)

    agent.unbind_telemetry_conversation("session-foundry", "conv_shared")
    _check(
        "closed voice connection releases telemetry binding",
        getattr(agent, "_telemetry_conversation_bindings", {}),
        {},
    )


def test_hosted_conversation_disables_nested_storage():
    session = _session()
    session._foundry_conversation_id = "conv_shared"

    original = vp._is_hosted
    vp._is_hosted = lambda: True
    try:
        body = session._build_agent_request("voice turn")
    finally:
        vp._is_hosted = original

    _check("hosted voice disables nested response storage", body.get("store"), False)


async def test_tts_completion_order():
    frames = []

    async def send_text(obj):
        frames.append(obj)

    session = vp.VoiceSession(
        agent=None,
        send_text=send_text,
        send_bytes=_noop,
        session_id="browser-conversation",
    )
    session._previous_response_id = "resp_local"

    async def fake_stream_agent(self, _transcript, _state):
        return "A short reply."

    async def fake_synthesize(self, _sentence):
        if not self._speaking:
            self._speaking = True
            await self._send_text({"type": "speaking_start"})

    session._stream_agent = MethodType(fake_stream_agent, session)
    session._synthesize_and_stream = MethodType(fake_synthesize, session)

    original = vp._is_hosted
    vp._is_hosted = lambda: False
    try:
        await session._run_turn_inner("hello")
    finally:
        vp._is_hosted = original

    frame_types = [frame.get("type") for frame in frames]
    _check("normal TTS lifecycle order", frame_types, ["speaking_start", "speaking_end", "done"])
    _check("local done returns continuation", frames[-1].get("response_id"), "resp_local")


async def main():
    test_hosted_conversation_payload()
    test_local_response_chain_payload()
    test_hosted_fallback_payload()
    test_loopback_identity_headers()
    await test_voice_media_scope()
    test_conversation_trace_attributes()
    test_nested_agent_telemetry_conversation()
    test_hosted_conversation_disables_nested_storage()
    await test_tts_completion_order()
    print("\nFAILURES PRESENT" if _check.failed else "\nall checks passed")
    return 1 if _check.failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
