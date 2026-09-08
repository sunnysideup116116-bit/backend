"""Privacy-safe, per-turn context assembly for public Ayue."""

from __future__ import annotations

import re
import time
from typing import Any

from bson.objectid import ObjectId

from database import matches_coll, profiles_coll
from services.conversation_compaction_service import load_validated_conversation_continuity
from services.profile_projection import safe_recent_context
from services.owner_memory_projection import preference_wording
from services.profile_location import safe_profile_location
from services.match_state_service import load_match_state
from services.proposal_namespace import (
    EVENT_INVITATION_NAMESPACE,
    RELATIONSHIP_MATCH_NAMESPACE,
    live_proposal_query,
    namespace_clause,
    namespace_for_document,
)

from .contracts import AgentTurnContext, PublicAgentTurnContext, TurnClockV1
from .capabilities import CAPABILITY_MANIFEST_VERSION
from .public_relationship_projection import mentioned_contact_refs, validated_mentioned_contact_ids
from .time_context import build_turn_clock


INTERNAL_ID_RE = re.compile(r"(?:@?seed_user_[\w-]+|@?demo_user|@?user[_-]?\d+)", re.IGNORECASE)
MAX_HISTORY_MESSAGES = 32
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


def _focused_match_projection(
    ctx: AgentTurnContext,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve a Hub focus into a safe prompt view plus private CAS authority."""
    match_id = str(getattr(ctx, "focused_match_id", "") or "").strip()
    if not match_id:
        return None, None
    try:
        document = matches_coll.find_one({
            "_id": ObjectId(match_id),
            "$or": [{"from_user": ctx.user_id}, {"to_user": ctx.user_id}],
        })
    except Exception:
        document = None
    if not document:
        return {"status": "unavailable", "reason": "card_not_found"}, None

    namespace = namespace_for_document(document)
    status = str(document.get("status") or "")
    is_from = document.get("from_user") == ctx.user_id
    if status == "draft":
        stage = "waiting_user" if is_from else "waiting_other"
    elif status == "pending":
        stage = "waiting_other" if is_from else "incoming_decision"
    elif status == "accepted":
        stage = "completed"
    else:
        stage = status or "unavailable"
    can_decide = (
        (status == "draft" and is_from)
        or (status == "pending" and not is_from)
    )
    event_snapshot = document.get("event_snapshot") or {}
    projection: dict[str, Any] = {
        "status": status or "unavailable",
        "stage": stage,
        "proposal_namespace": namespace,
        "user_can_decide": can_decide,
        "proposal_revision": int(document.get("proposal_revision", 0) or 0),
        "counterparty": _public_label(_other_id(document, ctx.user_id)),
    }
    if namespace == EVENT_INVITATION_NAMESPACE:
        title = _clean_text(event_snapshot.get("title"), 120)
        if title:
            projection["event_title"] = title
    source_title = _clean_text(document.get("source_room_title"), 60)
    source_summary = _clean_text(document.get("source_summary"), 180)
    if source_title:
        projection["source_room_title"] = source_title
    if source_summary:
        projection["source_summary"] = source_summary
    authority = {
        "match_id": match_id,
        "expected_status": status,
        "proposal_revision": int(document.get("proposal_revision", 0) or 0),
        "proposal_namespace": namespace,
    }
    return projection, authority


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
    preferences = [_clean_text(text, 80) for text in preference_wording(
        profile.get("profile_memory_preview"), owner_id=ctx.user_id,
    )]
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
    source_char_count = sum(
        len(str(item.get("content") or item.get("message") or ""))
        for item in (ctx.recent_history or [])
    )
    history_budget_limited = bool(
        getattr(ctx, "history_truncated", False)
        or len(ctx.recent_history or []) > MAX_HISTORY_MESSAGES
        or source_char_count > MAX_HISTORY_CHARS
    )
    match_state = load_match_state(ctx.user_id)
    active = match_state["active_proposal"]
    active_prompt = None
    active_authority = None
    if active and not match_state["ambiguous"]:
        other = _other_id(active, ctx.user_id)
        status = active.get("status")
        active_prompt = {
            "status": status,
            "counterparty": _public_label(other),
            # Decisions are canonical Hub-card actions.  Keep the old fields
            # in the projection for client compatibility, but never expose
            # them as chat authority or invite the public agent to execute
            # them from a normal conversation.
            "user_can_decide": False,
            "allowed_actions": [],
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
        event_prompt = {
            "status": event_status,
            "event_title": _clean_text(
                (event_active.get("event_snapshot") or {}).get("title"), 120,
            ),
            "user_can_decide": False,
            "proposal_revision": int(event_active.get("proposal_revision", 0) or 0),
        }
    # Terminal match outcomes are intentionally not preloaded into every
    # conversational turn.  They used to make an unrelated "why" look like a
    # question about an old decline.  A planner asks the canonical status tool
    # when it semantically recognises a match-status question instead.
    outcome = None
    recent_context_draft = profile.get("recent_context_draft") or None
    # Calendar follow-up state is a bounded, server-owned projection.  It
    # contains no event ID/revision and is stored independently of profile data.
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
    from .v3.date_coordination_references import (
        authority_projection as date_coordination_authority_projection,
        date_coordination_summary,
        get_reference as get_date_coordination_reference,
        public_projection as date_coordination_reference_projection,
    )
    from .v3.relationship_recommendations import (
        get_snapshot as get_relationship_recommendation,
        public_projection as relationship_recommendation_projection,
    )
    from .v3.place_references import (
        get_candidate_set as get_place_candidate_set,
        public_projection as place_candidate_projection,
        recent_selected_projection as recent_place_reference_projection,
        place_reference_read_scope,
    )
    from .v3.place_followups import (
        PlaceFollowupPersistenceError,
        get_followup as get_place_followup,
        public_projection as place_followup_projection,
    )
    calendar_draft = calendar_draft_projection(get_calendar_draft(ctx.user_id))
    calendar_recent_reference = calendar_reference_projection(get_calendar_reference(ctx.user_id))
    calendar_recent_mutation = recent_mutation_projection(get_recent_mutation(ctx.user_id))
    recent_contact_reference = relationship_reference_projection(
        get_relationship_reference(ctx.user_id)
    )
    try:
        recent_action_record = get_date_coordination_reference(ctx.user_id, ctx.room_id)
    except Exception:
        # A convenience reference outage must not block ordinary chat or other
        # read-only domain flows; cancellation fails closed without it.
        recent_action_record = None
    recent_action_reference = date_coordination_reference_projection(recent_action_record)
    date_summary, date_summary_authority = date_coordination_summary(ctx.user_id)
    recent_recommendation = relationship_recommendation_projection(
        get_relationship_recommendation(ctx.user_id, ctx.room_id)
    )
    with place_reference_read_scope():
        recent_place_candidates = place_candidate_projection(
            get_place_candidate_set(ctx.user_id, ctx.room_id)
        )
        recent_place_reference = recent_place_reference_projection(ctx.user_id, ctx.room_id)
    try:
        recent_place_followup = place_followup_projection(
            get_place_followup(ctx.user_id, ctx.room_id)
        )
    except PlaceFollowupPersistenceError:
        # A missing auxiliary follow-up must not prevent ordinary chat or
        # Calendar reads from running; the Calendar runtime will fail closed
        # if it needs the unavailable store for a place continuation.
        recent_place_followup = None
    now = time.time()
    if recent_context_draft and now - float(recent_context_draft.get("created_at", 0) or 0) > RECENT_CONTEXT_DRAFT_TTL_SECONDS:
        # Context assembly is read-only, including expired auxiliary drafts.
        recent_context_draft = None
    memories = [_clean_text(text, 80) for text in preference_wording(
        profile.get("profile_memory_preview"), owner_id=ctx.user_id,
    )]
    mentioned_ids, validation_overflow = validated_mentioned_contact_ids(ctx.user_id, ctx.mentioned_ids)
    focused_match, focused_authority = _focused_match_projection(ctx)
    match_search = {
        **(match_state["search"] or {}),
        "active_proposal_count": len(match_state.get("all_live_proposals") or match_state.get("active_proposals") or []),
        "pending_action_count": sum(
            1 for item in (match_state.get("all_live_proposals") or match_state.get("active_proposals") or [])
            if (item.get("status") == "draft" and item.get("from_user") == ctx.user_id)
            or (item.get("status") == "pending" and item.get("to_user") == ctx.user_id)
        ),
        "waiting_other_count": sum(
            1 for item in (match_state.get("all_live_proposals") or match_state.get("active_proposals") or [])
            if item.get("status") == "pending" and item.get("from_user") == ctx.user_id
        ),
    }
    request_location_label = _request_location_label(ctx)
    turn = PublicAgentTurnContext(
        user_id=ctx.user_id, room_id=ctx.room_id, message=_clean_text(ctx.message, 1600),
        recent_messages=history,
        history_projection_status=(
            "recent_only_budget_limited" if history_budget_limited else "complete"
        ),
        conversation_continuity=continuity["summary"] if continuity else None,
        recent_context=safe_recent_context(profile.get("current_context"), ""),
        user_location=(
            request_location_label
            or safe_profile_location(profile).get("display_name", "")
        ),
        relevant_memories=memories, active_proposal=active_prompt,
        active_event_invitation=event_prompt,
        focused_match=focused_match,
        match_search=match_search,
        latest_match_outcome=outcome, clock=turn_clock,
        calendar_draft=calendar_draft, calendar_recent_reference=calendar_recent_reference,
        calendar_recent_mutation=calendar_recent_mutation,
        recent_place_candidates=recent_place_candidates,
        recent_place_reference=recent_place_reference,
        place_followup=recent_place_followup,
        recent_context_draft=recent_context_draft,
        recent_contact_reference=recent_contact_reference,
        recent_action_reference=recent_action_reference,
        date_coordination_summary=date_summary,
        recent_recommendation=recent_recommendation,
        mentioned_contacts=mentioned_contact_refs(ctx.user_id, mentioned_ids),
        mentioned_contact_overflow=bool(ctx.mention_overflow or validation_overflow),
        capability_manifest_version=CAPABILITY_MANIFEST_VERSION,
    )
    turn._active_proposal_authority = active_authority  # type: ignore[attr-defined]
    turn._focused_match_authority = focused_authority  # type: ignore[attr-defined]
    turn._recent_action_reference_authority = date_coordination_authority_projection(  # type: ignore[attr-defined]
        recent_action_record
    )
    turn._date_coordination_summary_authority = date_summary_authority  # type: ignore[attr-defined]
    turn._match_state = match_state  # type: ignore[attr-defined]
    return turn


def _request_location_label(ctx: AgentTurnContext) -> str:
    request_location = getattr(ctx, "device_location", None) or {}
    if not isinstance(request_location, dict):
        return ""
    return _clean_text(request_location.get("display_name"), 120)
