"""Conversation correlation for nested hosted-agent telemetry."""

from __future__ import annotations

from typing import Any

from agent_framework import Agent


class FoundryConversationTelemetryAgent(Agent):
    """Resolve voice conversation IDs for Agent Framework telemetry only."""

    def bind_telemetry_conversation(
        self,
        agent_session_id: str,
        conversation_id: str,
    ) -> None:
        bindings = getattr(self, "_telemetry_conversation_bindings", None)
        if bindings is None:
            bindings = {}
            self._telemetry_conversation_bindings = bindings
        current = bindings.get(agent_session_id)
        count = current[1] + 1 if current and current[0] == conversation_id else 1
        bindings[agent_session_id] = (conversation_id, count)

    def unbind_telemetry_conversation(
        self,
        agent_session_id: str,
        conversation_id: str,
    ) -> None:
        bindings = getattr(self, "_telemetry_conversation_bindings", {})
        current = bindings.get(agent_session_id)
        if not current or current[0] != conversation_id:
            return
        if current[1] > 1:
            bindings[agent_session_id] = (conversation_id, current[1] - 1)
        else:
            bindings.pop(agent_session_id, None)

    def _get_otel_conversation_id(self, session: Any) -> str | None:
        conversation_id = super()._get_otel_conversation_id(session)
        if conversation_id:
            return conversation_id

        try:
            from azure.ai.agentserver.core import get_request_context

            agent_session_id = get_request_context().session_id
        except (AttributeError, ImportError, LookupError):
            return None

        binding = getattr(self, "_telemetry_conversation_bindings", {}).get(
            agent_session_id
        )
        return binding[0] if binding else None