"""Smoke tests for voice history, identity propagation, and TTS lifecycle.

Runs without Azure credentials or Speech SDK calls:

    python devTools/test_voice_pipeline.py
"""

import asyncio
import os
import sys
from types import MethodType

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
    test_conversation_trace_attributes()
    test_nested_agent_telemetry_conversation()
    test_hosted_conversation_disables_nested_storage()
    await test_tts_completion_order()
    print("\nFAILURES PRESENT" if _check.failed else "\nall checks passed")
    return 1 if _check.failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
