"""Smoke test for Activity-path turn serialisation and transcript handling.

Runs without the hosting SDK: `_run_turn` only touches the agent, the turn
context and the TurnState through duck-typed attributes, so stubs are enough.

    python devTools/test_activity_history.py
"""

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import activity_bridge as ab  # noqa: E402


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
            text=text,
            value=value,
            conversation=SimpleNamespace(id=conversation_id),
            recipient=SimpleNamespace(id="28:agent"),
            channel_id="emulator",
            delivery_mode=None,
        )
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
    ab._supports_streaming = lambda _context: False

    class _Emitter(ab._Emitter):
        async def status(self, text):
            self._last_status = text

        async def text(self, chunk):
            pass

        async def card(self, attachment):
            pass

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


async def main():
    _patch_framework()
    await test_serialised_turns()
    await test_clear_discards_inflight()
    await test_card_submit_transcript()
    await test_detach_race()
    print("\nFAILURES PRESENT" if _check.failed else "\nall checks passed")
    return 1 if _check.failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
