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

import logging
import os
from typing import Any

logger = logging.getLogger("blender_agent.activity")

# ── Update routing ──────────────────────────────────────────────────────────
# Message ids stamped by ToolStatusMiddleware / SceneIsolationMiddleware in
# main.py. Keep these in sync with the `message_id=` arguments there.
_STATUS_PREFIXES = ("status-", "scene-status-")

# How many prior messages (user + assistant) to replay on each turn. The
# Activity path has NO Foundry-managed history — unlike /responses, which gets
# it from the hosting layer — so we keep our own in the M365 conversation
# state.
_MAX_HISTORY_MESSAGES = 20
_HISTORY_PATH = "conversation.blender_history"

_WELCOME_TEXT = (
    "Hi! I'm a 3D scene assistant powered by Blender. Ask me to build a scene "
    "— for example *\"create a small cabin on a snowy hill and render it\"* — "
    "and I'll model it, texture it and send you back a render."
)

_EMPTY_REPLY_TEXT = "I finished that step but didn't have anything to add."

_ERROR_TEXT = (
    "Sorry — something went wrong while working on your scene. "
    "Please try again, or rephrase your request."
)

# Populated by the guarded import below; `activity_available()` reports on it.
_IMPORT_ERROR: Exception | None = None

try:
    from agent_framework import Content, Message
    from agent_framework_foundry_hosting import ResponsesHostServer
    from azure.ai.agentserver.activity import ActivityAgentServerHost
    from microsoft_agents.activity import Channels, DeliveryModes
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

    host = BlenderAgentHost(agent=agent)
    paths = sorted({getattr(r, "path", str(r)) for r in host.router.routes})
    logger.info("Multi-protocol host routes: %s", paths)
    return host


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

    @app.error
    async def on_error(context: Any, error: Exception) -> None:  # pyright: ignore[reportUnusedFunction]
        logger.error("Activity handler error: %s", error, exc_info=True)
        await _safe_send(context, _ERROR_TEXT)

    logger.info("Activity protocol handlers registered (message, conversationUpdate, error).")


# ──────────────────────────────────────────────
# Turn execution
# ──────────────────────────────────────────────


async def _run_turn(agent: Any, context: Any, state: Any) -> None:
    user_text = _user_text(context)
    if not user_text:
        return

    conversation_id = _conversation_id(context)
    streaming = _supports_streaming(context)
    logger.info(
        "Activity turn started: channel=%s conversation=%s streaming=%s",
        _channel(context), conversation_id, streaming,
    )

    history = _load_history(state)
    messages = [
        *(
            Message(role=entry["role"], contents=[Content.from_text(entry["text"])])
            for entry in history
        ),
        Message(role="user", contents=[Content.from_text(user_text)]),
    ]

    emitter = _StreamingEmitter(context) if streaming else _FallbackEmitter(context)
    reply_parts: list[str] = []

    # `user` carries the Teams conversation id into
    # SceneIsolationMiddleware._get_conversation_id (it reads
    # `context.options["user"]`), exactly like the web chat proxy does. It is
    # used for logging / the persisted state file only — the scene file itself
    # has a fixed name per micro-VM.
    options = {"user": conversation_id} if conversation_id else None

    try:
        async for update in agent.run(messages, stream=True, options=options):
            for content in update.contents or []:
                if content.type != "text" or not content.text:
                    continue
                message_id = update.message_id or ""
                if message_id.startswith(_STATUS_PREFIXES):
                    await emitter.status(_clean_status(content.text))
                else:
                    # Model prose, plus the tool images / download links that
                    # ToolStatusMiddleware surfaces early (markdown, which
                    # Teams renders) and the middleware's friendly error text.
                    reply_parts.append(content.text)
                    await emitter.text(content.text)
    except Exception:
        # ToolStatusMiddleware already emitted user-facing error text before
        # re-raising, so only add our own when nothing reached the user.
        logger.error("Activity turn failed", exc_info=True)
        if not reply_parts:
            await emitter.text(_ERROR_TEXT)
    finally:
        # The streaming queue drains in the background, so a Bot Connector
        # delivery failure surfaces here rather than on the individual
        # queue_* calls. Swallow it: an outbound failure must not become a
        # 500 on the inbound webhook, or the connector retries the whole turn.
        try:
            await emitter.finish()
        except Exception as exc:
            logger.warning("Could not finish the activity response: %s", exc)

    reply = "".join(reply_parts).strip()
    if reply:
        _save_history(
            state,
            [
                *history,
                {"role": "user", "text": user_text},
                {"role": "assistant", "text": reply},
            ],
        )
    logger.info("Activity turn finished: conversation=%s reply_chars=%d", conversation_id, len(reply))


# ──────────────────────────────────────────────
# Emitters
# ──────────────────────────────────────────────


class _StreamingEmitter:
    """Streams via the M365 streaming response (Teams, WebChat, DirectLine)."""

    def __init__(self, context: Any) -> None:
        self._context = context
        self._stream = context.streaming_response
        self._stream.set_generated_by_ai_label(True)
        self._sent_text = False

    async def status(self, text: str) -> None:
        # Renders as a live "thinking" line above the reply in Teams.
        self._stream.queue_informative_update(text)

    async def text(self, chunk: str) -> None:
        self._sent_text = True
        self._stream.queue_text_chunk(chunk)

    async def finish(self) -> None:
        if not self._sent_text:
            # end_stream() falls back to a placeholder string when the message
            # is empty; send something intentional instead.
            self._stream.queue_text_chunk(_EMPTY_REPLY_TEXT)
        await self._stream.end_stream()


class _FallbackEmitter:
    """Non-streaming channels — notably M365 Copilot's agentic requests.

    Informative updates are dropped on these channels, so each status becomes
    its own message activity. That is chattier than a live status line, but it
    is the only way the custom waiting text reaches the user today.
    """

    def __init__(self, context: Any) -> None:
        self._context = context
        self._parts: list[str] = []

    async def status(self, text: str) -> None:
        await _safe_send(self._context, text)

    async def text(self, chunk: str) -> None:
        self._parts.append(chunk)

    async def finish(self) -> None:
        body = "".join(self._parts).strip()
        await _safe_send(self._context, body or _EMPTY_REPLY_TEXT)


async def _safe_send(context: Any, text: str) -> None:
    """Send an activity, logging (not raising) on delivery failure.

    Outbound delivery goes to the Bot Connector. A transient failure there must
    not surface as a 500 on the inbound webhook, or the connector retries the
    whole turn.
    """
    try:
        await context.send_activity(text)
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


def _clean_status(text: str) -> str:
    """Unwrap the ``\\n\\n*status*\\n\\n`` marker the middleware emits."""
    return text.strip().strip("*").strip()


def _load_history(state: Any) -> list[dict[str, str]]:
    try:
        history = state.get_value(_HISTORY_PATH)
    except Exception:  # pragma: no cover - defensive
        logger.warning("Could not read Activity conversation history", exc_info=True)
        return []
    if not isinstance(history, list):
        return []
    return [
        entry
        for entry in history
        if isinstance(entry, dict) and isinstance(entry.get("text"), str) and entry.get("role") in ("user", "assistant")
    ]


def _save_history(state: Any, history: list[dict[str, str]]) -> None:
    try:
        state.set_value(_HISTORY_PATH, history[-_MAX_HISTORY_MESSAGES:])
    except Exception:  # pragma: no cover - defensive
        logger.warning("Could not persist Activity conversation history", exc_info=True)
