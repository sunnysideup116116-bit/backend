"""Privacy-safe, per-turn context assembly for public Ayue."""

from __future__ import annotations

import re
import time
from typing import Any

from database import matches_coll, profiles_coll
from services.profile_projection import safe_recent_context
from services.profile_location import safe_profile_location

from .contracts import AgentTurnContext, PublicAgentTurnContext, TurnClockV1
from .capabilities import CAPABILITY_MANIFEST_VERSION
from .public_relationship_projection import mentioned_contact_refs, validated_mentioned_contact_ids
from .time_context import build_turn_clock
from services.conversation_compaction_service import load_public_continuity


INTERNAL_ID_RE = re.compile(r"(?:@?seed_user_[\w-]+|@?demo_user|@?user[_-]?\d+)", re.IGNORECASE)
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CHARS = 6000
RECENT_CONTEXT_DRAFT_TTL_SECONDS = 30 * 60


def _clean_text(value: Any, limit: int = 900) -> str:
    text = INTERNAL_ID_RE.sub("對方", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _public_label(user_id: str | None) -> str:
    if not user_id:
        return "對方"
    profile = profiles_coll.find_one(
        {"user_id": user_id}, {"display_name": 1, "nickname": 1, "name": 1}
    ) or {}
    return _clean_text(profile.get("display_name") or profile.get("nickname") or profile.get("name") or "對方", 30) or "對方"


def _other_id(match: dict[str, Any], user_id: str) -> str | None:
    return match.get("to_user") if match.get("from_user") == user_id else match.get("from_user")


def _history(ctx: AgentTurnContext) -> tuple[list[dict[str, str]], str]:
    history: list[dict[str, str]] = []
    previous_assistant = ""
    used = 0
    for item in reversed((ctx.recent_history or [])[-MAX_HISTORY_MESSAGES:]):
        sender = item.get("sender_id") or item.get("role") or "assistant"
        role = "user" if sender == ctx.user_id or sender == "user" else "assistant"
        content = _clean_text(item.get("content") or item.get("message"), 900)
        if not content:
            continue
        if role == "assistant" and not previous_assistant:
            previous_assistant = content
        if used + len(content) > MAX_HISTORY_CHARS:
            content = content[: max(0, MAX_HISTORY_CHARS - used)]
        if not content:
            break
        history.append({"role": role, "content": content})
        used += len(content)
        if used >= MAX_HISTORY_CHARS:
            break
    history.reverse()
    return history, previous_assistant


def build_public_context(ctx: AgentTurnContext) -> dict[str, Any]:
    """Build prompt-safe state. Internal IDs stay server-side and are never returned."""
    profile = ctx.user_profile or profiles_coll.find_one({"user_id": ctx.user_id}, {"_id": 0}) or {}
    history, previous_assistant = _history(ctx)
    active = matches_coll.find_one(
        {"status": {"$in": ["draft", "pending"]}, "$or": [{"from_user": ctx.user_id}, {"to_user": ctx.user_id}]},
        sort=[("created_at", -1)],
    )
    latest_declined = matches_coll.find_one(
        {"status": "declined", "$or": [{"from_user": ctx.user_id}, {"to_user": ctx.user_id}]},
        sort=[("updated_at", -1), ("created_at", -1)],
    )
    active_prompt = None
    if active:
        active_prompt = {
            "status": active.get("status"),
            "counterparty": _public_label(_other_id(active, ctx.user_id)),
            "user_can_decide": (
                (active.get("status") == "draft" and active.get("from_user") == ctx.user_id)
                or (active.get("status") == "pending" and ctx.user_id in {active.get("from_user"), active.get("to_user")})
            ),
        }
    outcome_prompt = None
    if latest_declined:
        decision = latest_declined.get("last_decision") or {}
        outcome_prompt = {
            "counterparty": _public_label(_other_id(latest_declined, ctx.user_id)),
            "declined_by_other": bool(decision.get("actor") and decision.get("actor") != ctx.user_id),
            "reason_available": False,
        }
    preferences = []
    for item in (profile.get("profile_memory_preview") or [])[:8]:
        if isinstance(item, dict):
            label = _clean_text(item.get("label") or item.get("label_zh_tw"), 60)
        else:
            label = _clean_text(item, 60)
        if label:
            preferences.append(label)
    return {
        "recent_messages": history,
        "previous_assistant_message": previous_assistant,
        "current_context": safe_recent_context(profile.get("current_context"), ""),
        "relevant_preferences": preferences,
        "active_match": active_prompt,
        "latest_match_outcome": outcome_prompt,
    }


def build_public_agent_turn_context(ctx: AgentTurnContext, *, clock: TurnClockV1 | None = None) -> PublicAgentTurnContext:
    """Assemble the only bounded state the Public V3 runtime may see.

    Database identifiers, raw profile documents, other users' calendars and old
    unrelated matches deliberately remain outside this object.
    """
    turn_clock = clock or build_turn_clock(ctx.message)
    profile = ctx.user_profile or profiles_coll.find_one({"user_id": ctx.user_id}, {"_id": 0}) or {}
    history, _ = _history(ctx)
    active = matches_coll.find_one(
        {"status": {"$in": ["draft", "pending"]}, "$or": [{"from_user": ctx.user_id}, {"to_user": ctx.user_id}]},
        sort=[("created_at", -1)],
    )
    # Only one active proposal is actionable. If old data has duplicates, expose
    # none rather than letting a model choose an arbitrary one.
    active_count = matches_coll.count_documents(
        {"status": {"$in": ["draft", "pending"]}, "$or": [{"from_user": ctx.user_id}, {"to_user": ctx.user_id}]}
    )
    active_prompt = None
    if active and active_count == 1:
        other = _other_id(active, ctx.user_id)
        status = active.get("status")
        can_decide = (status == "draft" and active.get("from_user") == ctx.user_id) or (
            status == "pending" and active.get("to_user") == ctx.user_id
        )
        active_prompt = {
            "status": status,
            "counterparty": _public_label(other),
            "user_can_decide": can_decide,
            "proposal_revision": int(active.get("proposal_revision", 0)),
        }
    # Terminal match outcomes are intentionally not preloaded into every
    # conversational turn.  They used to make an unrelated "why" look like a
    # question about an old decline.  A planner asks the canonical status tool
    # when it semantically recognises a match-status question instead.
    outcome = None
    recent_context_draft = profile.get("recent_context_draft") or None
    # Calendar follow-up state is a bounded, server-owned projection.  It
    # contains no event ID/revision and expires independently of profile data.
    from .v3.calendar_drafts import get_draft as get_calendar_draft, public_projection as calendar_draft_projection
    from .v3.calendar_references import (
        get_reference as get_calendar_reference,
        get_recent_mutation,
        public_projection as calendar_reference_projection,
        recent_mutation_projection,
    )
    calendar_draft = calendar_draft_projection(get_calendar_draft(ctx.user_id))
    calendar_recent_reference = calendar_reference_projection(get_calendar_reference(ctx.user_id))
    calendar_recent_mutation = recent_mutation_projection(get_recent_mutation(ctx.user_id))
    now = time.time()
    if recent_context_draft and now - float(recent_context_draft.get("created_at", 0) or 0) > RECENT_CONTEXT_DRAFT_TTL_SECONDS:
        profiles_coll.update_one({"user_id": ctx.user_id}, {"$unset": {"recent_context_draft": ""}})
        recent_context_draft = None
    memories = []
    for item in (profile.get("profile_memory_preview") or [])[:8]:
        label = item.get("label") if isinstance(item, dict) else item
        label = _clean_text(label, 80)
        if label:
            memories.append(label)
    mentioned_ids, validation_overflow = validated_mentioned_contact_ids(ctx.user_id, ctx.mentioned_ids)
    continuity = load_public_continuity(ctx.user_id)
    return PublicAgentTurnContext(
        user_id=ctx.user_id, room_id=ctx.room_id, message=_clean_text(ctx.message, 1600),
        recent_messages=history, recent_context=safe_recent_context(profile.get("current_context"), ""),
        user_location=safe_profile_location(profile).get("display_name", ""),
        relevant_memories=memories, active_proposal=active_prompt,
        conversation_continuity=continuity,
        latest_match_outcome=outcome, clock=turn_clock,
        calendar_draft=calendar_draft, calendar_recent_reference=calendar_recent_reference,
        calendar_recent_mutation=calendar_recent_mutation,
        recent_context_draft=recent_context_draft,
        mentioned_contacts=mentioned_contact_refs(ctx.user_id, mentioned_ids),
        mentioned_contact_overflow=bool(ctx.mention_overflow or validation_overflow),
        capability_manifest_version=CAPABILITY_MANIFEST_VERSION,
    )
