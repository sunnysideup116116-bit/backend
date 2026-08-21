"""Deterministic context slicer: cuts privacy-safe slices for each sub-agent."""

from __future__ import annotations

from typing import Any

from services.ayue_agent.contracts import PublicAgentTurnContext
from services.ayue_agent.capabilities import product_knowledge_catalog
from .contracts import AgentContextSlice


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

    if agent_name == "calendar":
        return AgentContextSlice(agent="calendar", payload={
            "message": turn_ctx.message,
            "recent_messages": turn_ctx.recent_messages,
            "clock": clock_dump,
            # calendar agent may need recent_context to understand scheduling context
            "recent_context": turn_ctx.recent_context,
            "calendar_draft": getattr(turn_ctx, "calendar_draft", None),
            "calendar_recent_reference": getattr(turn_ctx, "calendar_recent_reference", None),
            "calendar_recent_mutation": getattr(turn_ctx, "calendar_recent_mutation", None),
            "prior_observations": prior_observations,
        })

    if agent_name == "places":
        return AgentContextSlice(agent="places", payload={
            "message": turn_ctx.message,
            "recent_messages": turn_ctx.recent_messages,
            "user_location": turn_ctx.user_location,
            "clock": clock_dump,
            "prior_observations": prior_observations,
        })

    if agent_name == "web":
        recent_messages: list[Any] = []
        recent_chars = 0
        for message in reversed(list(turn_ctx.recent_messages or [])):
            text = str(message or "")
            if not text:
                continue
            remaining = max(0, 2000 - recent_chars)
            if not remaining:
                break
            clipped = text[:remaining]
            recent_messages.append(clipped)
            recent_chars += len(clipped)
            if len(recent_messages) >= 4:
                break
        recent_messages.reverse()
        return AgentContextSlice(agent="web", payload={
            "message": turn_ctx.message,
            "recent_messages": recent_messages,
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
            "recent_messages": turn_ctx.recent_messages,
            "conversation_continuity": (
                turn_ctx.conversation_continuity.model_dump(mode="json")
                if turn_ctx.conversation_continuity else None
            ),
            "active_proposal": {
                key: active_proposal[key]
                for key in ("status", "counterparty", "user_can_decide")
                if active_proposal.get(key) not in (None, "")
            } or None,
            "active_event_invitation": {
                key: active_event[key]
                for key in ("status", "event_title", "counterparty", "user_can_decide")
                if active_event.get(key) not in (None, "")
            } or None,
            "latest_match_outcome": turn_ctx.latest_match_outcome,
            "clock": clock_dump,
            "prior_observations": prior_observations,
        })

    if agent_name == "relationship":
        return AgentContextSlice(agent="relationship", payload={
            "message": turn_ctx.message,
            "recent_messages": turn_ctx.recent_messages,
            "mentioned_contacts": turn_ctx.mentioned_contacts,
            "mentioned_contact_overflow": turn_ctx.mentioned_contact_overflow,
            "recent_contact_reference": turn_ctx.recent_contact_reference,
            "clock": clock_dump,
            "prior_observations": prior_observations,
        })

    if agent_name == "profile":
        return AgentContextSlice(agent="profile", payload={
            "message": turn_ctx.message,
            "recent_messages": turn_ctx.recent_messages,
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
            "recent_messages": list(turn_ctx.recent_messages or [])[-4:],
            "product_knowledge_catalog": product_knowledge_catalog(),
        })

    if agent_name == "synthesizer":
        return AgentContextSlice(agent="synthesizer", payload={
            "message": turn_ctx.message,
            "recent_messages": turn_ctx.recent_messages,
            "conversation_continuity": (
                turn_ctx.conversation_continuity.model_dump(mode="json")
                if turn_ctx.conversation_continuity else None
            ),
            "recent_context": turn_ctx.recent_context,
            "user_preferences": list(turn_ctx.relevant_memories or []),
            "user_location": turn_ctx.user_location,
            "clock": clock_dump,
            "observations": prior_observations,
        })

    raise ValueError(f"unknown agent: {agent_name}")
