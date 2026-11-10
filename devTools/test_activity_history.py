"""Smoke test for Activity-path turn serialisation and transcript handling.

Runs without the hosting SDK: `_run_turn` only touches the agent, the turn
context and the TurnState through duck-typed attributes, so stubs are enough.

    python devTools/test_activity_history.py
"""

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import activity_bridge as ab  # noqa: E402
import teams_media as tm
import video_jobs as vj
import media_analysis as ma
from artifact_storage import conversation_scope

_supports_streaming = ab._supports_streaming
_FallbackEmitter = ab._FallbackEmitter


class _Activity(SimpleNamespace):
    def __init__(self, **values):
        super().__init__(**{"text": "", "attachments": [], "reply_to_id": None, **values})

    def apply_conversation_reference(self, reference):
        self.conversation = reference.conversation


class _Scope:
    """Stands in for one TurnState scope (ConversationState)."""

    def __init__(self, backing: dict) -> None:
        self._backing = backing

    def get_value(self, key):
        return self._backing.get(key)

    def set_value(self, key, value):
        self._backing[key] = value


class _State:
    def __init__(self, backing: dict) -> None:
        # A fresh snapshot per turn, like the SDK loading state per request.
        self.conversation = _Scope(dict(backing))
        self.saved = 0

    async def save(self, _context, force=False):
        self.saved += 1


class _Context:
    def __init__(self, conversation_id: str, text: str = "", value=None) -> None:
        self.activity = SimpleNamespace(
            id=None,
            text=text,
            value=value,
            attachments=[],
            conversation=SimpleNamespace(id=conversation_id),
            recipient=SimpleNamespace(id="28:agent"),
            channel_id="emulator",
            delivery_mode=None,
            service_url="https://smba.trafficmanager.net/teams/",
        )
        self.identity = SimpleNamespace(is_authenticated=True, get_token_scope=lambda: ["connector-scope"])
        self.adapter = SimpleNamespace(CHANNEL_SERVICE_FACTORY_KEY="factory", OAUTH_SCOPE_KEY="audience")
        self.turn_state = {"audience": "connector-audience"}
        self.activity.get_conversation_reference = lambda: SimpleNamespace(
            service_url=self.activity.service_url, conversation=self.activity.conversation)
        self.sent: list = []

    def remove_recipient_mention(self):
        return self.activity.text

    async def send_activity(self, message):
        self.sent.append(message)


class _Agent:
    """Records the transcript it was handed, and how long each turn overlapped."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.seen: list[list[str]] = []
        self.concurrent = 0
        self.max_concurrent = 0

    def run(self, messages, stream=True, options=None):
        agent = self

        async def _gen():
            agent.concurrent += 1
            agent.max_concurrent = max(agent.max_concurrent, agent.concurrent)
            try:
                agent.seen.append([m.contents[0].text for m in messages])
                await asyncio.sleep(agent.delay)
                yield SimpleNamespace(
                    message_id="msg",
                    contents=[SimpleNamespace(type="text", text=f"reply-{len(agent.seen)}")],
                )
            finally:
                agent.concurrent -= 1

        return _gen()


def _patch_framework():
    """Provide the two agent_framework names `_run_turn` needs."""
    ab.Content = SimpleNamespace(from_text=lambda t: SimpleNamespace(type="text", text=t))
    ab.Message = lambda role, contents: SimpleNamespace(role=role, contents=contents)
    ab.Activity = _Activity
    ab.Attachment = lambda **values: SimpleNamespace(**values)
    ab._supports_streaming = lambda _context: False

    class _Emitter(ab._Emitter):
        async def status(self, text):
            self._last_status = text

        async def text(self, chunk):
            self._context.sent.append(chunk)

        async def card(self, attachment):
            self._context.sent.append(attachment)

        async def keepalive(self):
            pass

        async def finish(self):
            pass

    ab._FallbackEmitter = _Emitter
    ab._StreamingEmitter = _Emitter


def _reset(conversation_id: str):
    ab._conversations.pop(conversation_id, None)


def _check(label: str, actual, expected):
    status = "ok  " if actual == expected else "FAIL"
    print(f"[{status}] {label}: {actual!r}")
    if actual != expected:
        _check.failed = True


_check.failed = False


async def test_serialised_turns():
    """Two overlapping messages must not run at once, and must chain history."""
    _reset("c-serial")
    agent = _Agent(delay=0.15)
    backing: dict = {}
    ctx_a, ctx_b = _Context("c-serial", "first"), _Context("c-serial", "second")
    state_a, state_b = _State(backing), _State(backing)

    await asyncio.gather(
        ab._run_turn(agent, ctx_a, state_a),
        ab._run_turn(agent, ctx_b, state_b),
    )

    _check("turns never overlapped", agent.max_concurrent, 1)
    _check("first turn saw only its own message", agent.seen[0], ["first"])
    _check(
        "second turn replayed the first exchange",
        agent.seen[1],
        ["first", "reply-1", "second"],
    )


async def test_clear_discards_inflight():
    """/clear during a turn must not let that turn write itself back."""
    _reset("c-clear")
    agent = _Agent(delay=0.2)
    backing: dict = {}
    turn = asyncio.create_task(
        ab._run_turn(agent, _Context("c-clear", "build a cabin"), _State(backing))
    )
    await asyncio.sleep(0.05)
    ab._clear_conversation(_State(backing), ab._conversation_slot("c-clear"))
    await turn

    _check("transcript is empty after /clear", ab._conversation_slot("c-clear").history, [])


async def test_card_submit_transcript():
    """The stored transcript keeps the user's intent, not the tool scaffolding."""
    _reset("c-card")
    agent = _Agent()
    value = {"action": "load_model", "name": "Oak Tree", "modelUrl": "https://x/y.glb"}
    await ab._run_turn(agent, _Context("c-card", "", value), _State({}))

    prompt = agent.seen[0][-1]
    stored = ab._conversation_slot("c-card").history[0]["text"]
    _check("model received the tool scaffolding", "download_model" in prompt, True)
    _check("transcript kept it out", "download_model" in stored, False)
    _check("transcript kept the intent", stored, 'Add the 3D model "Oak Tree" to the scene.')


async def test_detach_race():
    """Output produced while the live response is closing must not kill the turn.

    Regression for the production failure where a status update landed during
    `_Relay.detach`'s two network round trips, hit the stream that was being
    closed, and raised `RuntimeError: The stream has already ended` — aborting
    the very turn the handover exists to keep alive.
    """

    class _Dying:
        """A live stream that dies partway through `finish()`."""

        def __init__(self) -> None:
            self.ended = False
            self.sent: list = []
            self._started = 0.0
            self._last_status = ""

        def _guard(self) -> None:
            if self.ended:
                raise RuntimeError("The stream has already ended.")

        async def status(self, text):
            self._guard()
            self.sent.append(("status", text))

        async def text(self, chunk):
            self._guard()
            self.sent.append(("text", chunk))

        async def finish(self):
            # Mirrors the SDK: `end_stream()` marks the stream closed partway
            # through and then keeps awaiting network I/O. The gap between the
            # two is the window the turn task used to slip into — modelling the
            # close as happening when finish() *returns* makes this test vacuous,
            # because the pre-fix swap happened immediately after with no await
            # in between.
            await asyncio.sleep(0.02)
            self.ended = True
            await asyncio.sleep(0.05)

    class _Proactive:
        def __init__(self, _context, previous) -> None:
            self.buffered: list = []
            self._started = previous._started

        async def status(self, text):
            self.buffered.append(("status", text))

        async def text(self, chunk):
            self.buffered.append(("text", chunk))

    original = ab._ProactiveEmitter
    ab._ProactiveEmitter = _Proactive
    try:
        dying = _Dying()
        relay = ab._Relay(dying)
        detach = asyncio.create_task(relay.detach(object()))
        await asyncio.sleep(0.04)  # after the stream closed, before finish() returns

        error = None
        try:
            await relay.status("Rendering the final image…")
        except Exception as exc:  # noqa: BLE001 - that is what we are asserting on
            error = exc
        await detach

        _check("status during handover did not raise", error, None)
        _check(
            "it was buffered for proactive delivery",
            relay._inner.buffered,
            [("status", "Rendering the final image…")],
        )
        _check(
            "handover notice still went to the live response",
            dying.sent,
            [("text", ab._HANDOVER_TEXT)],
        )
    finally:
        ab._ProactiveEmitter = original


def test_streaming_capabilities():
    cases = [
        ("personal Teams chat", ab.Channels.ms_teams, "personal", False, False, None, True),
        ("Teams channel", ab.Channels.ms_teams, "channel", True, False, None, False),
        ("Teams group chat", ab.Channels.ms_teams, "groupChat", True, False, None, False),
        ("Teams missing conversation type", ab.Channels.ms_teams, None, None, False, None, False),
        ("Teams contradictory group flag", ab.Channels.ms_teams, "personal", True, False, None, False),
        ("Teams channel explicitly requesting streaming", ab.Channels.ms_teams, "channel", True, False, ab.DeliveryModes.stream, False),
        ("M365 Copilot", ab.Channels.ms_teams, "personal", False, True, None, False),
        ("WebChat", ab.Channels.webchat, None, None, False, None, True),
        ("DirectLine", ab.Channels.direct_line, None, None, False, None, True),
        ("other streaming channel", "other", None, None, False, ab.DeliveryModes.stream, True),
        ("ordinary emulator", "emulator", None, None, False, None, False),
    ]
    for label, channel, kind, group, agentic, delivery, expected in cases:
        context = _Context("streaming-capabilities")
        context.activity.channel_id = SimpleNamespace(channel=channel)
        context.activity.conversation.conversation_type = kind
        context.activity.conversation.is_group = group
        context.activity.is_agentic_request = lambda: agentic
        context.activity.delivery_mode = delivery
        _check(f"{label} streaming support", _supports_streaming(context), expected)


async def test_teams_thread_turns():
    class ThreadAgent:
        def __init__(self):
            self.seen = []
            self.scene_keys = []

        async def run(self, messages, stream=True, options=None):
            self.seen.append([m.contents[0].text for m in messages])
            self.scene_keys.append(options["user"])
            yield SimpleNamespace(
                message_id="status-get_scene_info",
                contents=[SimpleNamespace(type="text", text="\n\n*Inspecting the scene...*\n\n")],
            )
            reply = (
                "![viewport](https://files.example/viewport.png)"
                if len(self.seen) == 1
                else "[Download scene](https://files.example/castle.blend)"
            )
            yield SimpleNamespace(
                message_id="answer",
                contents=[SimpleNamespace(type="text", text=reply)],
            )

    conversation = "19:test-channel@thread.tacv2;messageid=100"
    _reset(conversation)
    agent = ThreadAgent()
    contexts = []
    with (
        patch.object(ab, "_supports_streaming", _supports_streaming),
        patch.object(ab, "_FallbackEmitter", _FallbackEmitter),
        patch.object(ab, "_StreamingEmitter", side_effect=AssertionError("Threads must not open a stream")),
    ):
        for index, text in enumerate(["create a fantasy castle", "download the Blender scene"]):
            context = _Context(conversation, text)
            context.activity.id = str(100 + index)
            context.activity.channel_id = ab.Channels.ms_teams
            context.activity.conversation.conversation_type = "channel"
            context.activity.conversation.is_group = True
            context.activity.is_agentic_request = lambda: False
            contexts.append(context)
            await ab._run_turn(agent, context, _State({}))

    first_reply = "![viewport](https://files.example/viewport.png)"
    _check("thread first reply is a complete ordinary message", contexts[0].sent[-1].text, first_reply)
    _check("thread follow-up delivers download", contexts[1].sent[-1].text,
           "[Download scene](https://files.example/castle.blend)")
    _check("thread status is sent without streaming", contexts[1].sent[0], "Inspecting the scene...")
    _check("thread follow-up replays history", agent.seen[1],
           ["create a fantasy castle", first_reply, "download the Blender scene"])
    _check("thread follow-up keeps scene affinity", agent.scene_keys[0], agent.scene_keys[1])

    context = contexts[1]
    send = AsyncMock(return_value=SimpleNamespace(id="result"))
    client = SimpleNamespace(conversations=SimpleNamespace(send_to_conversation=send), close=AsyncMock())
    context.turn_state["factory"] = SimpleNamespace(create_connector_client=AsyncMock(return_value=client))
    relay = ab._Relay(_FallbackEmitter(context))
    await relay.text("Preview ready.")
    await relay.detach(context)
    await relay.text("Render ready.")
    await relay.finish()
    _check("thread handover closes with ordinary message", context.sent[-1].text,
           "Preview ready." + ab._HANDOVER_TEXT)
    _check("thread proactive result stays in original thread", send.await_args.args[0], conversation)
    _check("thread proactive result is delivered", send.await_args.args[1].text, "Render ready.")
    _check("thread proactive client is closed", client.close.await_count, 1)


async def test_native_teams_status():
    context = _Context("19:status@thread.tacv2;messageid=100")
    context.activity.id = "follow-up-200"
    context.activity.channel_id = ab.Channels.ms_teams
    context.activity.is_agentic_request = lambda: False
    send = AsyncMock(side_effect=[SimpleNamespace(id="status-stream"), None, None, None])
    reply = AsyncMock(side_effect=AssertionError("Streaming must post to /activities, not /activities/id"))
    client = SimpleNamespace(
        conversations=SimpleNamespace(send_to_conversation=send, reply_to_activity=reply),
        close=AsyncMock(),
    )
    context.turn_state["factory"] = SimpleNamespace(create_connector_client=AsyncMock(return_value=client))
    emitter = ab._TeamsStatusEmitter(context, ab._Emitter(context))
    with patch.object(ab, "_TEAMS_STATUS_INTERVAL", 0):
        await emitter.status("Inspecting the scene...")
        await emitter.status("Rendering the image...")
        await emitter.text("![render](https://files.example/render.png)")
        await emitter.card(SimpleNamespace(content_type="card"))
        await emitter.finish()
    activities = [call.args[1] for call in send.await_args_list]
    _check("native status uses conversation endpoint", reply.await_count, 0)
    _check("native status retains follow-up anchor", [item.reply_to_id for item in activities],
           ["follow-up-200"] * 3)
    _check("native updates use typing activities", [item.type for item in activities], ["typing", "typing", "message"])
    _check("native initial sequence", activities[0].entities[0],
           {"type": "streaminfo", "streamType": "informative", "streamSequence": 1})
    _check("native subsequent sequence retains stream ID", activities[1].entities[0],
           {"type": "streaminfo", "streamType": "informative", "streamSequence": 2, "streamId": "status-stream"})
    _check("native final omits sequence", activities[2].entities[0],
           {"type": "streaminfo", "streamType": "final", "streamId": "status-stream"})
    _check("native final keeps complete image reply", activities[2].text, "![render](https://files.example/render.png)")
    _check("native final keeps cards", len(activities[2].attachments), 1)
    _check("native status connector clients close", client.close.await_count, 3)

    send.reset_mock(side_effect=True)
    send.return_value = SimpleNamespace(id="after-detach")
    emitter = ab._TeamsStatusEmitter(context, ab._Emitter(context))
    relay = ab._Relay(emitter)
    with patch.object(ab, "_TEAMS_STATUS_INTERVAL", 0):
        await relay.status("Inspecting...")
        await relay.text("Viewport ready.")
        await relay.detach(context)
        _check("native HTTP detach sends no chat message", send.await_count, 1)
        context.send_activity = AsyncMock(side_effect=RuntimeError("Inbound context closed"))
        await relay.status("Rendering...")
        await relay.card(SimpleNamespace(content_type="card"))
        await relay.progress()
        _check("native render progress stays in status control", send.await_count, 2)
        for elapsed in (35, 57, 77, 100, 180, 500):
            now = ab.time.monotonic()
            emitter._started = now - elapsed
            emitter._stream_started = now - elapsed
            await relay.tick()
            await relay.keepalive()
        await relay.text("Render ready.")
        await relay.finish()
        completed_count = send.await_count
        await relay.keepalive()
        await relay.status("Already done")
        await relay.progress()
        await relay.finish()
    activities = [call.args[1] for call in send.await_args_list]
    _check("native turn detaches", relay.detached, True)
    _check("native actions survive detach", activities[1].text, "Rendering...")
    _check("native long-turn keepalives never become chat messages",
           [item.type for item in activities], ["typing"] * 8 + ["message"])
    _check("native long-turn status sequences stay continuous",
           [item.entities[0]["streamSequence"] for item in activities if item.type == "typing"],
           list(range(1, 9)))
    _check("native final after detach preserves the full answer", activities[-1].text,
           "Viewport ready.Render ready.")
    _check("native final after detach preserves cards", len(activities[-1].attachments), 1)
    _check("native final after detach closes the status control", activities[-1].entities,
           [{"type": "streaminfo", "streamType": "final", "streamId": "after-detach"}])
    _check("native completed turn sends nothing else", send.await_count, completed_count)
    _check("native detached turn never reuses inbound context", context.send_activity.await_count, 0)
    _check("native detached result keeps thread", {call.args[0] for call in send.await_args_list},
           {context.activity.conversation.id})


async def test_native_teams_buffered_reply():
    intro = "I'll make six colorful cottages around a looping footpath."
    viewport = "Here's the current viewport:\n\n![viewport](https://files.example/viewport.png)"
    render = "Here's the rendered image:\n\n![render](https://files.example/render.png)"

    for kind in ("personal", "channel", "groupChat"):
        for delivery in ("chunked", "text-first", "call-first"):
            label = f"{kind} {delivery}"
            conversation = f"19:buffered-{kind}-{delivery};messageid=100"
            _reset(conversation)
            tool_started, release_tool = asyncio.Event(), asyncio.Event()
            media_ready, release_render = asyncio.Event(), asyncio.Event()
            completed = asyncio.Event()
            slots = []
            active = None
            informative = []
            interleaved = []
            client_finished = False

            class StagedAgent:
                def run(self, messages, stream=True, options=None):
                    async def generate():
                        def text(message_id, value):
                            return SimpleNamespace(
                                message_id=message_id,
                                contents=[SimpleNamespace(type="text", text=value)],
                            )

                        yield text("scene-status-0", "Preparing the scene...")
                        call = SimpleNamespace(type="function_call", name="execute_blender_code")
                        if delivery != "chunked":
                            yield text("status-execute_blender_code", "Executing Blender code...")
                            prose = SimpleNamespace(type="text", text=intro)
                            yield SimpleNamespace(
                                message_id="plan",
                                contents=[prose, call] if delivery == "text-first" else [call, prose],
                            )
                        else:
                            yield text("plan", intro[:18])
                            _check(f"{label} never publishes a partial sentence",
                                   [call.args[1].text for call in send.await_args_list
                                    if call.args[1].type == "message"], [])
                            yield text("plan", intro[18:])
                            yield text("status-execute_blender_code", "Executing Blender code...")
                            yield SimpleNamespace(message_id="plan", contents=[call])
                        tool_started.set()
                        await release_tool.wait()
                        yield text("status-get_viewport_screenshot", "Capturing viewport screenshot...")
                        yield text("tool-img-viewport", f"\n\n{viewport}\n\n")
                        yield text("render-plan", "I'll render the village next.")
                        yield text("status-render_final", "Rendering the final image...")
                        media_ready.set()
                        await release_render.wait()
                        yield text("tool-img-render", f"\n\n{render}\n\n")
                        yield text("answer", "Created the village.\n\n![viewport](")
                        yield text("answer", "https://files.example/viewport.png)\n\n![render](https://files.example/render.png)")

                    return generate()

            async def post(_conversation_id, activity):
                nonlocal active, client_finished
                if getattr(activity, "entities", None):
                    entity = activity.entities[0]
                    stream_id = entity.get("streamId")
                    if entity["streamType"] == "informative":
                        if stream_id is None:
                            if active is not None:
                                raise RuntimeError("Only one open stream per conversation")
                            active = SimpleNamespace(id=f"status-{len(slots) + 1}", text=None, invalidated=False)
                            slots.append(active)
                        elif active is None or active.id != stream_id or active.invalidated:
                            raise RuntimeError("404: ordinary reply invalidated informative updates")
                        # The service accepts later streams, but Teams no longer
                        # renders their informative updates after the first final.
                        if not client_finished:
                            informative.append((active.id, entity["streamSequence"], activity.text))
                        response_id = active.id
                    else:
                        if active is None or active.id != stream_id:
                            raise RuntimeError("Final references a closed or unknown stream")
                        active.text = activity.text
                        response_id = active.id
                        active = None
                        client_finished = True
                else:
                    if active is not None:
                        active.invalidated = True
                        interleaved.append(activity.text)
                    response_id = f"ordinary-{len(slots) + 1}"
                    slots.append(SimpleNamespace(id=response_id, text=activity.text))
                if "Created the village." in activity.text:
                    completed.set()
                return SimpleNamespace(id=response_id)

            send = AsyncMock(side_effect=post)
            client = SimpleNamespace(
                conversations=SimpleNamespace(
                    send_to_conversation=send,
                    reply_to_activity=AsyncMock(side_effect=AssertionError("Wrong thread endpoint")),
                ),
                close=AsyncMock(),
            )
            context = _Context(conversation, "Create a village and render it.")
            context.activity.id = "user-100"
            context.activity.channel_id = ab.Channels.ms_teams
            context.activity.conversation.conversation_type = kind
            context.activity.conversation.is_group = kind != "personal"
            context.activity.is_agentic_request = lambda: False
            context.turn_state["factory"] = SimpleNamespace(
                create_connector_client=AsyncMock(return_value=client),
            )

            def visible():
                # Finalization fills the slot reserved by the initial update,
                # rather than appending a new message at finalization time.
                return [slot.text for slot in slots if slot.text is not None]

            with (
                patch.object(ab, "_TEAMS_STATUS_INTERVAL", 0),
                patch.object(ab, "_PROACTIVE_AFTER_SECONDS", 0.01),
            ):
                await ab._run_turn(StagedAgent(), context, _State({}))
                await asyncio.wait_for(tool_started.wait(), timeout=2)
                _check(f"{label} never publishes the redundant plan", visible(), [])
                _check(f"{label} native status remains visible during the slow tool",
                       active is not None and not client_finished, True)
                context.send_activity = AsyncMock(side_effect=RuntimeError("Inbound context closed"))
                release_tool.set()
                await asyncio.wait_for(media_ready.wait(), timeout=2)
                _check(f"{label} buffers the viewport until completion", visible(), [])
                _check(f"{label} rendering status remains visible after the viewport is ready",
                       informative[-1][2], "Rendering the final image...")
                release_render.set()
                await asyncio.wait_for(completed.wait(), timeout=2)
            activities = [call.args[1] for call in send.await_args_list]
            body = activities[-1].text
            _check(f"{label} publishes exactly one complete answer", len(visible()), 1)
            _check(f"{label} final keeps viewport, render, summary in order",
                   body.startswith(viewport) and body.find(viewport) < body.find(render) < body.find("Created the village."),
                   True)
            _check(f"{label} final omits the redundant plan", intro in body, False)
            _check(f"{label} final omits intermediate render commentary", "I'll render the village next." in body, False)
            _check(f"{label} viewport and render each appear once",
                   [body.count("https://files.example/viewport.png"), body.count("https://files.example/render.png")], [1, 1])
            _check(f"{label} never injects ordinary replies into an open stream", interleaved, [])
            _check(f"{label} preserves the final summary", body.endswith("Created the village."), True)
            finals = [a for a in activities if a.type == "message" and getattr(a, "entities", None)]
            _check(f"{label} sends final only after all work completes", len(finals), 1)
            _check(f"{label} never reopens a stream", len(slots), 1)
            _check(f"{label} final closes the original informative stream", finals[-1].entities,
                   [{"type": "streaminfo", "streamType": "final", "streamId": "status-1"}])
            _check(f"{label} no native stream remains open", active, None)
            sequences = {}
            for stream_id, sequence, _text in informative:
                sequences.setdefault(stream_id, []).append(sequence)
            _check(f"{label} informative updates stay on one continuous sequence",
                   list(sequences.values()), [[1, 2, 3, 4]])
            typing = [a for a in activities if a.type == "typing"]
            _check(f"{label} never converts statuses to streamed content",
                   [a.entities[0]["streamType"] for a in typing], ["informative"] * len(typing))
            statuses = [text for _, _, text in informative]
            _check(f"{label} each tool status is accepted after earlier stages",
                   [text for index, text in enumerate(statuses) if index == 0 or text != statuses[index - 1]],
                   ["Preparing the scene...", "Executing Blender code...", "Capturing viewport screenshot...", "Rendering the final image..."])
            _check(f"{label} every stage keeps its user-message anchor", {a.reply_to_id for a in activities}, {"user-100"})
            _check(f"{label} keeps the complete plan in history", ab._conversations[conversation].history[-1]["text"].count(intro), 1)
            _check(f"{label} never reuses a detached inbound context", context.send_activity.await_count, 0)


async def test_native_teams_error_after_commentary():
    for error_id in (None, "agent-stream-error", "agent-turn-timeout"):
        conversation = f"19:staged-error-{error_id};messageid=100"
        _reset(conversation)

        class ErrorAgent:
            def run(self, messages, stream=True, options=None):
                async def generate():
                    yield SimpleNamespace(
                        message_id="status-get_scene_info",
                        contents=[SimpleNamespace(type="text", text="Inspecting...")],
                    )
                    yield SimpleNamespace(
                        message_id="plan",
                        contents=[
                            SimpleNamespace(type="text", text="The plan."),
                            SimpleNamespace(type="function_call", name="execute_blender_code"),
                        ],
                    )
                    if error_id:
                        yield SimpleNamespace(
                            message_id=error_id,
                            contents=[SimpleNamespace(type="text", text="The tool failed. Please retry.")],
                        )
                    raise RuntimeError("Tool failed")

                return generate()

        send = AsyncMock(return_value=SimpleNamespace(id="native-status"))
        client = SimpleNamespace(conversations=SimpleNamespace(send_to_conversation=send), close=AsyncMock())
        context = _Context(conversation, "Create a village.")
        context.activity.id = "user-100"
        context.activity.channel_id = ab.Channels.ms_teams
        context.activity.is_agentic_request = lambda: False
        context.turn_state["factory"] = SimpleNamespace(create_connector_client=AsyncMock(return_value=client))
        with patch.object(ab, "_TEAMS_STATUS_INTERVAL", 0):
            await ab._run_turn(ErrorAgent(), context, _State({}))
        _check(f"{error_id or 'unannounced error'} after commentary never claims success",
               send.await_args.args[1].text, "The tool failed. Please retry." if error_id else ab._ERROR_TEXT)


async def test_native_teams_status_failures():
    context = _Context("19:status-failures@thread.tacv2;messageid=100")
    context.activity.id = "200"
    send = AsyncMock()
    client = SimpleNamespace(conversations=SimpleNamespace(send_to_conversation=send), close=AsyncMock())
    context.turn_state["factory"] = SimpleNamespace(create_connector_client=AsyncMock(return_value=client))
    for label, responses in [
        ("initial 404", [RuntimeError("404"), SimpleNamespace(id="answer")]),
        ("missing stream ID", [None, SimpleNamespace(id="answer")]),
        ("final 403", [SimpleNamespace(id="stream"), RuntimeError("403"), SimpleNamespace(id="answer")]),
    ]:
        send.reset_mock(side_effect=True)
        send.side_effect = responses
        emitter = ab._TeamsStatusEmitter(context, ab._Emitter(context))
        with patch.object(ab, "_TEAMS_STATUS_INTERVAL", 0):
            await emitter.status("Inspecting...")
            if label != "final 403":
                await emitter.status("Rendering...")
                await emitter.keepalive()
            await emitter.text("The complete answer.")
            await emitter.card(SimpleNamespace(content_type="card"))
            await emitter.finish()
        activities = [call.args[1] for call in send.await_args_list]
        final = activities[-1]
        _check(f"{label} keeps the complete answer", final.text, "The complete answer.")
        _check(f"{label} keeps final attachments", len(final.attachments), 1)
        _check(f"{label} falls back without stream metadata", getattr(final, "entities", None), None)
        _check(f"{label} never sends ordinary status text",
               [item.text for item in activities if item.type == "message" and not getattr(item, "entities", None)],
               ["The complete answer."])

    send.reset_mock(side_effect=True)
    send.side_effect = [
        SimpleNamespace(id="stream"), RuntimeError("403"), SimpleNamespace(id="final"),
    ]
    emitter = ab._TeamsStatusEmitter(context, ab._Emitter(context))
    with patch.object(ab, "_TEAMS_STATUS_INTERVAL", 0):
        await emitter.status("Inspecting...")
        await emitter.status("Rendering...")
        await emitter.keepalive()
        await emitter.progress()
        await emitter.text("Recovered answer.")
        await emitter.finish()
    activities = [call.args[1] for call in send.await_args_list]
    _check("failed native update never becomes a chat countdown",
           [item.type for item in activities], ["typing", "typing", "message"])
    _check("native final still closes after an informative failure", activities[-1].entities,
           [{"type": "streaminfo", "streamType": "final", "streamId": "stream"}])
    _check("native recovery preserves the answer", activities[-1].text, "Recovered answer.")

    send.reset_mock(side_effect=True)
    send.return_value = SimpleNamespace(id="stream")
    emitter = ab._TeamsStatusEmitter(context, ab._Emitter(context))
    with patch.object(ab, "_TEAMS_STATUS_INTERVAL", 0):
        await emitter.status("Inspecting...")
        assert emitter._stream_started is not None
        emitter._stream_started -= ab._TEAMS_STATUS_MAX_SECONDS + 1
        await emitter.tick()
        await emitter.status("Rendering...")
        await emitter.keepalive()
        await emitter.progress()
        await emitter.text("Late answer.")
        await emitter.finish()
    activities = [call.args[1] for call in send.await_args_list]
    _check("native deadline closes status stream", activities[1].entities[0]["streamType"], "final")
    _check("native deadline closes only once without ordinary statuses", len(activities), 3)
    _check("native deadline preserves late answer", activities[-1].text, "Late answer.")
    _check("native deadline sends the late answer without stream metadata",
           getattr(activities[-1], "entities", None), None)
    _check("native deadline leaves a minute before the platform limit", ab._TEAMS_STATUS_MAX_SECONDS, 540)

    send.reset_mock(side_effect=True)
    emitter = ab._TeamsStatusEmitter(context, ab._Emitter(context))
    emitter._started -= 1000
    with patch.object(ab, "_TEAMS_STATUS_INTERVAL", 0):
        await emitter.status("Inspecting after a long queue...")
        await emitter.tick()
        await emitter.text("Queued answer.")
        await emitter.finish()
    activities = [call.args[1] for call in send.await_args_list]
    _check("native stream clock starts at its first update, not at turn creation",
           [item.type for item in activities], ["typing", "message"])
    _check("queued native turn still finalizes its own stream", activities[-1].entities,
           [{"type": "streaminfo", "streamType": "final", "streamId": "stream"}])

    send.reset_mock(side_effect=True)
    send.return_value = SimpleNamespace(id="stream")
    emitter = ab._TeamsStatusEmitter(context, ab._Emitter(context))
    with patch.object(ab.asyncio, "sleep", AsyncMock()) as sleep:
        await emitter.status("One")
        await emitter.status("Two")
    _check("native updates respect one request per second", 0 < sleep.await_args.args[0] <= 1, True)
    emitter._current = lambda: False
    await emitter.status("Superseded")
    _check("native clear suppresses stale status", send.await_count, 2)


async def test_native_teams_turns():
    class StatusAgent(_Agent):
        def __init__(self):
            super().__init__()
            self.ready = asyncio.Event()

        def run(self, messages, stream=True, options=None):
            updates = super().run(messages, stream=stream, options=options)

            async def generate():
                yield SimpleNamespace(
                    message_id="status-get_scene_info",
                    contents=[SimpleNamespace(type="text", text="Inspecting the scene...")],
                )
                await self.ready.wait()
                async for update in updates:
                    yield update

            return generate()

    for kind in ("personal", "channel", "groupChat"):
        conversation = f"19:native-{kind};messageid=100"
        _reset(conversation)
        agent = StatusAgent()
        completed = asyncio.Event()

        async def post(_conversation_id, activity):
            if activity.type == "message" and activity.text.startswith("reply-"):
                completed.set()
            return SimpleNamespace(id="native-stream")

        send = AsyncMock(side_effect=post)
        client = SimpleNamespace(
            conversations=SimpleNamespace(
                send_to_conversation=send,
                reply_to_activity=AsyncMock(side_effect=AssertionError("Wrong streaming endpoint")),
            ),
            close=AsyncMock(),
        )
        factory = SimpleNamespace(create_connector_client=AsyncMock(return_value=client))
        with (
            patch.object(ab, "_supports_streaming", _supports_streaming),
            patch.object(ab, "_StreamingEmitter", side_effect=AssertionError("Use native action updates")),
            patch.object(ab, "_TEAMS_STATUS_INTERVAL", 0),
            patch.object(ab, "_PROACTIVE_AFTER_SECONDS", 0.01),
        ):
            for index, text in enumerate(("first request", "follow-up request")):
                completed.clear()
                context = _Context(conversation, text)
                context.activity.id = str(100 + index)
                context.activity.channel_id = ab.Channels.ms_teams
                context.activity.conversation.conversation_type = kind
                context.activity.conversation.is_group = kind != "personal"
                context.activity.is_agentic_request = lambda: False
                context.turn_state["factory"] = factory
                await ab._run_turn(agent, context, _State({}))
                if index == 0:
                    _check(f"{kind} first request detaches before the slow tool completes", completed.is_set(), False)
                    context.send_activity = AsyncMock(side_effect=RuntimeError("Inbound context closed"))
                    agent.ready.set()
                    await asyncio.wait_for(completed.wait(), timeout=2)
                    _check(f"{kind} detached answer never reuses inbound context", context.send_activity.await_count, 0)
        activities = [call.args[1] for call in send.await_args_list]
        _check(f"{kind} routes two turns through native status delivery",
               [item.type for item in activities], ["typing", "message", "typing", "message"])
        _check(f"{kind} follow-up status uses its own user-message anchor",
               activities[2].reply_to_id, "101")
        _check(f"{kind} follow-up delivers answer", activities[3].text, "reply-2")
        _check(f"{kind} preserves history", agent.seen[1],
               ["first request", "reply-1", "follow-up request"])


async def main():
    _patch_framework()
    test_streaming_capabilities()
    await test_native_teams_status()
    await test_native_teams_buffered_reply()
    await test_native_teams_error_after_commentary()
    await test_native_teams_status_failures()
    await test_native_teams_turns()
    await test_teams_thread_turns()
    await test_serialised_turns()
    await test_clear_discards_inflight()
    await test_card_submit_transcript()
    await test_detach_race()
    await test_attachment_only_rejected_explicitly()
    await test_text_with_html_attachment()
    await test_reference_ingestion()
    await test_attachment_transport()
    await test_video_actions()
    await test_scoped_turn_and_reference_history()
    await test_video_cards()
    await test_video_notifications()
    with patch.object(ab, "_supports_streaming", _supports_streaming):
        await test_live_video_card_updates()
        await test_live_video_card_failures()
    await test_video_delivery_fallback()
    await test_clear_queued_action()
    test_optional_imports()
    print("\nFAILURES PRESENT" if _check.failed else "\nall checks passed")
    return 1 if _check.failed else 0


async def test_attachment_only_rejected_explicitly():
    context = _Context("c-attachment-only")
    context.activity.attachments = [SimpleNamespace(content_type="application/pdf")]
    agent = _Agent()
    await ab._run_turn(agent, context, _State({}))
    _check("unsupported attachment does not invoke model", agent.seen, [])
    _check("attachment-only message gets actionable response", bool(context.sent), True)


async def test_text_with_html_attachment():
    for index, attachment in enumerate([
        {"contentType": "text/html", "content": "<p>Hello</p>"},
        SimpleNamespace(content_type="text/html", content="<p>Hello</p>"),
    ]):
        context = _Context(f"c-html-{index}", text="Hello")
        context.activity.attachments = [attachment]
        agent = _Agent()
        with patch.object(tm, "store_references", AsyncMock()) as upload:
            await ab._run_turn(agent, context, _State({}))
        _check(f"HTML text attachment {index} reaches model", bool(agent.seen), True)
        _check(f"HTML text attachment {index} is not uploaded", upload.await_count, 0)

    context = _Context("c-html-empty", text="")
    context.activity.attachments = [{"contentType": "text/html", "content": ""}]
    agent = _Agent()
    await ab._run_turn(agent, context, _State({}))
    _check("HTML-only empty message does not invent a reference prompt", agent.seen, [])


SCOPE = "a" * 32
JOB_ID = "b" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"offline-decode-fixture"


class _Jobs:
    def __init__(self, scope=SCOPE, state="awaiting_seedance_approval"):
        self.scope = scope
        self.document = {"id": JOB_ID, "scope": scope, "state": state, "progress": 100}
        self.repo = SimpleNamespace(read=self.read)
        self._notifications = {}
        self.calls = []
        self.uploads = []
        self.storage = SimpleNamespace(upload_file=self.upload)

    def read(self, scope, job_id):
        if scope != self.scope or job_id != JOB_ID:
            raise ValueError("Not owned")
        return dict(self.document), "etag"

    def upload(self, name, path, media_type):
        self.uploads.append((name, path.read_bytes(), media_type))

    def describe(self, document):
        prefix = f"https://test.blob.core.windows.net/screenshots/videos/{self.scope}/{JOB_ID}"
        return {**document, "estimate_usd": 2.2, "seedance_enabled": True,
                "preview_url": prefix + "/preview.mp4?sig=FAKE", "poster_url": prefix + "/poster.png?sig=FAKE"}

    def status(self, scope, job_id):
        self.calls.append(("status", scope, job_id))
        return self.describe(self.read(scope, job_id)[0])

    def control(self, scope, action):
        self.read(scope, action["job_id"])
        self.calls.append((action["type"], scope, action["job_id"]))
        if action["type"] == "approve":
            self.document["state"] = "wavespeed_processing"
        elif action["type"] == "cancel":
            self.document["state"] = "cancelled"
        return self.describe(self.document)


def _action(scope=SCOPE, action="approve", **changes):
    value = {"type": "blenderVideoAction", "scope": scope, "job_id": JOB_ID, "action": action}
    if action == "approve":
        value.update(prompt="A snowy cabin", resolution="720p", generate_audio=False, consent="true", estimate_usd=2.2)
    return {**value, **changes}


async def _rejects(label, call):
    try:
        await call()
    except tm.MediaError as error:
        _check(label, bool(str(error)), True)
    else:
        _check(label, "accepted", "rejected")


async def test_reference_ingestion():
    context = _Context("c-reference")
    valid = {"contentType": "image/png", "name": "reference.png", "contentUrl": "data:image/png;base64," + base64.b64encode(PNG).decode()}
    html = {"contentType": "text/html", "content": "<p>Analyze these</p>"}
    _check("HTML message body is not a reference", tm.attachment_specs([html]), [])
    _check("HTML does not consume four-reference limit",
           tm.attachment_specs([html] + [valid] * 4), tm.attachment_specs([valid] * 4))
    malformed = [([valid] * 5), [{**valid, "name": "../ref.png"}], [{**valid, "name": "ref.mp4"}],
                 [{**valid, "contentType": {}}], [{**valid, "contentUrl": 42}],
                 [{"contentType": tm.FILE_INFO, "name": "ref.mp4", "content": {}}],
                 [{**html, "name": "page.html"}],
                 [{**html, "contentUrl": "https://files.example/page.html"}],
                 [html] + [valid] * 5,
                 [html, {"contentType": "application/pdf"}]]
    for index, attachments in enumerate(malformed):
        try:
            tm.attachment_specs(attachments)
        except tm.MediaError:
            _check(f"attachment validation {index}", True, True)
        else:
            _check(f"attachment validation {index}", False, True)
    file_spec = tm.attachment_specs([{"contentType": tm.FILE_INFO, "name": "clip.mp4",
                                     "content": {"downloadUrl": "https://files.example/clip"}}])
    _check("Teams file download info parsed", file_spec[0][0], "video/mp4")
    specs = tm.attachment_specs([valid])
    jobs = _Jobs()
    commands = []
    def media(command, **kwargs):
        commands.append(command)
        _check("decoder runs before upload", len(jobs.uploads), 0)
        return SimpleNamespace(stdout=json.dumps({"streams": [{"codec_type": "video", "width": 1, "height": 1}]}).encode())
    with patch.object(vj, "get_video_jobs", return_value=jobs), patch.object(ma, "run_media", side_effect=media):
        references = await tm.store_references(context, specs, SCOPE, lambda: True)
    _check("ffprobe and ffmpeg validation both run", len(commands), 2)
    _check("probe constrained to local protocols", "file,pipe" in commands[0], True)
    _check("decoder rejects decode errors", "-xerror" in commands[1], True)
    _check("stored reference namespace", references[0].startswith(f"references/{SCOPE}/"), True)
    _check("stored reference randomized filename", len(Path(references[0]).stem), 32)
    _check("uploaded only decoded bytes", jobs.uploads[0][1:] == (PNG, "image/png"), True)
    jobs.uploads.clear()
    with patch.object(vj, "get_video_jobs", return_value=jobs), patch.object(ma, "run_media", side_effect=ValueError("secret transport failure")):
        await _rejects("decode failure rejects upload", lambda: tm.store_references(context, specs, SCOPE, lambda: True))
    _check("decode failure uploads nothing", jobs.uploads, [])
    await _rejects("clear before upload rejects reference", lambda: tm.store_references(context, specs, SCOPE, lambda: False))


class _Session:
    def __init__(self, chunks=(PNG,), status=200, headers=None):
        self.chunks, self.status, self.headers = chunks, status, headers or {}
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        owner = self
        class Response:
            status = owner.status
            headers = owner.headers
            async def __aenter__(self):
                self.content = self
                return self
            async def __aexit__(self, *args):
                return False
            async def iter_chunked(self, _size):
                for chunk in owner.chunks:
                    yield chunk
        return Response()


async def test_attachment_transport():
    import aiohttp
    import wavespeed_client as ws

    context = _Context("c-transport")
    protected = context.activity.service_url + "v3/attachments/attachment-id/views/original"
    session = _Session()
    client = SimpleNamespace(client=session, close=AsyncMock())
    factory = SimpleNamespace(create_connector_client=AsyncMock(return_value=client))
    context.turn_state["factory"] = factory
    _check("verified connector attachment accepted", tm.connector_attachment(context, protected), True)
    for target in [protected.replace("/teams/", "/other/"), protected + "?redirect=x", protected + "/../x",
                   "https://evil.example/v3/attachments/id/views/original", "https://smba.trafficmanager.net.evil.example/teams/v3/attachments/id/views/original"]:
        _check("unverified token destination rejected", tm.connector_attachment(context, target), False)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "reference.png"
        with patch.object(ws, "public_https", side_effect=lambda url: url):
            await tm.download_attachment(context, "image/png", protected, path)
        _check("fresh SDK client created", factory.create_connector_client.await_count, 1)
        _check("SDK identity scope preserved", factory.create_connector_client.call_args.args[-2:], (["connector-scope"], False))
        _check("SDK client closed", client.close.await_count, 1)
        _check("download streamed to temp", path.read_bytes() == PNG, True)
        _check("authenticated redirect following disabled", session.requests[0][1]["allow_redirects"], False)
        _check("download deadline configured", session.requests[0][1]["timeout"].total, 60)
        plain = _Session()
        factory.create_connector_client.reset_mock()
        with patch.object(ws, "public_https", side_effect=lambda url: url), patch.object(aiohttp, "ClientSession", return_value=plain):
            await tm.download_attachment(context, "image/png", "https://files.example/ref.png?sig=FAKE", path)
        _check("signed download never borrows connector token", factory.create_connector_client.await_count, 0)
        _check("uncredentialed request has no auth header", "headers" in plain.requests[0][1], False)
        for label, candidate in [("redirect", _Session(status=302)), ("unauthorized", _Session(status=401)),
                                 ("declared oversize", _Session(headers={"Content-Length": str(tm.MAX_BYTES + 1)})),
                                 ("compressed", _Session(headers={"Content-Encoding": "gzip"})), ("empty", _Session(chunks=()))]:
            await _rejects(label, lambda candidate=candidate: tm._copy_response(candidate, "https://files.example/ref", path))
        with patch.object(tm, "MAX_BYTES", 4):
            await _rejects("streamed oversize", lambda: tm._copy_response(_Session(chunks=(b"123", b"45")), "https://files.example/ref", path))
        with patch.object(ws.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]), patch.object(aiohttp, "ClientSession") as sessions:
            await _rejects("private DNS destination rejected", lambda: tm.download_attachment(context, "image/png", "https://files.example/ref", path))
            _check("private DNS rejection before network", sessions.call_count, 0)


async def test_video_actions():
    context = _Context("c-controls")
    jobs = _Jobs()
    notifier = (asyncio.get_running_loop(), AsyncMock())
    backend = SimpleNamespace(control_action=jobs.control)
    invalid = [{"type": "wrong"}, _action(scope="c" * 32), _action(job_id=[]), _action(action="unknown"),
               _action(consent=False), _action(consent=True), _action(generate_audio="false"),
               _action(generate_audio=True), _action(resolution="1080p"), _action(prompt=" "),
               _action(prompt="x" * 8001), _action(estimate_usd=float("nan")), _action(url="https://evil.example")]
    with patch.object(vj, "get_video_jobs", return_value=jobs), patch.dict(sys.modules, {"media_control": backend}):
        for index, value in enumerate(invalid):
            await _rejects(f"invalid video action {index}", lambda value=value: tm.run_video_action(context, value, SCOPE, notifier, lambda: True))
        _check("invalid actions never call control backend", jobs.calls, [])
        await _rejects("price change requires new consent", lambda: tm.run_video_action(context, _action(estimate_usd=0), SCOPE, notifier, lambda: True))
        _check("invalid price cannot register notifier", jobs._notifications, {})
        await tm.run_video_action(context, _action(), SCOPE, notifier, lambda: True)
        await tm.run_video_action(context, _action(), SCOPE, notifier, lambda: True)
        _check("duplicate approvals execute once", [call[0] for call in jobs.calls].count("approve"), 1)
        _check("approval attaches fresh notification", jobs._notifications[JOB_ID] is notifier, True)
        newer = (asyncio.get_running_loop(), AsyncMock())
        await tm.run_video_action(context, _action(action="status"), SCOPE, newer, lambda: True)
        _check("status reattaches notification after idle", jobs._notifications[JOB_ID] is newer, True)
        await tm.run_video_action(context, _action(action="cancel"), SCOPE, newer, lambda: True)
        _check("cancel reaches backend", jobs.calls[-1][0], "cancel")
        await _rejects("epoch rejects action before control", lambda: tm.run_video_action(context, _action(action="status"), SCOPE, notifier, lambda: False))
        context.identity.is_authenticated = False
        await _rejects("anonymous action rejected", lambda: tm.run_video_action(context, _action(), SCOPE, notifier, lambda: True))


async def test_scoped_turn_and_reference_history():
    conversation = "c-scoped"
    _reset(conversation)
    context = _Context(conversation)
    context.activity.id = "attachment-activity-1"
    context.activity.attachments = [{"contentType": "image/png", "name": "ref.png", "contentUrl": "https://files.example/ref.png?sig=FAKE"}]
    scope = conversation_scope(ab._scene_key(conversation))
    reference = f"references/{scope}/{JOB_ID}.png"
    observed = []
    class ScopedAgent(_Agent):
        def run(self, messages, stream=True, options=None):
            async def generate():
                observed.append((vj.video_scope.get(), vj.video_notifier.get(), options, messages[-1].contents[0].text))
                observed.append(await asyncio.to_thread(lambda: vj.video_scope.get()))
                yield SimpleNamespace(message_id="text", contents=[SimpleNamespace(type="text", text="Reference analyzed.")])
            return generate()
    with patch.object(tm, "store_references", AsyncMock(return_value=[reference])) as store:
        await ab._run_turn(ScopedAgent(), context, _State({}))
        await ab._run_turn(ScopedAgent(), context, _State({}))
    _check("attachment-only valid turn accepted", len(observed), 2)
    _check("reference uploaded only once for duplicate activity", store.await_count, 1)
    _check("scope bound during streamed model iteration", observed[0][0], scope)
    _check("scope copied into tool thread", observed[1], scope)
    _check("notifier loop captured before inbound closes", observed[0][1][0] is asyncio.get_running_loop(), True)
    _check("scene user key retained", observed[0][2], {"user": ab._scene_key(conversation)})
    _check("model receives stored ID", reference in observed[0][3], True)
    _check("model never receives signed input URL", "sig=FAKE" in observed[0][3], False)
    _check("history retains safe reference IDs", reference in ab._conversation_slot(conversation).history[0]["text"], True)
    _check("scope reset after turn", vj.video_scope.get(), None)
    _check("notifier reset after turn", vj.video_notifier.get(), None)
    value = _action(scope=scope)
    jobs = _Jobs(scope)
    agent = _Agent()
    with patch.object(vj, "get_video_jobs", return_value=jobs), patch.dict(sys.modules, {"media_control": SimpleNamespace(control_action=jobs.control)}):
        await ab._run_turn(agent, _Context(conversation, value=value), _State({}))
        await ab._run_turn(agent, _Context(conversation, text="approve paid finishing"), _State({}))
    _check("card approval bypasses model", len(agent.seen), 1)
    _check("plain approval text never invokes paid control", [call[0] for call in jobs.calls].count("approve"), 1)


async def test_video_cards():
    jobs = _Jobs(state="completed")
    raw = "```videojob\n" + json.dumps({"id": JOB_ID, "state": "awaiting_seedance_approval", "preview_url": "https://evil.example/forged.mp4", "estimate_usd": 0}) + "\n```"
    gallery = ab._GalleryFilter(SCOPE)
    cards, text = [], ""
    for character in raw + raw:
        plain, chunk = gallery.feed(character)
        text += plain
        cards.extend(chunk)
    _check("fragmented repeated video fence produces one card", len(cards), 1)
    _check("video JSON is not emitted as prose", text, "")
    incomplete = ab._GalleryFilter(SCOPE)
    incomplete.feed(raw[:-3])
    _check("incomplete fence never exposes raw descriptor", "preview_url" in incomplete.flush()[0], False)
    context = _Context("c-cards")
    with patch.object(vj, "get_video_jobs", return_value=jobs):
        await ab._emit_filtered(ab._FallbackEmitter(context), ("", cards), ab._MediaDedupeFilter())
    adaptive = context.sent[0].content
    rendered = json.dumps(adaptive)
    _check("card uses authoritative completed state", "completed" in rendered, True)
    _check("model forged media URL ignored", "evil.example" in rendered, False)
    _check("no approval form before verified awaiting state", "Action.ShowCard" in rendered, False)
    _check("native mp4 attachment supplied", context.sent[1].content_type, "video/mp4")
    _check("signed download fallback always present", any(action["type"] == "Action.OpenUrl" for action in adaptive["actions"]), True)
    _check("status recovery action present", adaptive["actions"][0]["data"]["action"], "status")
    jobs.document["state"] = "awaiting_seedance_approval"
    approval = tm.video_attachments(jobs.describe(jobs.document), SCOPE, ab.Attachment)[0].content
    review = next(action["card"] for action in approval["actions"] if action["type"] == "Action.ShowCard")
    _check("consent defaults off", review["body"][1]["value"], "false")
    _check("paid submission fixed to no audio", review["actions"][0]["data"]["generate_audio"], False)
    _check("third-party and estimate disclosed", "WaveSpeed" in json.dumps(approval) and "$2.20" in json.dumps(approval), True)
    dedupe = ab._MediaDedupeFilter()
    link = "[Download](https://test.example/video.mp4?sig=FAKE)"
    _check("MP4 echoes deduplicated", dedupe.feed(link + link) + dedupe.flush(), link)


async def test_video_notifications():
    conversation = "c-notify"
    _reset(conversation)
    context = _Context(conversation)
    slot = ab._conversation_slot(conversation)
    sent = []
    async def send(_conversation, activity):
        sent.append(activity)
    client = SimpleNamespace(conversations=SimpleNamespace(send_to_conversation=send), close=AsyncMock())
    factory = SimpleNamespace(create_connector_client=AsyncMock(return_value=client))
    context.turn_state["factory"] = factory
    notifier = ab._capture_video_notifier(context, ab._FallbackEmitter(context), slot, slot.epoch, SCOPE)
    context.send_activity = AsyncMock(side_effect=RuntimeError("Inbound closed"))
    result = _Jobs().describe(_Jobs().document)
    await notifier[1](result)
    await notifier[1](result)
    _check("proactive callback uses fresh connector", factory.create_connector_client.await_count, 1)
    _check("duplicate notification suppressed", len(sent), 1)
    _check("proactive result includes MP4 and card", len(sent[0].attachments), 2)
    _check("closed inbound context not reused", context.send_activity.await_count, 0)
    ab._clear_conversation(_State({}), slot)
    await notifier[1]({**result, "state": "completed"})
    _check("clear suppresses stale notification", len(sent), 1)
    notifier = ab._capture_video_notifier(context, ab._FallbackEmitter(context), slot, slot.epoch, SCOPE)
    entered, resume = asyncio.Event(), asyncio.Event()
    async def create(*args):
        entered.set()
        await resume.wait()
        return client
    factory.create_connector_client = create
    pending = asyncio.create_task(notifier[1](result))
    await entered.wait()
    ab._clear_conversation(_State({}), slot)
    resume.set()
    await pending
    _check("clear during fresh-token await suppresses send", len(sent), 1)


async def test_live_video_card_updates():
    conversation = "c-live-video"
    _reset(conversation)
    context = _Context(conversation)
    context.activity.channel_id = ab.Channels.ms_teams
    context.activity.conversation.conversation_type = "channel"
    context.activity.conversation.is_group = True
    context.activity.is_agentic_request = lambda: False
    slot = ab._conversation_slot(conversation)
    sends, updates = [], []

    async def send(conversation_id, activity):
        sends.append(activity)
        return SimpleNamespace(id="card-1")

    async def update(conversation_id, activity_id, activity):
        updates.append((activity_id, activity))
        return SimpleNamespace(id=activity_id)

    client = SimpleNamespace(conversations=SimpleNamespace(send_to_conversation=send, update_activity=update), close=AsyncMock())
    context.turn_state["factory"] = SimpleNamespace(create_connector_client=AsyncMock(return_value=client))
    notifier = ab._capture_video_notifier(context, ab._FallbackEmitter(context), slot, slot.epoch, SCOPE)
    _check("channel video cards do not require text streaming", _supports_streaming(context), False)
    _check("Teams opts into worker progress", notifier[1].live_progress, True)
    result = {**_Jobs().describe(_Jobs().document), "state": "rendering", "progress": 10}
    result.pop("preview_url")
    result.pop("poster_url")
    with patch.object(vj, "get_video_jobs", return_value=SimpleNamespace(status=lambda *args: result)):
        await ab._emit_filtered(ab._FallbackEmitter(context), ("", [ab._VideoCard(SCOPE, JOB_ID)]),
                                ab._MediaDedupeFilter(), notifier=notifier)
    _check("initial live card sent once", len(sends), 1)
    _check("live card is standalone without split text", sends[0].text, "")
    await notifier[1]({**result, "progress": 40})
    await notifier[1]({**result, "progress": 40})
    _check("progress updates original card", [item[0] for item in updates], ["card-1"])
    next_notifier = ab._capture_video_notifier(context, ab._FallbackEmitter(context), slot, slot.epoch, SCOPE)
    await next_notifier[1]({**result, "state": "completed", "progress": 100})
    await notifier[1]({**result, "progress": 60})
    _check("card ID survives notifier recapture and stale progress is dropped", len(updates), 2)
    _check("progress never sends another card", len(sends), 1)
    await next_notifier[1]({**result, "state": "completed", "progress": 100}, refresh=True)
    _check("explicit Status can refresh completed card links", len(updates), 3)
    ab._clear_conversation(_State({}), slot)
    await notifier[1]({**result, "progress": 80})
    _check("clear stops live card edits", len(updates), 3)
    _check("clear discards old card bindings", slot.video_cards, {})
    recovery_context = _Context("c-live-recovery")
    recovery_context.activity.channel_id = ab.Channels.ms_teams
    recovery_context.activity.is_agentic_request = lambda: False
    recovery_context.activity.reply_to_id = "existing-card"
    recovery_context.turn_state["factory"] = context.turn_state["factory"]
    recovery_scope = conversation_scope(ab._scene_key("c-live-recovery"))
    recovery_context.activity.value = _action(scope=recovery_scope, action="status")
    jobs = _Jobs(recovery_scope)
    agent = _Agent()
    with patch.object(vj, "get_video_jobs", return_value=jobs), patch.dict(sys.modules, {"media_control": SimpleNamespace(control_action=jobs.control)}):
        await ab._run_turn(agent, recovery_context, _State({}))
    _check("Status recovers existing card ID after registry loss", updates[-1][0], "existing-card")
    _check("recovered Status does not invoke model", agent.seen, [])
    _check("recovered Status registers live notifications", jobs._notifications[JOB_ID][1].live_progress, True)
    context.activity.is_agentic_request = lambda: True
    copilot = ab._capture_video_notifier(context, ab._FallbackEmitter(context), slot, slot.epoch, SCOPE)
    _check("M365 Copilot keeps non-live delivery", copilot[1].live_progress, False)


async def test_live_video_card_failures():
    context = _Context("c-live-failures")
    context.activity.channel_id = ab.Channels.ms_teams
    context.activity.is_agentic_request = lambda: False
    result = {**_Jobs().describe(_Jobs().document), "state": "rendering", "progress": 20}
    attachment = tm.video_attachments(result, SCOPE, ab.Attachment)[0]
    send = AsyncMock(return_value=SimpleNamespace(id="replacement"))
    update = AsyncMock()
    client = SimpleNamespace(conversations=SimpleNamespace(send_to_conversation=send, update_activity=update), close=AsyncMock())
    factory = SimpleNamespace(create_connector_client=AsyncMock(return_value=client))
    context.turn_state["factory"] = factory
    sender = ab._ProactiveEmitter(context, ab._FallbackEmitter(context))
    binding = ab._LiveVideoCard()
    binding.activity_id = "original"
    throttled = RuntimeError("throttled")
    throttled.status_code = 429
    throttled.response = SimpleNamespace(headers={"Retry-After": "0"})
    update.side_effect = [throttled, None]
    _check("429 retries existing activity", await sender._update_video_card(binding, attachment), True)
    _check("429 does not create duplicate card", send.await_count, 0)
    _check("429 uses same target twice", [call.args[1] for call in update.await_args_list], ["original", "original"])
    missing = RuntimeError("missing activity")
    missing.status_code = 404
    update.side_effect = missing
    try:
        await sender._update_video_card(binding, attachment)
    except RuntimeError:
        pass
    _check("generic 404 does not replace the card", send.await_count, 0)
    missing.error = SimpleNamespace(code="ActivityNotFound")
    update.side_effect = missing
    _check("deleted card gets one replacement", await sender._update_video_card(binding, attachment), True)
    _check("replacement ID retained", binding.activity_id, "replacement")
    try:
        await sender._update_video_card(binding, attachment)
    except RuntimeError:
        pass
    _check("missing-card replacement is bounded", send.await_count, 1)
    denied = RuntimeError("blocked bot")
    denied.status_code = 403
    update.side_effect = denied
    try:
        await sender._update_video_card(binding, attachment)
    except RuntimeError:
        pass
    _check("403 stops automatic updates", binding.blocked, True)
    _check("403 never posts a replacement", send.await_count, 1)
    uncertain = ab._LiveVideoCard()
    send.side_effect = TimeoutError("response lost")
    try:
        await sender._update_video_card(uncertain, attachment)
    except TimeoutError:
        pass
    _check("ambiguous initial send stops duplicate creation", uncertain.blocked, True)
    _check("ambiguous send is not automatically retried", await sender._update_video_card(uncertain, attachment), False)
    binding.blocked = False
    sender._current = lambda: False
    update.reset_mock()
    _check("clear during connector acquisition stops update", await sender._update_video_card(binding, attachment), False)
    _check("stale connector never edits card", update.await_count, 0)


async def test_video_delivery_fallback():
    context = _Context("c-video-fallback")
    messages = []
    async def send(*args):
        activity = args[-1]
        if any(attachment.content_type == "video/mp4" for attachment in activity.attachments):
            raise ValueError("Native video unsupported")
        messages.append(activity)
    attachments = tm.video_attachments(_Jobs().describe(_Jobs().document), SCOPE, ab.Attachment)
    activity = _Activity(type="message", text="Video update", attachments=attachments)
    context.send_activity = send
    await ab._safe_send(context, activity)
    _check("live native rejection delivers fallback card", len(messages), 1)
    _check("fallback retains signed download action", "Download MP4" in json.dumps(messages[0].attachments[0].content), True)
    messages.clear()
    sender = ab._ProactiveEmitter(context, ab._FallbackEmitter(context))
    async def proactive(activity):
        await send(activity)
        return True
    sender._delivery_attempts = lambda: [("offline", proactive)]
    await sender._send(activity)
    _check("proactive native rejection delivers fallback card", len(messages), 1)
    _check("fallback strips only native video", len(messages[0].attachments), 1)


async def test_clear_queued_action():
    conversation = "c-clear-controls"
    _reset(conversation)
    slot = ab._conversation_slot(conversation)
    scope = conversation_scope(ab._scene_key(conversation))
    context = _Context(conversation, value=_action(scope=scope))
    agent = _Agent()
    await slot.lock.acquire()
    with patch.object(tm, "run_video_action", AsyncMock()) as control:
        task = asyncio.create_task(ab._run_turn(agent, context, _State({})))
        await asyncio.sleep(0.02)
        ab._clear_conversation(_State({}), slot)
        slot.lock.release()
        await task
        _check("clear rejects queued paid action", control.await_count, 0)
    _check("queued action never reaches model", agent.seen, [])
    with patch.object(tm, "run_video_action", wraps=tm.run_video_action), patch.object(vj, "get_video_jobs") as jobs:
        await ab._run_turn(agent, _Context(conversation, value=_action(scope=scope)), _State({}))
        _check("old card scope rejected after clear with stale TurnState", jobs.call_count, 0)
    ab._clear_conversation(_State({}), slot)
    _check("repeated clear rotates generation despite stale snapshots", slot.generation, 2)


def test_optional_imports():
    result = subprocess.run([sys.executable, "-B", "-c",
                             "import sys; sys.modules['agent_framework'] = None; import activity_bridge; assert not activity_bridge.activity_available()"],
                            capture_output=True, cwd=Path(__file__).resolve().parents[1], timeout=30)
    _check("missing optional SDK still permits importing bridge", result.returncode, 0)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
