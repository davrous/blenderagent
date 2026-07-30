"""
Activity protocol bridge — Teams / Microsoft 365 Copilot support.

Why this module exists
----------------------
The default "publish to M365" path from the Foundry portal does not implement
the Activity protocol natively: it adapts the ``responses`` protocol, which has
no concept of an *informative update*. The custom waiting messages this agent
broadcasts from ``ToolStatusMiddleware`` ("Rendering the final image…") are
plain text deltas, so Teams collapses them into a single generic "working on
it" indicator.

This module speaks the Activity protocol directly. It composes
``ActivityAgentServerHost`` (from ``azure-ai-agentserver-activity``) with the
existing ``ResponsesHostServer`` into ONE multi-protocol server, so a single
container serves:

    POST /responses         → the web chat + the voice loopback (unchanged)
    POST /activity/messages → Teams / M365 Copilot        (and /api/messages)
    WS   /invocations_ws    → voice, mounted by voice_pipeline (unchanged)

The Activity handler runs the *same* ``agent_framework`` ``Agent`` — same
tools, same ``SceneIsolationMiddleware(ToolStatusMiddleware(), …)`` stack — and
maps the streamed ``AgentResponseUpdate``s onto native Activity constructs:

    message_id "status-*" / "scene-status-*" → queue_informative_update()
    everything else (model text, tool images, download links, error text)
                                             → queue_text_chunk()

Routing is done on ``message_id`` rather than by regex-matching the
``\\n\\n*text*\\n\\n`` markers the way the web client has to, because in-process
we still have the structured update objects.

Non-streaming fallback
----------------------
``StreamingResponse`` silently drops informative updates on channels that don't
support streaming, and the M365 SDK explicitly disables streaming for *agentic*
requests — which is the M365 Copilot path. On those channels each status is
sent as its own message activity instead, so the custom waiting text is still
visible (chattier, but visible).

Everything here is optional: ``main.py`` falls back to a responses-only host if
the imports fail or ``ENABLE_ACTIVITY`` is turned off, mirroring how
``voice_pipeline`` degrades.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger("blender_agent.activity")

# ── Update routing ──────────────────────────────────────────────────────────
# Message ids stamped by ToolStatusMiddleware / SceneIsolationMiddleware in
# main.py. Keep these in sync with the `message_id=` arguments there.
_STATUS_PREFIXES = ("status-", "scene-status-")

# How many prior messages (user + assistant) to replay on each turn. The
# Activity path has NO Foundry-managed history — unlike /responses, which gets
# it from the hosting layer — so we keep our own in the M365 conversation
# state. NOTE: this is a property name on the ConversationState scope, NOT a
# dotted `TurnState.get_value` path — `TurnState` registers its scopes under
# their CLASS names (`ConversationState`, `UserState`; only `temp` is
# lowercase), so "conversation.<key>" raises ValueError: Scope not found.
_MAX_HISTORY_MESSAGES = 20
# Second budget on top of the message count. Scene-building replies carry long
# prose plus markdown image and download links, so 20 raw messages is unbounded
# in tokens; ~4 chars/token puts this around 6k tokens of replayed transcript.
_MAX_HISTORY_CHARS = 24_000
_HISTORY_KEY = "blender_history"

# Shown when a message arrives while the previous one is still being worked on.
_QUEUED_STATUS_TEXT = "Finishing the previous request first…"

# One VM serves one conversation in practice; the cap only stops a long-lived
# container in a busy group chat from growing without bound.
_MAX_TRACKED_CONVERSATIONS = 32

# Bumped by /clear. Folded into the scene key so the next turn presents a NEW
# id to SceneIsolationMiddleware, which is exactly the signal the web chat's
# Reset button produces by rotating its conversation UUID.
_SCENE_GENERATION_KEY = "blender_scene_generation"
# NOT "/reset": Teams treats that as one of its own native chat commands, so it
# never reaches the agent.
_CLEAR_COMMAND = "/clear"

# Fenced blocks the system prompt tells the model to emit for the asset
# galleries. On the web chat these render as clickable thumbnail grids; in
# Teams they become Adaptive Cards.
_GALLERY_TAGS = ("models", "textures")
_MAX_CARD_ITEMS = 6
_ADAPTIVE_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"

# Markdown image `![alt](url)` or link `[text](url)`; group 1 is the leading
# `!`, group 2 the url. Mirrors MEDIA_RE in the web client's parseMarkdown.ts.
_MEDIA_RE = re.compile(r"(!?)\[[^\]\n]*\]\(([^)\s]+)(?:\s+[^)\n]*)?\)")
_DOWNLOAD_SUFFIXES = (".blend", ".glb", ".gltf", ".fbx")
# Cap on how much text may be held back waiting for a half-arrived media token,
# so a stray "[" can never stall the stream for a whole turn.
_MAX_MEDIA_HOLD = 4096

_WELCOME_TEXT = (
    "Hi! I'm a 3D scene assistant powered by Blender. Ask me to build a scene "
    "— for example *\"create a small cabin on a snowy hill and render it\"* — "
    "and I'll model it, texture it and send you back a render.\n\n"
    "Send **/clear** at any time to throw away the current scene and start from scratch."
)

_CLEAR_TEXT = (
    "🧹 Done — I've cleared this conversation. Your next message starts from a "
    "brand-new, empty Blender scene."
)

_EMPTY_REPLY_TEXT = "I finished that step but didn't have anything to add."

_GALLERY_ONLY_TEXT = "Here's what I found — tap one to use it."

_ERROR_TEXT = (
    "Sorry — something went wrong while working on your scene. "
    "Please try again, or rephrase your request."
)

# ── Keeping long turns alive ────────────────────────────────────────────────
# Teams and M365 Copilot abandon a turn that goes quiet for roughly 45s, and a
# single tool call (a final render, a model import) routinely takes longer than
# that. Status updates only fire when a tool *starts*, so a pump re-states the
# current status whenever nothing has been sent for a while.
_KEEPALIVE_SECONDS = float(os.environ.get("ACTIVITY_KEEPALIVE_SECONDS", "20"))
_KEEPALIVE_POLL_SECONDS = 2.0
_KEEPALIVE_FALLBACK_TEXT = "Working on your scene"

# Teams enforces a hard two-minute lifetime on a streamed message and then
# rejects everything further with 403 ContentStreamNotAllowed ("Content stream
# finished due to exceeded streaming time") — including the final message, so
# the whole reply is lost. Close the stream before that and keep going with
# ordinary messages. MUST stay below 120: a larger value disables the guard
# entirely and hands the turn back to the failure it exists to prevent.
_STREAM_MAX_SECONDS = min(
    float(os.environ.get("ACTIVITY_STREAM_MAX_SECONDS", "90")), 110.0
)
_STREAM_CONTINUED_TEXT = "Still working on it — I'll send the rest right here."

# ── Fast turns vs. slow turns ───────────────────────────────────────────────
# The platform in front of the container gives up on the inbound request after
# roughly 45s and shows an error in the client, even though the agent is still
# working. Turns that pass this mark therefore stop replying *to the request*:
# the live response is closed with a "this will take a while" note, the request
# completes, and the result is delivered later as a proactive message.
# Set to 0 to disable and always answer in-request.
_PROACTIVE_AFTER_SECONDS = float(
    os.environ.get("ACTIVITY_PROACTIVE_AFTER_SECONDS", "35")
)
_HANDOVER_TEXT = (
    "\n\n⏳ This task requires time — I'll keep working on it in the "
    "background and message you here as soon as it's ready."
)

# Statuses that mark the start of the slowest step. Once a turn has been
# detached, reaching one of these flushes whatever is buffered (typically the
# viewport screenshot the middleware surfaced) as an interim message, so the
# user sees the scene instead of silence while the render runs.
_PROGRESS_STATUS_PREFIXES = ("status-render_final", "status-render_preview")
_PROGRESS_WITH_MEDIA_TEXT = (
    "\n\nThat's the scene so far — I'm rendering the final image now and will "
    "send it as soon as it's ready."
)
_PROGRESS_PLAIN_TEXT = (
    "🎬 Rendering the final image now — I'll send it as soon as it's ready."
)

# Populated by the guarded import below; `activity_available()` reports on it.
_IMPORT_ERROR: Exception | None = None

try:
    from agent_framework import Content, Message
    from agent_framework_foundry_hosting import ResponsesHostServer
    from azure.ai.agentserver.activity import ActivityAgentServerHost
    from microsoft_agents.activity import Activity, Attachment, Channels, DeliveryModes
    from microsoft_agents.hosting.core.storage import MemoryStorage
except Exception as exc:  # pragma: no cover - exercised only on broken installs
    _IMPORT_ERROR = exc


def activity_available() -> bool:
    """Whether the Activity protocol can be served in this process."""
    flag = os.environ.get("ENABLE_ACTIVITY", "true").strip().lower()
    if flag in ("0", "false", "no", "off"):
        logger.info("Activity protocol disabled via ENABLE_ACTIVITY=%s", flag)
        return False
    if _IMPORT_ERROR is not None:
        logger.warning(
            "Activity protocol unavailable — the azure-ai-agentserver-activity / "
            "microsoft-agents packages failed to import: %s",
            _IMPORT_ERROR,
        )
        return False
    return True


def build_multi_protocol_host(agent: Any) -> Any:
    """Build a host that serves both the responses and activity protocols.

    Composition is plain Python mixin inheritance — both classes are
    ``AgentServerHost`` (i.e. Starlette) subclasses that append their own
    routes during a cooperative ``__init__``, which is the pattern the
    ``azure-ai-agentserver-activity`` ``05-multi-protocol`` sample documents.

    ``ActivityAgentServerHost`` is listed first (matching that sample); its
    ``__init__`` is keyword-only with ``**kwargs``, so ``agent=`` flows through
    to ``ResponsesHostServer``.
    """

    class BlenderAgentHost(ActivityAgentServerHost, ResponsesHostServer):  # type: ignore[misc]
        """Serves /responses (web chat, voice loopback) and /activity/messages (Teams)."""

    # The host defaults to in-memory storage, which would make the transcript
    # the ONLY part of a conversation that does not survive an idle/resume
    # cycle — the scene itself is persisted to the same volume by
    # `scene_manager`. A user coming back after an idle would find their scene
    # intact but the agent with no memory of how it got there.
    host = BlenderAgentHost(agent=agent, storage=_build_storage())
    paths = sorted({getattr(r, "path", str(r)) for r in host.router.routes})
    logger.info("Multi-protocol host routes: %s", paths)
    return host


def _build_storage() -> Any:
    """Conversation state backed by the platform's durable ``$HOME`` volume.

    Falls back to plain in-memory storage when ``$HOME`` is not writable (some
    local Docker setups), matching how ``scene_manager`` degrades to ``/tmp``.
    """

    class _FileStorage(MemoryStorage):  # type: ignore[misc]
        """``MemoryStorage`` that mirrors itself to a JSON file after every change.

        Subclassing rather than reimplementing keeps the SDK's ``StoreItem``
        serialisation: ``MemoryStorage`` already holds its state as plain JSON,
        so persisting is just dumping that dict.

        Declared here, not at module scope, because ``MemoryStorage`` comes from
        the guarded import above — a module-level base class would turn a
        missing SDK into an ImportError for the whole module instead of the
        clean ``activity_available() is False`` degradation main.py expects.

        The state is small (a bounded transcript plus a scene generation
        counter) and writes happen once per turn, so a full rewrite is cheaper
        than any incremental scheme. Never raises on a write failure — losing
        durability must not fail the turn.
        """

        def __init__(self, path: str) -> None:
            self._path = path
            super().__init__(_read_state_file(path))

        async def write(self, changes: Any) -> None:
            await super().write(changes)
            self._flush()

        async def delete(self, keys: Any) -> None:
            await super().delete(keys)
            self._flush()

        def _flush(self) -> None:
            # Deliberately does NOT touch `MemoryStorage._lock`. That attribute
            # is an `asyncio.Lock` in SDK 1.2.0 but a `threading.Lock` in older
            # releases, so `with`/`async with` are not interchangeable and
            # reaching for it couples us to a private detail that has already
            # changed once. No lock is needed anyway: this runs synchronously
            # (no await between the copy and the write), and asyncio cannot
            # preempt a coroutine without an await point, so no other writer can
            # interleave.
            _write_state_file(self._path, dict(self._memory))

    path = os.path.join(os.path.expanduser("~"), ".blender_activity_state.json")
    try:
        storage = _FileStorage(path)
    except OSError:
        logger.warning(
            "Could not open %s — Activity conversation state will not survive "
            "an idle/resume cycle",
            path, exc_info=True,
        )
        return MemoryStorage()
    logger.info("Activity conversation state (persisted): %s", path)
    return storage


def _read_state_file(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read %s — starting empty", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _write_state_file(path: str, payload: dict) -> None:
    try:
        serialized = json.dumps(payload)
    except (TypeError, ValueError):
        logger.warning("Could not serialize Activity state", exc_info=True)
        return
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(serialized)
        os.replace(tmp, path)
    except OSError:
        logger.warning("Could not persist Activity state to %s", path, exc_info=True)


def register_activity_handlers(host: Any, agent: Any) -> None:
    """Wire the Activity handlers on the host's M365 ``AgentApplication``."""
    app = host.agent_app

    @app.activity("message")
    async def on_message(context: Any, state: Any) -> None:  # pyright: ignore[reportUnusedFunction]
        await _run_turn(agent, context, state)

    @app.conversation_update("membersAdded")
    async def on_members_added(context: Any, _state: Any) -> None:  # pyright: ignore[reportUnusedFunction]
        recipient_id = getattr(context.activity.recipient, "id", None)
        for member in context.activity.members_added or []:
            if getattr(member, "id", None) == recipient_id:
                continue
            await _safe_send(context, _WELCOME_TEXT)

    @app.activity("installationUpdate")
    async def on_installation_update(context: Any, state: Any) -> None:  # pyright: ignore[reportUnusedFunction]
        # Teams fires this with action "add" / "remove" (and "*-upgrade") when
        # the app is installed or uninstalled — the only app-lifecycle signal a
        # bot gets, and the one the docs point at for dropping stored user data
        # on uninstall. There is NO event for "Remove chat history", so an
        # uninstall/reinstall is the closest thing to the user asking for a
        # clean slate: wipe both ends of it.
        #
        # Silent on purpose — messages sent after an uninstall are rejected
        # (403), and on install the membersAdded welcome already greets the user.
        action = str(getattr(context.activity, "action", "") or "").lower()
        conversation_id = _conversation_id(context)
        generation = _clear_conversation(state, _conversation_slot(conversation_id))
        logger.info(
            "Activity installationUpdate: action=%s conversation=%s new_generation=%d",
            action, conversation_id, generation,
        )

    @app.error
    async def on_error(context: Any, error: Exception) -> None:  # pyright: ignore[reportUnusedFunction]
        logger.error("Activity handler error: %s", error, exc_info=True)
        await _safe_send(context, _ERROR_TEXT)

    logger.info(
        "Activity protocol handlers registered "
        "(message, conversationUpdate, installationUpdate, error)."
    )


# ──────────────────────────────────────────────
# Turn execution
# ──────────────────────────────────────────────


async def _run_turn(agent: Any, context: Any, state: Any) -> None:
    conversation_id = _conversation_id(context)

    # A tapped Adaptive Card arrives as a `message` activity with no text and
    # the card's Action.Submit payload in `activity.value`.
    submitted = _card_submit(context)
    if submitted is not None:
        user_text, history_text = submitted
    else:
        user_text = history_text = _user_text(context)
    if not user_text:
        return

    slot = _conversation_slot(conversation_id)

    if _is_clear_command(user_text):
        await _handle_clear(context, state, slot, conversation_id)
        return

    generation = _scene_generation(state)
    scene_key = _scene_key(conversation_id, generation) if conversation_id else None
    streaming = _supports_streaming(context)
    logger.info(
        "Activity turn started: channel=%s conversation=%s generation=%d scene_key=%s streaming=%s",
        _channel(context), conversation_id, generation, scene_key, streaming,
    )

    emitter = _StreamingEmitter(context) if streaming else _FallbackEmitter(context)
    relay = _Relay(emitter)
    gallery = _GalleryFilter()
    dedupe = _MediaDedupeFilter()
    reply_parts: list[str] = []

    # `user` carries the scene key into
    # SceneIsolationMiddleware._get_conversation_id (it reads
    # `context.options["user"]`), exactly like the web chat proxy does. It is
    # used for logging / the persisted state file only — the scene file itself
    # has a fixed name per micro-VM.
    options = {"user": scene_key} if scene_key else None
    reply_chars = 0

    async def _persist(epoch: int, history: list[dict[str, str]]) -> None:
        """Write this turn back into the transcript. Called INSIDE the lock."""
        nonlocal reply_chars
        reply = "".join(reply_parts).strip()
        reply_chars = len(reply)
        if not reply:
            return
        if slot.epoch != epoch:
            # /clear landed while this turn was running: the user asked for a
            # blank slate, so this exchange must not be replayed back into it.
            logger.info(
                "Discarding the transcript of a turn superseded by /clear: conversation=%s",
                conversation_id,
            )
            return
        _save_history(
            state,
            slot,
            [
                *history,
                {"role": "user", "text": history_text},
                {"role": "assistant", "text": reply},
            ],
        )
        if relay.detached:
            # The SDK persists TurnState right after the handler returns —
            # which already happened — so this turn's history would be lost
            # unless we write it ourselves. force=True because the SDK's
            # change detection has already seen (and saved) this state.
            try:
                await state.save(context, force=True)
            except Exception:
                logger.warning("Could not persist detached turn state", exc_info=True)

    async def _consume() -> None:
        # Tool calls can run for minutes; without this the channel sees nothing
        # between two status updates and gives up on the turn. The pump starts
        # BEFORE the lock so a turn queued behind a long render keeps its own
        # channel alive while it waits its turn.
        pump = asyncio.create_task(_keepalive_pump(relay))
        try:
            if slot.lock.locked():
                logger.info(
                    "Activity turn queued behind a running turn: conversation=%s",
                    conversation_id,
                )
                await relay.status(_QUEUED_STATUS_TEXT)
            # Held for the WHOLE turn, including after a detach: the work keeps
            # running against the shared Blender scene long after the inbound
            # request completed, so releasing early would let the next message
            # interleave with it.
            async with slot.lock:
                # Read the transcript inside the lock — `state` is a snapshot
                # taken before the turn ahead of us saved, so only the
                # in-process copy `_load_history` prefers is up to date.
                epoch = slot.epoch
                history = _load_history(state, slot)
                messages = [
                    *(
                        Message(role=entry["role"], contents=[Content.from_text(entry["text"])])
                        for entry in history
                    ),
                    Message(role="user", contents=[Content.from_text(user_text)]),
                ]
                try:
                    async for update in agent.run(messages, stream=True, options=options):
                        for content in update.contents or []:
                            if content.type != "text" or not content.text:
                                continue
                            message_id = update.message_id or ""
                            if message_id.startswith(_STATUS_PREFIXES):
                                await relay.status(_clean_status(content.text))
                                if message_id.startswith(_PROGRESS_STATUS_PREFIXES):
                                    await relay.progress()
                            else:
                                # Model prose, plus the tool images / download links that
                                # ToolStatusMiddleware surfaces early (markdown, which
                                # Teams renders) and the middleware's friendly error text.
                                # Keep the RAW text for history so the model still knows
                                # which gallery it offered; the user sees the filtered
                                # version with the fenced JSON replaced by a card.
                                reply_parts.append(content.text)
                                await _emit_filtered(relay, gallery.feed(content.text), dedupe)
                finally:
                    # Still inside the lock: the next turn must not read the
                    # transcript before this one has written itself into it.
                    await _persist(epoch, history)
        except Exception:
            # ToolStatusMiddleware already emitted user-facing error text before
            # re-raising, so only add our own when nothing reached the user.
            logger.error("Activity turn failed", exc_info=True)
            if not reply_parts:
                await relay.text(_ERROR_TEXT)
        finally:
            # Stop the pump before finishing so a keep-alive can't land after
            # (or interleave with) the final message.
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            try:
                await _emit_filtered(relay, gallery.flush(), dedupe, final=True)
            except Exception:
                logger.warning("Could not flush the gallery buffer", exc_info=True)
            # The streaming queue drains in the background, so a Bot Connector
            # delivery failure surfaces here rather than on the individual
            # queue_* calls. Swallow it: an outbound failure must not become a
            # 500 on the inbound webhook, or the connector retries the whole turn.
            try:
                await relay.finish()
            except Exception as exc:
                logger.warning("Could not finish the activity response: %s", exc)

        logger.info(
            "Activity turn finished: conversation=%s reply_chars=%d detached=%s elapsed=%.0fs",
            conversation_id, reply_chars, relay.detached, relay.elapsed,
        )

    turn = asyncio.create_task(_consume())
    turn.add_done_callback(_log_turn_result)
    if _PROACTIVE_AFTER_SECONDS <= 0:
        await turn
        return

    # Fast turns (asset searches, small scenes) finish here and keep the live
    # streaming experience. Slow ones (a high-fidelity render) are handed over
    # so the inbound request can complete before the platform's ~45s timeout
    # turns into an error in the client.
    done, _pending = await asyncio.wait({turn}, timeout=_PROACTIVE_AFTER_SECONDS)
    if done:
        return

    logger.info(
        "Activity turn exceeded %.0fs — detaching for proactive delivery "
        "(conversation=%s)",
        _PROACTIVE_AFTER_SECONDS, conversation_id,
    )
    try:
        await relay.detach(context)
    except Exception:
        # If the handover fails, leave the turn attached: it still delivers
        # through the original context for as long as the process lives.
        logger.error("Could not detach the turn for proactive delivery", exc_info=True)


def _log_turn_result(task: asyncio.Task) -> None:
    if task.cancelled():
        logger.warning("Activity turn was cancelled before delivery")
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Activity turn failed after delivery started: %s", exc, exc_info=exc)


async def _emit_filtered(
    emitter: Any,
    filtered: tuple[str, list[Any]],
    dedupe: Any,
    *,
    final: bool = False,
) -> None:
    """Send the plain-text part of a filtered chunk, then any gallery cards.

    Prose passes through ``dedupe`` so an image or download link the model
    echoes after the middleware already surfaced it is shown only once.
    """
    text, cards = filtered
    text = dedupe.feed(text)
    if final:
        text += dedupe.flush()
    if text:
        await emitter.text(text)
    for card in cards:
        await emitter.card(card)


# ──────────────────────────────────────────────
# /clear
# ──────────────────────────────────────────────


def _is_clear_command(text: str) -> bool:
    return text.strip().lower().rstrip(".!") == _CLEAR_COMMAND


def _clear_conversation(state: Any, slot: _Conversation) -> int:
    """Forget the transcript and hand out a fresh scene key.

    Teams owns the conversation id and keeps it stable even after "Remove chat
    history", so the agent cannot tell that the user wanted a clean slate — the
    persisted ``scene.blend`` is happily restored on the next turn.

    Rotating the *scene key* reproduces exactly what the web chat's Reset
    button does by rotating its conversation UUID:
    ``SceneManager.is_conversation_reset`` sees an id that differs from the one
    recorded by the last ``save_scene`` and resets Blender to a clean scene
    instead of loading the saved file. The reset therefore lands on the NEXT
    message, which is why the confirmation says so.

    Deliberately does NOT wait on ``slot.lock``: a render already in flight can
    hold it for minutes, and /clear has to answer now. Bumping the epoch is what
    makes that safe — the running turn checks it before writing its transcript
    back and drops it instead.
    """
    generation = _bump_scene_generation(state)
    slot.epoch += 1
    _save_history(state, slot, [])
    return generation


async def _handle_clear(
    context: Any, state: Any, slot: _Conversation, conversation_id: str | None
) -> None:
    generation = _clear_conversation(state, slot)
    logger.info(
        "Activity /clear: conversation=%s new_generation=%d new_scene_key=%s",
        conversation_id,
        generation,
        _scene_key(conversation_id, generation) if conversation_id else None,
    )
    await _safe_send(context, _CLEAR_TEXT)


def _scene_generation(state: Any) -> int:
    try:
        value = state.conversation.get_value(_SCENE_GENERATION_KEY)
    except Exception:  # pragma: no cover - defensive
        logger.warning("Could not read the scene generation", exc_info=True)
        return 0
    return value if isinstance(value, int) and value >= 0 else 0


def _bump_scene_generation(state: Any) -> int:
    generation = _scene_generation(state) + 1
    try:
        state.conversation.set_value(_SCENE_GENERATION_KEY, generation)
    except Exception:  # pragma: no cover - defensive
        logger.warning("Could not persist the scene generation", exc_info=True)
    return generation


# ──────────────────────────────────────────────
# Adaptive Card galleries
# ──────────────────────────────────────────────


class _GalleryFilter:
    """Splits the model's streamed text into prose and gallery Adaptive Cards.

    The system prompt makes the model answer asset searches with a fenced
    ```` ```models ```` / ```` ```textures ```` block containing the tool's raw
    JSON. The web client renders those as a thumbnail gallery; dumped into
    Teams verbatim they are a wall of JSON.

    A card cannot be built until the whole block has arrived, so text is
    streamed straight through until a fence opens, then held back until it
    closes. Fences that are not gallery tags (```` ```python ````, ````
    ```json ````) pass through untouched, and a fence that never closes is
    emitted verbatim at flush time rather than swallowed.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> tuple[str, list[Any]]:
        self._buffer += chunk
        return self._drain(final=False)

    def flush(self) -> tuple[str, list[Any]]:
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> tuple[str, list[Any]]:
        out: list[str] = []
        cards: list[Any] = []

        while True:
            start = self._buffer.find("```")
            if start == -1:
                break
            newline = self._buffer.find("\n", start + 3)
            end = self._buffer.find("```", newline + 1) if newline != -1 else -1
            if newline == -1 or end == -1:
                if final:
                    break  # unterminated fence — fall through and emit raw
                # Hold the incomplete block; emit only what precedes it.
                out.append(self._buffer[:start])
                self._buffer = self._buffer[start:]
                return "".join(out), cards

            tag = self._buffer[start + 3:newline].strip().lower()
            body = self._buffer[newline + 1:end]
            out.append(self._buffer[:start])
            card = _gallery_card(tag, body) if tag in _GALLERY_TAGS else None
            if card is not None:
                cards.append(card)
            else:
                out.append(self._buffer[start:end + 3])
            self._buffer = self._buffer[end + 3:]

        if final:
            out.append(self._buffer)
            self._buffer = ""
        else:
            # A fence can be split across chunks, so never emit a trailing
            # partial "`" / "``" that might turn out to open one.
            hold = 2 if self._buffer.endswith("``") else 1 if self._buffer.endswith("`") else 0
            if hold:
                out.append(self._buffer[:-hold])
                self._buffer = self._buffer[-hold:]
            else:
                out.append(self._buffer)
                self._buffer = ""

        return "".join(out), cards


# ──────────────────────────────────────────────
# Duplicate image / download-link suppression
# ──────────────────────────────────────────────


def _is_download_link(url: str) -> bool:
    """Heuristic: does this URL point at a downloadable scene file?"""
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    return path.endswith(_DOWNLOAD_SUFFIXES)


def _is_partial_media(text: str) -> bool:
    """Whether ``text`` could still grow into a complete media token."""
    i = 1 if text.startswith("!") else 0
    if not text[i:].startswith("["):
        return False
    close = text.find("]", i + 1)
    if close == -1:
        return "\n" not in text[i + 1:]
    if close + 1 >= len(text):
        return True
    if text[close + 1] != "(":
        return False
    rest = text[close + 2:]
    # A ")" here means the regex already had its chance and did not match.
    return "\n" not in rest and ")" not in rest


class _MediaDedupeFilter:
    """Drops repeated markdown images / download links within one reply.

    The agent emits some media twice: ``ToolStatusMiddleware`` surfaces the
    tool result early (so the render shows up the moment it exists) and the
    model then echoes the same markdown in its prose. The web client hides the
    second copy with ``dedupeMarkdownMedia()``; Teams and M365 Copilot render
    whatever text we queue, so the same suppression has to happen here.

    Unlike the client, which sees the finished message, this runs on the live
    stream: a token can be split across chunks, so an incomplete one is held
    back (up to ``_MAX_MEDIA_HOLD``) until it either completes or is proven not
    to be media. State is per-turn and keyed on the URL — blob URLs carry a
    unique timestamp + uuid, so distinct files never collide.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._seen: set[str] = set()

    def feed(self, chunk: str) -> str:
        self._buffer += chunk
        return self._drain(final=False)

    def flush(self) -> str:
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> str:
        buf = self._buffer
        out: list[str] = []
        emit_from = 0  # everything before this was already emitted or dropped
        pos = 0

        while True:
            start = buf.find("[", pos)
            if start == -1:
                break
            if start > 0 and buf[start - 1] == "!":
                start -= 1

            match = _MEDIA_RE.match(buf, start)
            if match is None:
                if (
                    not final
                    and len(buf) - start < _MAX_MEDIA_HOLD
                    and _is_partial_media(buf[start:])
                ):
                    out.append(buf[emit_from:start])
                    self._buffer = buf[start:]
                    return "".join(out)
                pos = start + (2 if buf[start] == "!" else 1)
                continue

            url = match.group(2)
            if match.group(1) == "!" or _is_download_link(url):
                if url in self._seen:
                    out.append(buf[emit_from:start])
                    emit_from = match.end()
                    logger.debug("Dropped duplicate media for %s", url)
                else:
                    self._seen.add(url)
            pos = match.end()

        # A trailing "!" may be the start of an image whose "[" is in the next
        # chunk; holding it keeps the token recognisable.
        tail = 1 if not final and buf.endswith("!") else 0
        out.append(buf[emit_from:len(buf) - tail])
        self._buffer = buf[len(buf) - tail:] if tail else ""
        return "".join(out)


def _gallery_card(tag: str, body: str) -> Any | None:
    """Build an Adaptive Card attachment from a gallery JSON block.

    Returns ``None`` when the block isn't usable, in which case the caller
    falls back to emitting the original text so nothing is silently lost.
    """
    try:
        items = json.loads(body.strip())
    except (ValueError, TypeError):
        logger.info("Gallery block for '%s' was not valid JSON — leaving as text", tag)
        return None
    if not isinstance(items, list) or not items:
        return None

    is_models = tag == "models"
    rows: list[dict[str, Any]] = []
    for item in items[:_MAX_CARD_ITEMS]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "Untitled")
        reference = item.get("modelUrl") if is_models else item.get("assetId")
        if not isinstance(reference, str) or not reference:
            continue

        data = (
            {"action": "load_model", "name": name, "modelUrl": reference}
            if is_models
            else {"action": "apply_texture", "name": name, "assetId": reference}
        )

        columns: list[dict[str, Any]] = []
        image_url = item.get("imageUrl")
        if isinstance(image_url, str) and image_url:
            columns.append({
                "type": "Column",
                "width": "auto",
                "items": [{"type": "Image", "url": image_url, "size": "Medium", "altText": name}],
            })
        columns.append({
            "type": "Column",
            "width": "stretch",
            "verticalContentAlignment": "Center",
            "items": [
                {"type": "TextBlock", "text": name, "weight": "Bolder", "wrap": True},
                {
                    "type": "TextBlock",
                    "text": "Tap to add it to the scene" if is_models else "Tap to use this texture",
                    "isSubtle": True,
                    "spacing": "None",
                    "wrap": True,
                },
            ],
        })
        rows.append({
            "type": "ColumnSet",
            "columns": columns,
            "separator": True,
            # selectAction makes the whole row tappable; the payload comes back
            # on the next message activity as `activity.value`.
            "selectAction": {"type": "Action.Submit", "title": name, "data": data},
        })

    if not rows:
        return None

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": "3D models you can add" if is_models else "Textures you can apply",
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
            },
            *rows,
        ],
    }
    return Attachment(content_type=_ADAPTIVE_CARD_CONTENT_TYPE, content=card)


def _card_submit(context: Any) -> tuple[str, str] | None:
    """Turn a tapped card into ``(prompt, transcript_text)``.

    The two differ on purpose. ``prompt`` carries the tool-directing
    scaffolding the model needs *this turn*; ``transcript_text`` is the plain
    thing the user actually did. Storing the scaffolding would replay tool
    names and argument literals back as if the user had said them, teaching the
    model that this is how requests look and burning budget on a URL it will
    never need again.

    Unlike the reference sample's to-do buttons, these actions are not
    deterministic CRUD that can be applied directly: importing a model is
    followed by a viewport screenshot, and a texture may need disambiguating
    against the current scene. Both need the model's judgment, so the turn goes
    through the agent rather than straight to the tool.
    """
    value = getattr(context.activity, "value", None)
    if not isinstance(value, dict):
        return None

    action = value.get("action")
    name = str(value.get("name") or "").strip() or "the one I picked"

    if action == "load_model":
        model_url = value.get("modelUrl")
        if isinstance(model_url, str) and model_url:
            return (
                f'I picked the 3D model "{name}". Import it by calling download_model with '
                f'model_url="{model_url}" and a short descriptive name, then take ONE '
                f"viewport screenshot so I can see it.",
                f'Add the 3D model "{name}" to the scene.',
            )
    elif action == "apply_texture":
        asset_id = value.get("assetId")
        if isinstance(asset_id, str) and asset_id:
            return (
                f'I picked the texture "{name}" (assetId "{asset_id}"). Apply it with '
                f"apply_texture — if it is not obvious which object I mean, ask me first.",
                f'Apply the texture "{name}".',
            )
    return None


# ──────────────────────────────────────────────
# Emitters
# ──────────────────────────────────────────────


class _Emitter:
    """Shared bookkeeping: what was said last, and how long ago."""

    def __init__(self, context: Any) -> None:
        self._context = context
        self._started = time.monotonic()
        self._last_sent = self._started
        self._last_status = ""

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    @property
    def idle(self) -> float:
        """Seconds since anything was last put on the wire."""
        return time.monotonic() - self._last_sent

    def _touch(self) -> None:
        self._last_sent = time.monotonic()

    def _keepalive_text(self) -> str:
        """Restate the live status rather than a canned 'please wait'."""
        detail = self._last_status.rstrip("…. ") or _KEEPALIVE_FALLBACK_TEXT
        return f"{detail} — still working ({int(self.elapsed)}s)"

    async def keepalive(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def progress(self) -> None:
        """Interim update at a slow step. Only detached turns need one — the
        live emitters are already showing status as it happens."""
        return None

    async def tick(self) -> None:
        """Called on every pump poll, whether or not anything is due."""
        return None


async def _keepalive_pump(emitter: _Emitter) -> None:
    """Nudge the channel while the agent is busy but silent."""
    try:
        while True:
            await asyncio.sleep(_KEEPALIVE_POLL_SECONDS)
            # Deadlines are checked on every poll, not only when a keep-alive
            # is due: with a 20s keep-alive interval, a quiet turn would
            # otherwise notice the stream cap up to 20s late — long enough for
            # Teams to have killed the stream first.
            await emitter.tick()
            if emitter.idle >= _KEEPALIVE_SECONDS:
                await emitter.keepalive()
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - defensive
        # A dead pump must never fail the turn; the answer still gets through.
        logger.warning("Keep-alive pump stopped", exc_info=True)


class _StreamingEmitter(_Emitter):
    """Streams via the M365 streaming response (Teams, WebChat, DirectLine)."""

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._stream = context.streaming_response
        self._stream.set_generated_by_ai_label(True)
        self._sent_text = False
        self._sent_card = False
        # Set once the two-minute cap forces the stream shut; everything after
        # that goes out as ordinary messages.
        self._overflow: _FallbackEmitter | None = None

    async def status(self, text: str) -> None:
        self._last_status = text
        target = await self._target()
        if target is not None:
            await target.status(text)
            return
        # Renders as a live "thinking" line above the reply in Teams.
        self._stream.queue_informative_update(text)
        self._touch()

    async def text(self, chunk: str) -> None:
        target = await self._target()
        if target is not None:
            await target.text(chunk)
            return
        self._sent_text = True
        self._stream.queue_text_chunk(chunk)
        self._touch()

    async def card(self, attachment: Any) -> None:
        target = await self._target()
        if target is not None:
            await target.card(attachment)
            return
        # Attachments ride on the FINAL message the stream emits, which is the
        # only place the M365 SDK allows them.
        self._sent_card = True
        self._stream.add_attachment(attachment)

    async def keepalive(self) -> None:
        target = await self._target()
        if target is not None:
            await target.keepalive()
            return
        # Teams stops *rendering* informative updates once real text has been
        # streamed, but they still count as stream traffic, and by then the
        # partial answer plus the typing indicator already show progress. Never
        # inject keep-alive noise into the answer body: streamed content is
        # cumulative, so it could not be taken back out.
        self._stream.queue_informative_update(self._keepalive_text())
        self._touch()

    async def tick(self) -> None:
        # Close the stream on time even if the turn has gone completely quiet.
        await self._target()

    async def finish(self) -> None:
        if self._overflow is not None:
            await self._overflow.finish()
            return
        await self._end_stream(
            _GALLERY_ONLY_TEXT if self._sent_card else _EMPTY_REPLY_TEXT
        )

    async def _target(self) -> _FallbackEmitter | None:
        """Close the stream before Teams' two-minute cap kills it.

        Returns the plain-message emitter that took over, or ``None`` while the
        stream is still healthy.
        """
        if self._overflow is None and self.elapsed > _STREAM_MAX_SECONDS:
            logger.info(
                "Closing the Teams stream after %.0fs (cap %.0fs) and "
                "continuing with plain messages",
                self.elapsed, _STREAM_MAX_SECONDS,
            )
            try:
                await self._end_stream(_STREAM_CONTINUED_TEXT)
            except Exception as exc:  # pragma: no cover - network dependent
                logger.warning("Could not close the stream cleanly: %s", exc)
            self._overflow = _FallbackEmitter(self._context)
            # Carry the turn clock over so the keep-alive keeps counting from
            # the user's message, not from the handover.
            self._overflow._started = self._started
            self._overflow._last_status = self._last_status
        return self._overflow

    async def _end_stream(self, placeholder: str) -> None:
        if not self._sent_text:
            # end_stream() falls back to a placeholder string when the message
            # is empty; send something intentional instead.
            self._sent_text = True
            self._stream.queue_text_chunk(placeholder)
        await self._stream.end_stream()


class _FallbackEmitter(_Emitter):
    """Non-streaming channels — notably M365 Copilot's agentic requests.

    Informative updates are dropped on these channels, so each status becomes
    its own message activity. That is chattier than a live status line, but it
    is the only way the custom waiting text reaches the user today — and it is
    also what keeps a long turn from being abandoned, since those messages are
    the only traffic the channel sees while a tool runs.
    """

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._parts: list[str] = []
        self._attachments: list[Any] = []

    async def status(self, text: str) -> None:
        self._last_status = text
        await _safe_send(self._context, text)
        self._touch()

    async def text(self, chunk: str) -> None:
        # Buffered until finish(), so this is deliberately not a _touch().
        self._parts.append(chunk)

    async def card(self, attachment: Any) -> None:
        self._attachments.append(attachment)

    async def keepalive(self) -> None:
        await _safe_send(self._context, self._keepalive_text())
        self._touch()

    async def finish(self) -> None:
        body = "".join(self._parts).strip()
        if not body:
            body = _GALLERY_ONLY_TEXT if self._attachments else _EMPTY_REPLY_TEXT
        await _safe_send(
            self._context,
            Activity(type="message", text=body, attachments=self._attachments or []),
        )


class _ProactiveEmitter(_Emitter):
    """Delivers the rest of a detached turn as a proactive message.

    Once the turn is detached the inbound request is already answered, so there
    is no stream and no request context to reply on. Output is buffered and
    sent as ONE message through ``ChannelServiceAdapter.continue_conversation``,
    which builds a fresh turn context (and a fresh connector client, so tokens
    are re-acquired) from the conversation reference captured on the way in.

    Statuses and keep-alives are dropped on purpose: the user has just been
    told the work continues in the background, so the next thing they should
    see is the result.
    """

    def __init__(self, context: Any, previous: _Emitter) -> None:
        super().__init__(context)
        # Keep the original turn clock so logs and any elapsed text stay honest.
        self._started = previous._started
        self._last_status = previous._last_status
        self._adapter = context.adapter
        self._app_id = _agent_app_id(context)
        # Everything needed to rebuild the inbound turn's connector client. The
        # client itself cannot be reused: `process_activity` closes its aiohttp
        # session as soon as the handler returns ("Session is closed").
        self._identity = getattr(context, "identity", None)
        self._factory = context.turn_state.get(
            getattr(self._adapter, "CHANNEL_SERVICE_FACTORY_KEY", "")
        )
        self._audience = context.turn_state.get(
            getattr(self._adapter, "OAUTH_SCOPE_KEY", "")
        )
        self._reference = context.activity.get_conversation_reference()
        self._parts: list[str] = []
        self._attachments: list[Any] = []

    async def status(self, text: str) -> None:
        self._last_status = text

    async def text(self, chunk: str) -> None:
        self._parts.append(chunk)

    async def card(self, attachment: Any) -> None:
        self._attachments.append(attachment)

    async def keepalive(self) -> None:
        return None

    async def progress(self) -> None:
        """Send what has been produced so far, ahead of the slow step.

        Everything buffered is handed over now — typically the viewport
        screenshot `ToolStatusMiddleware` surfaced — and the buffer is cleared
        so the final message only carries the render and the wrap-up prose.
        """
        body = "".join(self._parts).strip()
        attachments = self._attachments
        if body or attachments:
            note = _PROGRESS_WITH_MEDIA_TEXT if _MEDIA_RE.search(body) else ""
            activity = Activity(
                type="message",
                text=f"{body}{note}".strip() or _PROGRESS_PLAIN_TEXT,
                attachments=attachments or [],
            )
        else:
            activity = Activity(type="message", text=_PROGRESS_PLAIN_TEXT)
        self._parts = []
        self._attachments = []
        await self._send(activity)

    async def finish(self) -> None:
        body = "".join(self._parts).strip()
        if not body and not self._attachments:
            return
        if not body:
            body = _GALLERY_ONLY_TEXT
        activity = Activity(
            type="message", text=body, attachments=self._attachments or []
        )
        await self._send(activity)

    async def _deliver_direct(self, activity: Any) -> bool:
        """Post the activity with a connector client built like the inbound one.

        This is the primary path because `continue_conversation*` cannot work
        on this host: `process_proactive()` always builds a `UserTokenClient`
        for the OAuth flow and — unlike `process_activity()` — never computes
        the anonymous-auth flag or passes the token scopes, so it dies with
        ``ManagedIdentityError: You shall specify one of the three parameters:
        client_id, resource_id, object_id`` before it ever reaches the send.

        Posting an activity needs no user token at all, so the OAuth machinery
        is skipped and the inbound client's construction is mirrored instead —
        same identity, audience, scopes and anonymous flag, which is what makes
        this work both locally (anonymous) and in Foundry (managed identity).
        """
        scopes = self._identity.get_token_scope()
        anonymous = (
            not self._identity.is_authenticated
            and self._identity.authentication_type == "Anonymous"
        )
        client = await self._factory.create_connector_client(
            self._context,
            self._identity,
            self._reference.service_url,
            self._audience,
            scopes,
            anonymous,
        )
        try:
            activity.apply_conversation_reference(self._reference)
            activity.id = None
            conversation_id = self._reference.conversation.id
            if activity.reply_to_id:
                await client.conversations.reply_to_activity(
                    conversation_id, activity.reply_to_id, activity
                )
            else:
                await client.conversations.send_to_conversation(
                    conversation_id, activity
                )
        finally:
            with contextlib.suppress(Exception):
                await client.close()
        return True

    def _continuation(self) -> Any:
        activity = self._reference.get_continuation_activity()
        # `TurnContext.send_activities` copies the *context* activity's id onto
        # every outgoing message as `reply_to_id`, and the adapter then routes
        # anything with a reply_to_id through `reply_to_activity`. Since
        # `get_continuation_activity()` mints a fresh uuid, that would make the
        # channel reply to a message that never existed. Clearing the id routes
        # the message through `send_to_conversation` instead — which is what a
        # proactive message actually is.
        activity.id = None
        return activity

    async def _deliver_via(self, start: Any, activity: Any) -> bool:
        delivered = False

        async def _callback(turn_context: Any) -> None:
            nonlocal delivered
            await turn_context.send_activity(activity)
            delivered = True

        await start(_callback)
        return delivered

    async def _deliver_with_claims(self, activity: Any) -> bool:
        return await self._deliver_via(
            lambda callback: self._adapter.continue_conversation_with_claims(
                claims_identity=self._identity,
                continuation_activity=self._continuation(),
                callback=callback,
                audience=self._audience,
            ),
            activity,
        )

    async def _deliver_with_app_id(self, activity: Any) -> bool:
        return await self._deliver_via(
            lambda callback: self._adapter.continue_conversation(
                agent_app_id=self._app_id,
                continuation_activity=self._continuation(),
                callback=callback,
            ),
            activity,
        )

    def _delivery_attempts(self) -> list[tuple[str, Any]]:
        attempts: list[tuple[str, Any]] = []
        if self._factory is not None and self._identity is not None and self._audience:
            attempts.append(("a fresh connector client", self._deliver_direct))
        if self._identity is not None:
            attempts.append(("the inbound claims identity", self._deliver_with_claims))
        if self._app_id:
            attempts.append(("the agent app id", self._deliver_with_app_id))
        return attempts

    async def _send(self, activity: Any) -> None:
        logger.info(
            "Delivering proactive message: conversation=%s service_url=%s "
            "audience=%s app_id=%s chars=%d attachments=%d",
            getattr(self._reference.conversation, "id", None),
            self._reference.service_url, self._audience, self._app_id,
            len(activity.text or ""), len(activity.attachments or []),
        )

        for label, attempt in self._delivery_attempts():
            try:
                delivered = await attempt(activity)
            except Exception as exc:
                logger.warning("Proactive delivery via %s failed: %s", label, exc)
                continue

            if delivered:
                logger.info(
                    "Proactive message delivered via %s after %.0fs",
                    label, self.elapsed,
                )
                return

            # `ChannelAdapter.run_pipeline` hands callback errors to
            # `on_turn_error` and returns normally, so a clean return does NOT
            # mean the message went out. Without this check a failed delivery
            # looks like a success and the user simply never hears back.
            logger.warning(
                "Proactive delivery via %s returned without sending anything "
                "(the adapter's on_turn_error swallowed the failure)", label,
            )

        # Long shot: the request's own connector client is closed by
        # `process_activity` as soon as the handler returns, so this usually
        # fails with "Session is closed" — but it costs nothing.
        logger.error("All proactive delivery attempts failed — trying the request context")
        await _safe_send(self._context, activity)


class _Relay:
    """Indirection so a running turn can change where its output goes.

    The consumer loop and the keep-alive pump both talk to the relay, which
    forwards to the emitter that currently owns delivery. Handing a slow turn
    over to proactive delivery is then a single swap, with no coordination
    needed inside the loop.
    """

    def __init__(self, emitter: _Emitter) -> None:
        self._inner = emitter
        self._detached = False

    @property
    def detached(self) -> bool:
        return self._detached

    @property
    def elapsed(self) -> float:
        return self._inner.elapsed

    @property
    def idle(self) -> float:
        return self._inner.idle

    async def status(self, text: str) -> None:
        await self._inner.status(text)

    async def text(self, chunk: str) -> None:
        await self._inner.text(chunk)

    async def card(self, attachment: Any) -> None:
        await self._inner.card(attachment)

    async def keepalive(self) -> None:
        await self._inner.keepalive()

    async def progress(self) -> None:
        await self._inner.progress()

    async def tick(self) -> None:
        await self._inner.tick()

    async def finish(self) -> None:
        await self._inner.finish()

    async def detach(self, context: Any) -> None:
        """Close out the live response and continue in the background."""
        if self._detached:
            return
        previous = self._inner
        proactive = _ProactiveEmitter(context, previous)
        # Say so on the live response *before* swapping, so the notice lands on
        # the stream the user is currently watching.
        await previous.text(_HANDOVER_TEXT)
        await previous.finish()
        self._inner = proactive
        self._detached = True


def _agent_app_id(context: Any) -> str:
    """The bot's Entra app id, needed to mint outbound (proactive) credentials.

    Teams addresses the agent as ``28:<appId>`` in ``recipient.id``; the env
    var is an escape hatch for channels that don't.
    """
    configured = os.environ.get("ACTIVITY_AGENT_APP_ID", "").strip()
    if configured:
        return configured
    recipient_id = getattr(getattr(context.activity, "recipient", None), "id", "") or ""
    return recipient_id.split(":", 1)[-1] if recipient_id else ""


async def _safe_send(context: Any, message: Any) -> None:
    """Send an activity (or plain text), logging — not raising — on failure.

    Outbound delivery goes to the Bot Connector. A transient failure there must
    not surface as a 500 on the inbound webhook, or the connector retries the
    whole turn.
    """
    try:
        await context.send_activity(message)
    except Exception as exc:  # pragma: no cover - network dependent
        logger.warning("Could not send activity: %s", exc)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


def _channel(context: Any) -> Any:
    """Return the channel id, unwrapping the SDK's ChannelId wrapper."""
    channel_id = getattr(context.activity, "channel_id", None)
    return getattr(channel_id, "channel", channel_id)


def _supports_streaming(context: Any) -> bool:
    """Mirror ``StreamingResponse._set_defaults`` without reading private state.

    Teams supports streaming *except* for agentic requests (the M365 Copilot
    path), where the SDK disables it.
    """
    activity = context.activity
    channel = _channel(context)
    if channel == Channels.ms_teams:
        try:
            return not activity.is_agentic_request()
        except Exception:  # pragma: no cover - defensive
            return True
    if channel in (Channels.webchat, Channels.direct_line):
        return True
    return activity.delivery_mode == DeliveryModes.stream


def _user_text(context: Any) -> str:
    """The user's message with any @-mention of the agent removed."""
    text = context.activity.text or ""
    try:
        stripped = context.remove_recipient_mention()
        if isinstance(stripped, str):
            text = stripped
    except Exception:  # pragma: no cover - channels without mentions
        pass
    return text.strip()


def _conversation_id(context: Any) -> str | None:
    conversation = getattr(context.activity, "conversation", None)
    conversation_id = getattr(conversation, "id", None)
    return conversation_id if isinstance(conversation_id, str) and conversation_id else None


def _scene_key(conversation_id: str, generation: int = 0) -> str:
    """Derive a short, stable scene key from the channel conversation id.

    The key travels to the model as ChatOptions ``user``, which the Responses
    API caps at 64 characters. Real Teams conversation ids are ~131 chars and
    were rejected with HTTP 400 ``string_above_max_length``, failing the whole
    turn, so the raw id cannot be used.

    A SHA-256 digest is short enough AND deterministic, which matters just as
    much: ``SceneIsolationMiddleware.is_conversation_reset`` treats a *changed*
    id as a new conversation and starts a fresh scene, so anything random or
    per-turn would silently discard the user's work on every message.

    ``generation`` is bumped by /clear (and by install/uninstall) to
    deliberately produce a different key and trigger exactly that reset.
    Generation 0 hashes the bare conversation id so keys minted before /clear
    existed stay valid — otherwise upgrading would wipe every live Teams scene
    once.
    """
    seed = conversation_id if generation <= 0 else f"{conversation_id}#{generation}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    return f"teams-{digest}"


def _clean_status(text: str) -> str:
    """Unwrap the ``\\n\\n*status*\\n\\n`` marker the middleware emits."""
    return text.strip().strip("*").strip()


# ──────────────────────────────────────────────
# Conversation registry — turn serialisation + transcript
# ──────────────────────────────────────────────


class _Conversation:
    """Per-conversation turn lock and the authoritative transcript.

    Two problems are solved here.

    **Overlapping turns.** Teams and M365 Copilot deliver activities in bursts,
    and nothing upstream serialises them. Two turns running at once would drive
    the SINGLE shared Blender scene concurrently — one turn's ``clear_scene``
    landing in the middle of another's render — and would clobber each other on
    the read-modify-write of the transcript below.

    **Stale snapshots.** ``TurnState`` is loaded from storage *before* the
    handler runs, so a turn queued behind a long render holds a snapshot that
    predates the previous turn's save and would replay a transcript missing the
    last exchange. The in-process copy here is therefore the source of truth for
    the life of the container, and ``TurnState`` is the durable write-through.
    """

    __slots__ = ("lock", "history", "loaded", "epoch")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.history: list[dict[str, str]] = []
        self.loaded = False
        # Bumped by /clear so a turn already in flight knows not to write its
        # transcript back over the fresh slate it was cleared into.
        self.epoch = 0


_conversations: dict[str, _Conversation] = {}


def _conversation_slot(conversation_id: str | None) -> _Conversation:
    key = conversation_id or "__default__"
    slot = _conversations.get(key)
    if slot is not None:
        return slot
    for stale in list(_conversations):
        if len(_conversations) < _MAX_TRACKED_CONVERSATIONS:
            break
        # Never evict a conversation with a turn in flight — its lock and
        # transcript are still in use.
        if not _conversations[stale].lock.locked():
            del _conversations[stale]
    slot = _Conversation()
    _conversations[key] = slot
    return slot


def _sanitize_history(history: Any) -> list[dict[str, str]]:
    if not isinstance(history, list):
        return []
    return [
        entry
        for entry in history
        if isinstance(entry, dict)
        and isinstance(entry.get("text"), str)
        and entry.get("role") in ("user", "assistant")
    ]


def _trim_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep the newest tail that fits both the message and the character budget.

    The most recent exchange is always kept even when it alone blows the budget:
    dropping it would leave the model answering with no idea what was just
    asked, which is worse than one oversized turn.
    """
    budget = _MAX_HISTORY_CHARS
    kept: list[dict[str, str]] = []
    for entry in reversed(history[-_MAX_HISTORY_MESSAGES:]):
        budget -= len(entry["text"])
        if budget < 0 and kept:
            break
        kept.append(entry)
    kept.reverse()
    return kept


def _load_history(state: Any, slot: _Conversation) -> list[dict[str, str]]:
    """The transcript to replay. Call INSIDE ``slot.lock``."""
    if slot.loaded:
        return list(slot.history)
    history: Any = None
    try:
        history = state.conversation.get_value(_HISTORY_KEY)
    except Exception:  # pragma: no cover - defensive
        logger.warning("Could not read Activity conversation history", exc_info=True)
    slot.history = _trim_history(_sanitize_history(history))
    slot.loaded = True
    return list(slot.history)


def _save_history(state: Any, slot: _Conversation, history: list[dict[str, str]]) -> None:
    """Update the in-process transcript and write it through. Call INSIDE the lock."""
    trimmed = _trim_history(_sanitize_history(history))
    slot.history = trimmed
    slot.loaded = True
    try:
        state.conversation.set_value(_HISTORY_KEY, trimmed)
    except Exception:  # pragma: no cover - defensive
        logger.warning("Could not persist Activity conversation history", exc_info=True)
