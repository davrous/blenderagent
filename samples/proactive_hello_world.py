"""Hello-world: proactive (out-of-band) messages over the Activity protocol.

Why you would want this
-----------------------
A Teams / M365 Copilot turn is an HTTP request, and the platform in front of
your agent abandons that request after roughly 45 seconds — the user sees an
error even though your code is still happily working and will produce an answer
moments later. Anything genuinely slow (a render, a long tool chain, a batch
job) therefore has to:

    1. answer the request quickly with "I'm on it",
    2. keep working in the background,
    3. push the result later as a *proactive* message.

This file is a ~150-line, self-contained version of exactly that, extracted
from a production Blender agent. Nothing here depends on that agent.

Run it
------
    pip install azure-ai-agentserver-activity microsoft-agents-hosting-core
    python proactive_hello_world.py                  # serves :8088 (PORT env var)
    npx @microsoft/m365agentsplayground -e http://localhost:8088/api/messages

Then say:
    "hi"    -> instant reply, nothing fancy (the fast path)
    "slow"  -> immediate ack, then TWO proactive follow-ups a few seconds apart

The two follow-ups are the interesting part. A single "here's your result" at
the end leaves the user staring at nothing for a minute, so the pattern is:

    message 1 (in-request) : "this task requires time, I'll message you"
    message 2 (proactive)  : a cheap intermediate artefact — a preview image,
                             a partial result — so there is something to look
                             at while the slow step runs
    message 3 (proactive)  : the finished product

The hard-won details are in ProactiveSender below. Read those comments before
reaching for `adapter.continue_conversation()`, which looks like the obvious
API and does not work on this host.
"""

from __future__ import annotations

import asyncio
import logging

from azure.ai.agentserver.activity import ActivityAgentServerHost
from microsoft_agents.activity import Activity, ActivityTypes
from microsoft_agents.hosting.core import MemoryStorage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("proactive_hello_world")

PREVIEW_IMAGE = "https://raw.githubusercontent.com/microsoft/fluentui-emoji/main/assets/Hourglass%20not%20done/3D/hourglass_not_done_3d.png"
RESULT_IMAGE = "https://raw.githubusercontent.com/microsoft/fluentui-emoji/main/assets/Party%20popper/3D/party_popper_3d.png"


class ProactiveSender:
    """Everything needed to message a conversation *after* its turn has ended.

    Build it while you still have a live ``TurnContext``; use it whenever.

    Why not ``adapter.continue_conversation()``?
        That is the documented API and it is the first thing everyone tries.
        It routes through ``ChannelServiceAdapter.process_proactive()``, which
        unconditionally builds a ``UserTokenClient`` for the OAuth flow and —
        unlike ``process_activity()`` — never computes the anonymous-auth flag
        nor passes the token scopes. On any host without user-token credentials
        it dies before it ever reaches the send::

            msal.managed_identity.ManagedIdentityError: You shall specify one
            of the three parameters: client_id, resource_id, object_id

        Posting an activity needs no user token at all, so this class skips
        that machinery and rebuilds the *inbound* turn's connector client
        instead — same identity, audience, scopes and anonymous flag. That is
        what makes it work both locally (anonymous, against the Agents
        Playground) and in the cloud (managed identity).

    Why not just keep using the original ``TurnContext``?
        ``process_activity()`` closes the request's connector client the moment
        your handler returns, so a later send fails with
        ``RuntimeError: Session is closed``. A fresh client is required, which
        is a bonus: its credentials are acquired at send time rather than
        reused past their lifetime.
    """

    def __init__(self, context) -> None:
        adapter = context.adapter
        # The factory and the outgoing audience the inbound turn used. Both
        # live in turn_state under keys the adapter exposes as constants.
        self._factory = context.turn_state[adapter.CHANNEL_SERVICE_FACTORY_KEY]
        self._audience = context.turn_state[adapter.OAUTH_SCOPE_KEY]
        self._identity = context.identity
        # Where to send: channel, service url, conversation, and who is who.
        self._reference = context.activity.get_conversation_reference()
        # Only used to detect agentic requests when minting the token.
        self._context = context

    async def send(self, text: str) -> None:
        anonymous = (
            not self._identity.is_authenticated
            and self._identity.authentication_type == "Anonymous"
        )
        client = await self._factory.create_connector_client(
            self._context,
            self._identity,
            self._reference.service_url,
            self._audience,
            self._identity.get_token_scope(),
            anonymous,
        )
        try:
            activity = Activity(type=ActivityTypes.message, text=text)
            # Fills in channel_id, service_url, conversation, from/recipient —
            # and reply_to_id, when the reference carries the original message.
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
            logger.info("Proactive message delivered (%d chars)", len(text))
        finally:
            await client.close()


async def _slow_work(sender: ProactiveSender) -> None:
    """Runs after the HTTP request has been answered. Never let it raise.

    A background task that dies silently is the worst outcome here: the user
    was promised a follow-up that never arrives, and nothing is logged.
    """
    try:
        await asyncio.sleep(5)  # e.g. build the scene, then screenshot it
        await sender.send(
            f"🖼️ Here's a preview while I finish:\n\n![preview]({PREVIEW_IMAGE})\n\n"
            "That's the state so far — the final version is rendering now and "
            "will land in this chat as soon as it's ready."
        )

        await asyncio.sleep(10)  # e.g. the actual render
        await sender.send(f"✅ All done!\n\n![result]({RESULT_IMAGE})")
    except Exception:
        logger.exception("Slow work failed — telling the user")
        try:
            await sender.send("😞 Sorry, that job failed. Please try again.")
        except Exception:
            logger.exception("Could not even report the failure")


def register(app) -> None:
    @app.activity("message")
    async def on_message(context, _state) -> None:  # pyright: ignore[reportUnusedFunction]
        text = (context.activity.text or "").strip().lower()

        if "slow" not in text:
            # Fast path: answer in-request, exactly like any normal bot.
            await context.send_activity(
                "👋 Hello! Say **slow** and I'll show you the proactive flow."
            )
            return

        # Capture the conversation BEFORE the turn ends — this is the only
        # moment the identity, factory and reference are all available.
        sender = ProactiveSender(context)

        await context.send_activity(
            "⏳ This task requires time — I'll keep working on it in the "
            "background and message you here as soon as it's ready."
        )

        # Fire and forget: the handler returns immediately, the request
        # completes well inside the platform's timeout, and this task keeps
        # running. Keep a reference so it isn't garbage-collected mid-flight,
        # and remember the process must outlive the request for this to work
        # (serverless hosts that freeze on response will not deliver).
        task = asyncio.create_task(_slow_work(sender))
        _background.add(task)
        task.add_done_callback(_background.discard)


_background: set[asyncio.Task] = set()


def main() -> None:
    host = ActivityAgentServerHost(storage=MemoryStorage())
    register(host.agent_app)
    logger.info("POST /api/messages ready — point the Agents Playground at it.")
    host.run()  # PORT env var, default 8088


if __name__ == "__main__":
    main()
