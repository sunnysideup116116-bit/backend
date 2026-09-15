"""Privacy-safe, per-turn context assembly for public Ayue."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from bson.objectid import ObjectId

from database import matches_coll, profiles_coll
from services.conversation_compaction_service import load_validated_conversation_continuity
from services.conversation_message_ids import message_id_order_key
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
from .shared.interaction_history import historical_interaction_projection
from .web_tools import is_safe_public_url


INTERNAL_ID_RE = re.compile(r"(?:@?seed_user_[\w-]+|@?demo_user|@?user[_-]?\d+)", re.IGNORECASE)
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CHARS = 6000
RECENT_CONTEXT_DRAFT_TTL_SECONDS = 30 * 60


def _clean_text(value: Any, limit: int = 900) -> str:
    text = INTERNAL_ID_RE.sub("對方", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _clean_history_content(value: Any) -> str:
    """Keep visible paragraph/list structure while removing internal IDs."""
    text = INTERNAL_ID_RE.sub("對方", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def _history_sent_at(value: Any, timezone_name: str) -> str:
    """Render one persisted message timestamp in the turn's local timezone."""
    if value in (None, ""):
        return "unknown"
    try:
        if isinstance(value, datetime):
            instant = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        else:
            instant = datetime.fromtimestamp(float(value), tz=timezone.utc)
        return instant.astimezone(ZoneInfo(timezone_name)).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, KeyError):
        return "unknown"


def _visible_confirmation_history(item: dict[str, Any]) -> str:
    """Project only confirmation copy that the user could see in this message."""
    metadata = item.get("metadata")
    choice = metadata.get("choice_prompt") if isinstance(metadata, dict) else None
    if not isinstance(choice, dict):
        return ""
    display = choice.get("display")
    if not isinstance(display, dict):
        return ""
    state = str(choice.get("state") or "unknown")[:32]
    labels = {
        "pending": "待確認，尚未執行",
        "confirmed": "已確認",
        "cancelled": "已取消，尚未執行",
        "auto_cancelled": "因使用者繼續對話而取消，尚未執行",
        "expired": "已過期，尚未執行",
        "superseded": "已被新確認取代，不可再執行",
        "failed": "執行失敗",
    }
    lines = [f"[確認卡｜{labels.get(state, state)}]"]
    for key, label in (
        ("title", "操作"),
        ("summary", "內容"),
        ("consequence", "確認後"),
    ):
        value = _clean_history_content(display.get(key))[:600]
        if value:
            lines.append(f"{label}：{value}")
    return "\n".join(lines)[:900] if len(lines) > 1 else ""


def _visible_source_history(item: dict[str, Any]) -> list[dict[str, str]]:
    """Project sources saved with this exact visible assistant message."""
    metadata = item.get("metadata")
    raw_sources = metadata.get("sources") if isinstance(metadata, dict) else None
    if not isinstance(raw_sources, list):
        return []
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for source in raw_sources:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        if not is_safe_public_url(url) or url in seen:
            continue
        seen.add(url)
        output.append({
            "title": _clean_text(source.get("title") or "公開來源", 160),
            "url": url[:1000],
        })
        if len(output) >= 5:
            break
    return output


def _public_label(user_id: str | None) -> str:
    if not user_id:
        return "對方"
    profile = profiles_coll.find_one(
        {"user_id": user_id}, {"display_name": 1, "nickname": 1, "name": 1}
    ) or {}
    from services.public_nickname_service import contact_display_name
    return contact_display_name(user_id, profile) or "對方"


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
        return message_id_order_key(message_id) > message_id_order_key(covered_id)
    except Exception:
        return False


def _history(
    ctx: AgentTurnContext, *, watermark: dict[str, Any] | None = None,
    exclude_message_id: str | None = None,
    current_message: str = "",
    timezone_name: str = "Asia/Taipei",
) -> tuple[list[dict[str, Any]], str]:
    history: list[dict[str, Any]] = []
    previous_assistant = ""
    used = 0
    # Exclude the separately supplied current message before applying the
    # twelve-message budget; otherwise a persisted current row would silently
    # reduce usable history to eleven messages.
    source = list(ctx.recent_history or [])
    excluded_id = str(exclude_message_id or "").strip()
    has_excluded_id = bool(
        excluded_id
        and any(
            str(item.get("_id") or item.get("message_id") or "") == excluded_id
            for item in source
            if isinstance(item, dict)
        )
    )
    fallback_current = _clean_history_content(current_message)
    skipped_fallback_current = False
    for item in reversed(source):
        if len(history) >= MAX_HISTORY_MESSAGES:
            break
        if not _message_is_after_watermark(item, watermark):
            continue
        sender = item.get("sender_id") or item.get("role") or "assistant"
        role = "user" if sender == ctx.user_id or sender == "user" else "assistant"
        content = _clean_history_content(item.get("content") or item.get("message"))
        confirmation = _visible_confirmation_history(item)
        if confirmation:
            content = f"{content}\n\n{confirmation}".strip()
        if not content:
            continue
        message_id = str(item.get("_id") or item.get("message_id") or "")
        if has_excluded_id and message_id == excluded_id:
            continue
        if (
            not has_excluded_id
            and fallback_current
            and not skipped_fallback_current
            and role == "user"
            and content == fallback_current
        ):
            skipped_fallback_current = True
            continue
        if role == "assistant" and not previous_assistant:
            previous_assistant = content
        truncated = False
        if used + len(content) > MAX_HISTORY_CHARS:
            content = content[: max(0, MAX_HISTORY_CHARS - used)].rstrip()
            truncated = True
        if not content:
            break
        history_item = {
            "role": role,
            "content": content,
            "sent_at": _history_sent_at(item.get("timestamp"), timezone_name),
            "timezone": timezone_name,
            "truncated": truncated,
        }
        historical_interaction = historical_interaction_projection(item)
        if historical_interaction is not None:
            history_item["historical_interaction"] = historical_interaction
        if role == "assistant":
            sources = _visible_source_history(item)
            if sources:
                history_item["sources"] = sources
        history.append(history_item)
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
    """Assemble the only bounded state the public Pi runtime may see.

    Database identifiers, raw profile documents, other users' calendars and old
    unrelated matches deliberately remain outside this object.
    """
    turn_clock = clock or build_turn_clock(ctx.message)
    profile = ctx.user_profile or profiles_coll.find_one({"user_id": ctx.user_id}, {"_id": 0}) or {}
    continuity = load_validated_conversation_continuity(ctx.user_id, ctx.room_id)
    history, _ = _history(
        ctx,
        # The recent raw window remains available even when an older
        # compaction watermark exists; the summary cannot replace corrections.
        watermark=None,
        exclude_message_id=ctx.message_id,
        current_message=ctx.message,
        timezone_name=turn_clock.timezone,
    )
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
    from .shared.calendar_state import (
        get_recent_mutation,
        recent_mutation_projection,
    )
    from .shared.date_coordination_state import (
        date_coordination_summary,
    )
    calendar_recent_mutation = recent_mutation_projection(get_recent_mutation(ctx.user_id))
    date_summary, date_summary_authority = date_coordination_summary(ctx.user_id)
    now = time.time()
    if recent_context_draft and now - float(recent_context_draft.get("created_at", 0) or 0) > RECENT_CONTEXT_DRAFT_TTL_SECONDS:
        # Context assembly is read-only, including expired auxiliary drafts.
        recent_context_draft = None
    memories = [_clean_text(text, 80) for text in preference_wording(
        profile.get("profile_memory_preview"), owner_id=ctx.user_id,
    )]
    mentioned_ids, validation_overflow = validated_mentioned_contact_ids(ctx.user_id, ctx.mentioned_ids)
    owner_relationship_memories: list[dict[str, Any]] = []
    if (
        len(mentioned_ids) == 1
        and not validation_overflow
        and bool(getattr(ctx, "external_calendar_authorized", False))
    ):
        from services.relationship_memory_service import relationship_memory_context

        owner_relationship_memories = relationship_memory_context(ctx.user_id, mentioned_ids[0])
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
        calendar_recent_mutation=calendar_recent_mutation,
        recent_context_draft=recent_context_draft,
        date_coordination_summary=date_summary,
        mentioned_contacts=mentioned_contact_refs(ctx.user_id, mentioned_ids),
        mentioned_contact_overflow=bool(ctx.mention_overflow or validation_overflow),
        owner_relationship_memories=owner_relationship_memories,
        capability_manifest_version=CAPABILITY_MANIFEST_VERSION,
    )
    turn._active_proposal_authority = active_authority  # type: ignore[attr-defined]
    turn._focused_match_authority = focused_authority  # type: ignore[attr-defined]
    turn._date_coordination_summary_authority = date_summary_authority  # type: ignore[attr-defined]
    turn._match_state = match_state  # type: ignore[attr-defined]
    return turn


def _request_location_label(ctx: AgentTurnContext) -> str:
    request_location = getattr(ctx, "device_location", None) or {}
    if not isinstance(request_location, dict):
        return ""
    return _clean_text(request_location.get("display_name"), 120)
