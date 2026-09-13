"""Deterministic context slicer: cuts privacy-safe slices for each sub-agent."""

from __future__ import annotations

from typing import Any

from services.ayue_agent.contracts import PublicAgentTurnContext
from services.ayue_agent.capabilities import product_knowledge_catalog
from .contracts import AgentContextSlice


def _prior_messages(turn_ctx: PublicAgentTurnContext) -> list[dict[str, Any]]:
    """Return bounded prior messages without duplicating the current request."""
    messages = [
        {
            "role": str(item.get("role") or ""),
            "content": str(item.get("content") or "").strip(),
            "sent_at": str(item.get("sent_at") or "unknown")[:40],
            "timezone": str(item.get("timezone") or turn_ctx.clock.timezone)[:64],
            "truncated": bool(item.get("truncated")),
        }
        for item in (turn_ctx.recent_messages or [])
        if isinstance(item, dict)
        and str(item.get("role") or "") in {"user", "assistant"}
        and str(item.get("content") or "").strip()
    ]
    if (
        messages
        and messages[-1]["role"] == "user"
        and messages[-1]["content"] == str(turn_ctx.message or "").strip()
    ):
        messages.pop()
    return messages


def slice_for_agent(
    agent_name: str,
    turn_ctx: PublicAgentTurnContext,
    *,
    prior_observations: list[dict[str, Any]],
) -> AgentContextSlice:
    """Return a privacy-safe context slice for the named sub-agent.

    The Scheduler calls this before invoking a sub-agent. Each slice contains
    only the fields that agent is allowed to see, per the V3 spec §8.
    """
    clock_dump = turn_ctx.clock.model_dump()
    recent_messages = _prior_messages(turn_ctx)

    if agent_name == "calendar":
        return AgentContextSlice(agent="calendar", payload={
            "message": turn_ctx.message,
            "clock": clock_dump,
            # calendar agent may need recent_context to understand scheduling context
            "recent_context": turn_ctx.recent_context,
            "calendar_recent_mutation": getattr(turn_ctx, "calendar_recent_mutation", None),
            "cancelled_confirmation": getattr(
                turn_ctx, "_cancelled_confirmation_context", None,
            ),
            "prior_observations": prior_observations,
        })

    if agent_name == "places":
        return AgentContextSlice(agent="places", payload={
            "message": turn_ctx.message,
            "user_location": turn_ctx.user_location,
            "clock": clock_dump,
            "prior_observations": prior_observations,
        })

    if agent_name == "web":
        return AgentContextSlice(agent="web", payload={
            "message": turn_ctx.message,
            # Location is only a coarse saved city/district projection.  The
            # Web Agent may use it for explicitly local public research, but
            # it never receives a precise address or live position.
            "user_location": str(turn_ctx.user_location or "")[:80],
            "clock": clock_dump,
            "prior_observations": prior_observations[:4],
        })

    if agent_name == "match":
        active_proposal = turn_ctx.active_proposal or {}
        active_event = turn_ctx.active_event_invitation or {}
        return AgentContextSlice(agent="match", payload={
            "message": turn_ctx.message,
            "recent_messages": recent_messages,
            "conversation_continuity": (
                turn_ctx.conversation_continuity.model_dump(mode="json")
                if turn_ctx.conversation_continuity else None
            ),
            "active_proposal": {
                key: active_proposal[key]
                for key in ("status", "stage", "counterparty", "user_can_decide", "allowed_actions", "created_at", "source")
                if active_proposal.get(key) not in (None, "")
            } or None,
            "match_search": turn_ctx.match_search,
            "active_event_invitation": {
                key: active_event[key]
                for key in ("status", "event_title", "counterparty", "user_can_decide")
                if active_event.get(key) not in (None, "")
            } or None,
            "focused_match": turn_ctx.focused_match,
            "latest_match_outcome": turn_ctx.latest_match_outcome,
            "clock": clock_dump,
            "prior_observations": prior_observations,
        })

    if agent_name == "relationship":
        return AgentContextSlice(agent="relationship", payload={
            "message": turn_ctx.message,
            "recent_context": turn_ctx.recent_context,
            "relevant_memories": list(turn_ctx.relevant_memories or []),
            "mentioned_contacts": turn_ctx.mentioned_contacts,
            "mentioned_contact_overflow": turn_ctx.mentioned_contact_overflow,
            "owner_private_relationship_memories": list(
                getattr(turn_ctx, "owner_relationship_memories", []) or []
            ),
            "date_coordination_summary": getattr(turn_ctx, "date_coordination_summary", None),
            "clock": clock_dump,
            "prior_observations": prior_observations,
        })

    if agent_name == "profile":
        return AgentContextSlice(agent="profile", payload={
            "message": turn_ctx.message,
            "recent_messages": recent_messages,
            "recent_context": turn_ctx.recent_context,
            "relevant_memories": turn_ctx.relevant_memories,
            "clock": clock_dump,
            "prior_observations": prior_observations,
        })

    if agent_name == "product_info":
        # ProductInfo only receives the user proposition and bounded prior
        # observations declared by the DAG.  It must not see owner profile,
        # calendar contents, relationship-private context, or raw documents.
        return AgentContextSlice(agent="product_info", payload={
            "message": str(turn_ctx.message or "")[:1200],
            "recent_messages": recent_messages[-4:],
            "product_knowledge_catalog": product_knowledge_catalog(),
        })

    if agent_name == "synthesizer":
        return AgentContextSlice(agent="synthesizer", payload={
            "message": turn_ctx.message,
            "recent_messages": recent_messages,
            "conversation_continuity": (
                turn_ctx.conversation_continuity.model_dump(mode="json")
                if turn_ctx.conversation_continuity else None
            ),
            "recent_context": turn_ctx.recent_context,
            "user_preferences": list(turn_ctx.relevant_memories or []),
            "user_location": turn_ctx.user_location,
            "clock": clock_dump,
            "observations": prior_observations,
            "date_coordination_summary": getattr(turn_ctx, "date_coordination_summary", None),
        })

    raise ValueError(f"unknown agent: {agent_name}")
