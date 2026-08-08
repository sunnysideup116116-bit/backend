"""Deterministic context slicer: cuts privacy-safe slices for each sub-agent."""

from __future__ import annotations

from typing import Any

from services.ayue_agent.contracts import AgentTurnContextV2
from .contracts import AgentContextSlice


def slice_for_agent(
    agent_name: str,
    turn_ctx: AgentTurnContextV2,
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

    if agent_name == "match":
        return AgentContextSlice(agent="match", payload={
            "message": turn_ctx.message,
            "recent_messages": turn_ctx.recent_messages,
            "active_proposal": turn_ctx.active_proposal,
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

    if agent_name == "synthesizer":
        return AgentContextSlice(agent="synthesizer", payload={
            "message": turn_ctx.message,
            "recent_messages": turn_ctx.recent_messages,
            "recent_context": turn_ctx.recent_context,
            "user_location": turn_ctx.user_location,
            "clock": clock_dump,
            "observations": prior_observations,
        })

    raise ValueError(f"unknown agent: {agent_name}")
