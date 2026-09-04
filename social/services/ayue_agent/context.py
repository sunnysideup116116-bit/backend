"""Privacy-safe, per-turn context assembly for public Ayue."""

from __future__ import annotations

import re
import time
from typing import Any

from bson.objectid import ObjectId

from database import matches_coll, profiles_coll
from services.conversation_compaction_service import load_validated_conversation_continuity
from services.profile_projection import safe_recent_context
from services.profile_location import safe_profile_location
from services.match_state_service import load_match_state
from services.proposal_namespace import (
    EVENT_INVITATION_NAMESPACE,
    RELATIONSHIP_MATCH_NAMESPACE,
    live_proposal_query,
    namespace_clause,
)

from .contracts import AgentTurnContext, PublicAgentTurnContext, TurnClockV1
from .capabilities import CAPABILITY_MANIFEST_VERSION
from .public_relationship_projection import mentioned_contact_refs, validated_mentioned_contact_ids
from .time_context import build_turn_clock


INTERNAL_ID_RE = re.compile(r"(?:@?seed_user_[\w-]+|@?demo_user|@?user[_-]?\d+)", re.IGNORECASE)
MAX_HISTORY_MESSAGES = 20
MAX_HISTORY_CHARS = 8000
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


def _message_is_after_watermark(item: dict[str, Any], watermark: dict[str, Any] | None) -> bool:
    if not watermark:
        return True
    try:
        timestamp = float(item.get("timestamp", 0) or 0)
        covered_timestamp = float(watermark.get("covered_through_timestamp", 0) or 0)
    except (TypeError, ValueError):
        return False
    if timestamp != covered_timestamp:
        return timestamp > covered_timestamp
    message_id = str(item.get("_id") or item.get("message_id") or "")
    covered_id = str(watermark.get("covered_through_message_id") or "")
    try:
        return ObjectId(message_id) > ObjectId(covered_id)
    except Exception:
        return False


def _history(
    ctx: AgentTurnContext, *, watermark: dict[str, Any] | None = None,
) -> tuple[list[dict[str, str]], str]:
    history: list[dict[str, str]] = []
    previous_assistant = ""
    used = 0
    for item in reversed((ctx.recent_history or [])[-MAX_HISTORY_MESSAGES:]):
        if not _message_is_after_watermark(item, watermark):
            continue
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
        live_proposal_query(ctx.user_id, RELATIONSHIP_MATCH_NAMESPACE),
        sort=[("created_at", -1)],
    )
    latest_declined = matches_coll.find_one(
        {"$and": [
            {"status": "declined", "$or": [{"from_user": ctx.user_id}, {"to_user": ctx.user_id}]},
            namespace_clause(RELATIONSHIP_MATCH_NAMESPACE),
        ]},
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
    continuity = load_validated_conversation_continuity(ctx.user_id, ctx.room_id)
    watermark = None
    if continuity:
        watermark = {
            "covered_through_message_id": continuity["covered_through_message_id"],
            "covered_through_timestamp": continuity["covered_through_timestamp"],
        }
    history, _ = _history(ctx, watermark=watermark)
    match_state = load_match_state(ctx.user_id)
    active = match_state["active_proposal"]
    active_prompt = None
    active_authority = None
    if active and not match_state["ambiguous"]:
        other = _other_id(active, ctx.user_id)
        status = active.get("status")
        allowed_actions = match_state["allowed_actions"]
        active_prompt = {
            "status": status,
            "counterparty": _public_label(other),
            "user_can_decide": bool(allowed_actions),
            "allowed_actions": allowed_actions,
            "proposal_revision": int(active.get("proposal_revision", 0)),
            "stage": match_state["stage"],
            "created_at": active.get("created_at"),
            "source": "existing_proposal",
        }
        active_authority = {
            "match_id": str(active.get("_id") or ""),
            "expected_status": str(status or ""),
            "proposal_revision": int(active.get("proposal_revision", 0) or 0),
            "proposal_namespace": RELATIONSHIP_MATCH_NAMESPACE,
        }
    event_active = matches_coll.find_one(
        live_proposal_query(ctx.user_id, EVENT_INVITATION_NAMESPACE),
        sort=[("created_at", -1)],
    )
    event_active_count = matches_coll.count_documents(
        live_proposal_query(ctx.user_id, EVENT_INVITATION_NAMESPACE)
    )
    event_prompt = None
    if event_active and event_active_count == 1:
        event_status = str(event_active.get("status") or "")
        event_can_decide = (
            event_status == "draft" and event_active.get("from_user") == ctx.user_id
        ) or (
            event_status == "pending" and event_active.get("to_user") == ctx.user_id
        )
        event_prompt = {
            "status": event_status,
            "event_title": _clean_text(
                (event_active.get("event_snapshot") or {}).get("title"), 120,
            ),
            "user_can_decide": event_can_decide,
            "proposal_revision": int(event_active.get("proposal_revision", 0) or 0),
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
    from .v3.relationship_references import (
        get_reference as get_relationship_reference,
        public_projection as relationship_reference_projection,
    )
    from .v3.place_references import (
        get_candidate_set as get_place_candidate_set,
        public_projection as place_candidate_projection,
    )
    calendar_draft = calendar_draft_projection(get_calendar_draft(ctx.user_id))
    calendar_recent_reference = calendar_reference_projection(get_calendar_reference(ctx.user_id))
    calendar_recent_mutation = recent_mutation_projection(get_recent_mutation(ctx.user_id))
    recent_contact_reference = relationship_reference_projection(
        get_relationship_reference(ctx.user_id)
    )
    recent_place_candidates = place_candidate_projection(
        get_place_candidate_set(ctx.user_id, ctx.room_id)
    )
    now = time.time()
    if recent_context_draft and now - float(recent_context_draft.get("created_at", 0) or 0) > RECENT_CONTEXT_DRAFT_TTL_SECONDS:
        # Context assembly is read-only, including expired auxiliary drafts.
        recent_context_draft = None
    memories = []
    for item in (profile.get("profile_memory_preview") or [])[:8]:
        label = item.get("label") if isinstance(item, dict) else item
        label = _clean_text(label, 80)
        if label:
            memories.append(label)
    mentioned_ids, validation_overflow = validated_mentioned_contact_ids(ctx.user_id, ctx.mentioned_ids)
    match_search = match_state["search"]
    turn = PublicAgentTurnContext(
        user_id=ctx.user_id, room_id=ctx.room_id, message=_clean_text(ctx.message, 1600),
        recent_messages=history,
        conversation_continuity=continuity["summary"] if continuity else None,
        recent_context=safe_recent_context(profile.get("current_context"), ""),
        user_location=safe_profile_location(profile).get("display_name", ""),
        relevant_memories=memories, active_proposal=active_prompt,
        active_event_invitation=event_prompt,
        match_search=match_search,
        latest_match_outcome=outcome, clock=turn_clock,
        calendar_draft=calendar_draft, calendar_recent_reference=calendar_recent_reference,
        calendar_recent_mutation=calendar_recent_mutation,
        recent_place_candidates=recent_place_candidates,
        recent_context_draft=recent_context_draft,
        recent_contact_reference=recent_contact_reference,
        mentioned_contacts=mentioned_contact_refs(ctx.user_id, mentioned_ids),
        mentioned_contact_overflow=bool(ctx.mention_overflow or validation_overflow),
        capability_manifest_version=CAPABILITY_MANIFEST_VERSION,
    )
    turn._active_proposal_authority = active_authority  # type: ignore[attr-defined]
    turn._match_state = match_state  # type: ignore[attr-defined]
    return turn
