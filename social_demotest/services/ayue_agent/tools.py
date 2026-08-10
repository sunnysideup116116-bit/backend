"""Narrow tool facades for the public-Ayue agent.

Tools deliberately expose only user-safe, task-specific data.  They reuse the
existing domain services instead of owning matching, calendar, or memory data.
"""

from __future__ import annotations

import re
from datetime import datetime, time as time_value, timedelta, timezone
from typing import Any

from database import matches_coll, profiles_coll
from services.calendar_service import (
    as_utc,
    calendar_access_enabled,
    find_owned_events,
    get_calendar_context,
    get_next_event,
    get_timezone,
)
from services.match_state_service import (
    get_counterparty_match_source,
    get_match_status_snapshot,
    verified_accepted_match_query,
)
from services.profile_projection import clean_profile_text, contains_internal_identifier, safe_recent_context
from services.profile_location import safe_profile_location

from .contracts import AgentTurnContext, ToolCall, ToolResult, TurnClockV1
from .time_context import clock_utc
from .tool_registry import ToolRisk, get_tool_spec, validate_executor_arguments
from .public_relationship_projection import (
    anonymize_counterparty_payload,
    accepted_contact_ids_by_display_name,
    accepted_contact_summaries,
    display_name as _display_name,
    mentioned_contact_summary,
    other_id as _other_id,
    public_text as _public_text,
    safe_match_reason as _safe_proposal_summary,
    safe_public_profile as _public_counterparty_profile,
    verified_common_ground as _verified_common_ground,
)
from .web_tools import extract_web, search_web
from .maps_client import (
    MapClientError, measure_distance, nearby_places, nominatim_search,
    resolve_place as resolve_osm_place,
)
from .google_places_client import (
    GooglePlacesError, google_place_cards_enabled, google_routes_enabled,
    measure_distance_matrix, resolve_place as resolve_google_place,
    search_nearby_places,
)


def _calendar_events(user_id: str, clock: TurnClockV1 | None = None, arguments: dict | None = None) -> ToolResult:
    if not calendar_access_enabled(user_id):
        return ToolResult(ok=False, error_code="calendar_access_denied", user_message="你目前沒有授權我讀取行事曆。")
    zone = get_timezone(clock.timezone) if clock else timezone(timedelta(hours=8), name="Asia/Taipei")
    arguments = arguments or {}
    range_label = "next_90_days"

    def _parse_date(value: str) -> datetime.date | None:
        try:
            return datetime.fromisoformat(value.strip()).date()
        except (TypeError, ValueError):
            return None

    def _local_interval(start_date, end_date):
        """Convert two inclusive local dates into UTC datetimes (end exclusive)."""
        local_start = datetime.combine(start_date, time_value.min, zone)
        local_end = datetime.combine(end_date + timedelta(days=1), time_value.min, zone)
        return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)

    def _month_end(start_of_month):
        return (start_of_month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    now_utc, end_utc = None, None

    # Primary path: explicit start_date / end_date range.
    start_value = (arguments.get("start_date") or "").strip()
    end_value = (arguments.get("end_date") or "").strip()
    if start_value or end_value:
        start_date = _parse_date(start_value) if start_value else None
        end_date = _parse_date(end_value) if end_value else None
        if (start_value and start_date is None) or (end_value and end_date is None):
            return ToolResult(ok=False, error_code="invalid_tool_arguments", user_message="這個日期格式我不太確定，可以再跟我說一次嗎？")
        # Only one bound given → treat as a single day.
        if start_date is None:
            start_date = end_date
        if end_date is None:
            end_date = start_date
        if start_date > end_date:
            return ToolResult(ok=False, error_code="invalid_tool_arguments", user_message="這個日期範圍我不太確定，可以再跟我說一次嗎？")
        if (end_date - start_date).days > 366:
            return ToolResult(ok=False, error_code="invalid_tool_arguments", user_message="這個日期範圍太長，我先幫你確認較近的行程好嗎？")
        now_utc, end_utc = _local_interval(start_date, end_date)
        range_label = start_date.isoformat() if start_date == end_date else f"{start_date.isoformat()}~{end_date.isoformat()}"

    # Legacy fallback: single date.
    if now_utc is None:
        date_value = (arguments.get("date") or "").strip()
        if date_value:
            target_date = _parse_date(date_value)
            if target_date is None:
                return ToolResult(ok=False, error_code="invalid_tool_arguments", user_message="這個日期格式我不太確定，可以再跟我說一次嗎？")
            now_utc, end_utc = _local_interval(target_date, target_date)
            range_label = target_date.isoformat()

    # Legacy fallback: range_label resolved through the turn clock.
    if now_utc is None:
        range_value = (arguments.get("range_label") or "").strip()
        if range_value and clock and clock.temporal_references:
            resolved = clock.temporal_references.get(range_value)
            if resolved:
                start_date = _parse_date(resolved)
                if start_date is not None:
                    if range_value in {"本週", "這週", "下週"}:
                        end_date = start_date + timedelta(days=6)
                        now_utc, end_utc = _local_interval(start_date, end_date)
                        range_label = f"{start_date.isoformat()}~{end_date.isoformat()}"
                    elif range_value in {"本月", "下個月"}:
                        end_date = _month_end(start_date)
                        now_utc, end_utc = _local_interval(start_date, end_date)
                        range_label = f"{start_date.isoformat()}~{end_date.isoformat()}"
                    else:
                        now_utc, end_utc = _local_interval(start_date, start_date)
                        range_label = start_date.isoformat()

    if now_utc is None:
        # "最近的行程"：從今天 00:00 當地時間起算，含今天已結束的行程
        local_now = (clock_utc(clock) if clock else datetime.now(timezone.utc)).astimezone(zone)
        local_start = datetime.combine(local_now.date(), time_value.min, zone)
        now_utc = local_start.astimezone(timezone.utc)
        end_utc = now_utc + timedelta(days=90)
        range_label = "next_90_days"
    events = get_calendar_context(user_id, None, now_utc, end_utc).get("viewer_events", [])
    safe_events = []
    active_events = []
    for event in events:
        if event.get("status") == "cancelled":
            continue
        active_events.append(event)
        zone = get_timezone(event.get("timezone") or "Asia/Taipei")
        start = datetime.fromisoformat(str(event["start_at"]).replace("Z", "+00:00")).astimezone(zone)
        end = datetime.fromisoformat(str(event["end_at"]).replace("Z", "+00:00")).astimezone(zone)
        safe_events.append({
            "date": start.date().isoformat(),
            "start_time": start.strftime("%H:%M"),
            "end_time": end.strftime("%H:%M"),
            "activity": event.get("activity") or event.get("title") or "行程",
            "status": event.get("status", "confirmed"),
        })
    private_data: dict[str, Any] = {}
    if len(safe_events) == 1 and len(active_events) == 1:
        private_data["calendar_event_reference"] = {
            "event": active_events[0],
            "safe_label": (
                f"{safe_events[0]['date'][5:].replace('-', '/')} "
                f"{safe_events[0]['start_time']}–{safe_events[0]['end_time']} "
                f"{safe_events[0]['activity']}"
            ),
        }
    return ToolResult(ok=True, data={"events": safe_events, "range": range_label}, private_data=private_data)


def _calendar_next_event(user_id: str, clock: TurnClockV1 | None = None) -> ToolResult:
    if not calendar_access_enabled(user_id):
        return ToolResult(ok=False, error_code="calendar_access_denied", user_message="你目前沒有授權我讀取行事曆。")
    now_utc = clock_utc(clock) if clock else datetime.now(timezone.utc)
    event = get_next_event(user_id, now_utc, now_utc + timedelta(days=90))
    if not event:
        return ToolResult(ok=True, data={"status": "not_found", "event": None})
    safe_event = _calendar_event_fields(event)
    safe_event["status"] = str(event.get("status") or "confirmed")
    return ToolResult(
        ok=True,
        data={"status": "found", "event": safe_event},
        private_data={"calendar_event_reference": {"event": event}},
    )


def _calendar_event_fields(event: dict) -> dict[str, str]:
    zone = get_timezone(event.get("timezone") or "Asia/Taipei")
    start_value = event["start_at"]
    end_value = event["end_at"]
    if isinstance(start_value, str):
        start_value = datetime.fromisoformat(start_value.replace("Z", "+00:00"))
    if isinstance(end_value, str):
        end_value = datetime.fromisoformat(end_value.replace("Z", "+00:00"))
    start = as_utc(start_value).astimezone(zone)
    end = as_utc(end_value).astimezone(zone)
    return {
        "activity": str(event.get("activity") or event.get("title") or "行程")[:120],
        "date": start.date().isoformat(),
        "start_time": start.strftime("%H:%M"),
        "end_time": end.strftime("%H:%M"),
        "location": str(event.get("location") or "")[:160],
        "notes": str(event.get("notes") or "")[:200],
        "event_kind": "shared_date" if event.get("source_type") == "date" else "personal",
    }


def _calendar_event_companion(event: dict, user_id: str) -> dict[str, object]:
    """Resolve a shared-event companion only through canonical acceptance.

    Calendar storage contains participant IDs for synchronization.  They are
    intentionally kept executor-side and are never included in the tool result.
    """
    if event.get("source_type") != "date":
        return {
            "event_kind": "personal", "companion_known": False,
            "companion_display_name": "對方", "companion_safe_summary": "",
        }
    other_id = next((item for item in (event.get("participants") or []) if item != user_id), None)
    if not isinstance(other_id, str) or not other_id:
        return {
            "event_kind": "shared_date", "companion_known": False,
            "companion_display_name": "對方", "companion_safe_summary": "",
        }
    match = matches_coll.find_one(
        verified_accepted_match_query(user_id, other_id),
        {
            "_id": 0, "from_user": 1, "to_user": 1, "reason_items": 1,
            "receiver_reason_items": 1, "directional_reason_v2": 1,
            "reason_version": 1, "friend_intro_v4": 1,
        },
    )
    if not match:
        return {
            "event_kind": "shared_date", "companion_known": False,
            "companion_display_name": "對方", "companion_safe_summary": "",
        }
    label = _display_name(other_id)
    return {
        "event_kind": "shared_date", "companion_known": label != "對方",
        "companion_display_name": label,
        "companion_safe_summary": _safe_proposal_summary(match, user_id),
    }


def _calendar_find_event(ctx: AgentTurnContext, arguments: dict[str, Any]) -> ToolResult:
    if not calendar_access_enabled(ctx.user_id):
        return ToolResult(ok=False, error_code="calendar_access_denied", user_message="你目前沒有授權我讀取行事曆。")
    event_hint = str(arguments.get("event_hint") or "").strip()
    date_hint = str(arguments.get("date_hint") or "").strip()
    companion_hint = str(arguments.get("companion_hint") or "").strip()
    try:
        limit = int(arguments.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 30))
    companion_ids = accepted_contact_ids_by_display_name(ctx.user_id, companion_hint) if companion_hint else []
    if companion_hint and not companion_ids:
        return _empty_calendar_find("not_found", "companion_not_found", query=event_hint)
    if len(companion_ids) > 1:
        # A public display name is not a unique authority boundary. Do not use
        # calendar contents to infer which same-named accepted contact the
        # owner meant; ask for a different identifying description instead.
        return _empty_calendar_find("ambiguous", "companion_ambiguous", query=event_hint)
    events: list[dict] = []
    lookup_ids: list[str | None] = companion_ids if companion_ids else [None]
    for companion_user_id in lookup_ids:
        for event in find_owned_events(
            ctx.user_id, event_hint, date_hint=date_hint,
            companion_user_id=companion_user_id, limit=limit,
        ):
            if event not in events:
                events.append(event)
    if not events:
        reason = "companion_ambiguous" if len(companion_ids) > 1 else "event_not_found"
        return _empty_calendar_find(
            "ambiguous" if len(companion_ids) > 1 else "not_found", reason, query=event_hint,
        )
    if len(events) > 1:
        return ToolResult(ok=True, data={
            "status": "ambiguous", "reason_code": "event_ambiguous",
            "activity": "", "date": "", "start_time": "", "end_time": "",
            "event_kind": "", "companion_known": False, "companion_display_name": "對方",
            "companion_safe_summary": "",
            "candidates": [_calendar_event_fields(event) for event in events[:limit]],
        })
    event = events[0]
    return ToolResult(ok=True, data={
        "status": "found", "reason_code": "", **_calendar_event_fields(event),
        **_calendar_event_companion(event, ctx.user_id), "candidates": [],
    }, private_data={"calendar_event_reference": {"event": event}})


def _calendar_verify_recent_mutation(ctx: AgentTurnContext) -> ToolResult:
    """Verify the latest short-lived Calendar write against canonical state."""
    from services.ayue_agent.v3.calendar_references import verify_recent_mutation

    return ToolResult(ok=True, data={
        "calendar_mutation_verification": verify_recent_mutation(ctx.user_id),
    })


def _empty_calendar_find(status: str, reason_code: str, query: str = "") -> ToolResult:
    return ToolResult(ok=True, data={
        "status": status, "reason_code": reason_code,
        "activity": "", "date": "", "start_time": "", "end_time": "",
        "event_kind": "", "companion_known": False, "companion_display_name": "對方",
        "companion_safe_summary": "", "candidates": [], "query": query,
    })


def _match_state(user_id: str) -> ToolResult:
    profile = profiles_coll.find_one({"user_id": user_id}, {"_id": 0, "active_match_proposal_id": 1, "match_search": 1}) or {}
    pending = list(matches_coll.find({
        "status": {"$in": ["draft", "pending"]},
        "$or": [{"from_user": user_id}, {"to_user": user_id}],
    }, {"_id": 1, "from_user": 1, "to_user": 1, "status": 1}))
    active = [{"status": item.get("status")} for item in pending]
    search = profile.get("match_search", {}) or {}
    return ToolResult(ok=True, data={
        "active": active,
        "search": {"status": search.get("status", "idle"), "source": search.get("source")},
    })


def _match_latest_outcome(user_id: str) -> ToolResult:
    item = matches_coll.find_one(
        {
            "$and": [
                {"$or": [{"from_user": user_id}, {"to_user": user_id}]},
                {
                    "$or": [
                        {"status": "declined"},
                        verified_accepted_match_query(user_id),
                    ]
                },
            ]
        },
        sort=[("updated_at", -1), ("created_at", -1)],
    )
    if not item:
        return ToolResult(ok=True, data={"found": False})
    decision = item.get("last_decision") or {}
    return ToolResult(ok=True, data={
        "found": True,
        "status": item.get("status"),
        "declined_by_other": bool(decision.get("actor") and decision.get("actor") != user_id),
        "reason_available": False,
    })


def _match_status(user_id: str) -> ToolResult:
    return ToolResult(ok=True, data=get_match_status_snapshot(user_id))


def _current_time(clock: TurnClockV1 | None) -> ToolResult:
    if not clock:
        return ToolResult(ok=False, error_code="turn_clock_missing", user_message="我現在無法確認目前時間。")
    return ToolResult(ok=True, data=clock.model_dump())


def _public_match_state(match: dict, user_id: str) -> str:
    status = str(match.get("status") or "idle")
    if status == "draft":
        return "waiting_user" if match.get("from_user") == user_id else "incoming_decision"
    if status == "pending":
        return "waiting_other" if match.get("from_user") == user_id else "incoming_decision"
    return status


def _counterparty_summary(ctx: AgentTurnContext) -> ToolResult:
    source = get_counterparty_match_source(ctx.user_id)
    if source.get("ambiguous"):
        return ToolResult(ok=True, data={
            "found": False, "match_state": "failed", "display_name": "對方",
            "safe_summary": "", "recent_context": "", "initial_interest": "",
            "personality_summary": "", "distinctive_tags": [],
            "verified_common_ground": [], "recommendation_tier": "", "chat_opened": False,
        })
    match = source.get("match")
    if not match:
        return ToolResult(ok=True, data={
            "found": False, "match_state": None, "display_name": "對方",
            "safe_summary": "", "recent_context": "", "initial_interest": "",
            "personality_summary": "", "distinctive_tags": [],
            "verified_common_ground": [], "recommendation_tier": "", "chat_opened": False,
        })
    other_id = _other_id(match, ctx.user_id)
    status = _public_match_state(match, ctx.user_id)
    public_profile = _public_counterparty_profile(other_id)
    common_ground = _verified_common_ground(match, ctx.user_id)
    tags = [
        _public_text(value, 30)
        for value in (match.get("distinctive_tags") or [])
        if _public_text(value, 30)
    ][:4]
    tier = str(match.get("recommendation_tier") or "")
    if tier not in {"grounded", "exploratory"}:
        tier = "grounded" if common_ground else "exploratory"
    resolved_name = _display_name(other_id)
    display_label = resolved_name if status == "accepted" else "對方"
    if status != "accepted":
        public_profile = anonymize_counterparty_payload(
            public_profile, other_id, counterparty_name=resolved_name,
        )
        common_ground = anonymize_counterparty_payload(
            common_ground, other_id, counterparty_name=resolved_name,
        )
        tags = anonymize_counterparty_payload(tags, other_id, counterparty_name=resolved_name)
    return ToolResult(ok=True, data={
        "found": True,
        "match_state": status,
        # Profiles can be evaluated anonymously while a proposal is pending;
        # identity becomes public only after canonical acceptance.
        "display_name": display_label,
        "safe_summary": (
            _safe_proposal_summary(match, ctx.user_id)
            if status == "accepted"
            else anonymize_counterparty_payload(
                _safe_proposal_summary(match, ctx.user_id), other_id,
                counterparty_name=resolved_name,
            )
        ),
        **public_profile,
        "distinctive_tags": tags,
        "verified_common_ground": common_ground,
        "recommendation_tier": tier,
        "chat_opened": status == "accepted",
    })


def _recent_context(ctx: AgentTurnContext) -> ToolResult:
    try:
        profile = profiles_coll.find_one(
            {"user_id": ctx.user_id}, {"_id": 0, "current_context": 1, "current_context_revision": 1},
        ) or {}
    except Exception:
        # The per-turn owner snapshot is privacy-safe but may be stale, so it is
        # used only when the canonical read itself is unavailable.
        profile = ctx.user_profile or {}
    current_context = safe_recent_context(profile.get("current_context"), "")
    try:
        revision = max(0, int(profile.get("current_context_revision", 0) or 0))
    except (TypeError, ValueError):
        revision = 0
    return ToolResult(ok=True, data={
        "current_context": current_context,
        "revision": revision,
        "exists": bool(current_context),
    })


def _relationship_evidence(ctx: AgentTurnContext, other_id: str | None) -> ToolResult:
    accepted = list(matches_coll.find(
        verified_accepted_match_query(ctx.user_id, other_id)
    ))
    choices = []
    for match in accepted:
        candidate = _other_id(match, ctx.user_id)
        if other_id and candidate != other_id:
            continue
        summary = (match.get("relationship_memory") or {}).get("shared_summary", "")
        choices.append({
            "counterparty": _display_name(candidate),
            "summary": summary,
            "shared_message_count": int(match.get("shared_message_count", 0)),
        })
    if other_id and not choices:
        return ToolResult(ok=False, error_code="relationship_not_accepted", user_message="這位目前不是已接受配對，我不能把他的資料當成已確認資訊。")
    return ToolResult(ok=True, data={"relationships": choices})


def _mentioned_contact_summary(ctx: AgentTurnContext, other_ids: list[str]) -> ToolResult:
    return ToolResult(ok=True, data={"contacts": mentioned_contact_summary(ctx.user_id, other_ids)})


def _accepted_contact_list(ctx: AgentTurnContext) -> ToolResult:
    contacts, truncated = accepted_contact_summaries(ctx.user_id)
    total_count: int | None = len(contacts) if not truncated else None
    if truncated:
        try:
            total_count = int(matches_coll.count_documents(verified_accepted_match_query(ctx.user_id)))
        except Exception:
            # A bounded/truncated projection still remains useful when the
            # backing store cannot provide an exact count (for example in the
            # in-memory test store).  Do not turn that into a DB/network error.
            total_count = None
    return ToolResult(ok=True, data={
        "contacts": contacts,
        "truncated": truncated,
        "total_count": total_count,
    })


def _memory_profile(ctx: AgentTurnContext) -> ToolResult:
    profile = ctx.user_profile or profiles_coll.find_one({"user_id": ctx.user_id}, {"_id": 0}) or {}
    return ToolResult(ok=True, data={
        "summary": profile.get("profile_memory_summary", ""),
        "current_context": safe_recent_context(profile.get("current_context"), ""),
        "preferences": profile.get("profile_memory_preview", [])[:8],
    })


def _owner_profile_text(value: Any, limit: int) -> str:
    text = clean_profile_text(value, limit)
    return "" if contains_internal_identifier(text) else text


def _owner_profile_list(value: Any, *, limit: int = 3, item_limit: int = 32) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = _owner_profile_text(item, item_limit)
        if text and text not in items:
            items.append(text)
        if len(items) >= limit:
            break
    return items


def _owner_memory_preferences(value: Any, *, limit: int = 8) -> list[str]:
    """Render typed memory items without exposing graph/storage metadata."""
    if not isinstance(value, list):
        return []
    stance_prefix = {"like": "喜歡", "dislike": "不喜歡", "require": "需要", "avoid": "避免"}
    preferences: list[str] = []
    for item in value:
        if isinstance(item, dict):
            label = _owner_profile_text(item.get("label") or item.get("label_zh_tw"), 40)
            prefix = stance_prefix.get(str(item.get("stance") or "").strip(), "")
            text = f"{prefix}{label}" if label and prefix and not label.startswith(prefix) else label
        else:
            text = _owner_profile_text(item, 48)
        if text and text not in preferences:
            preferences.append(text)
        if len(preferences) >= limit:
            break
    return preferences


def _self_profile(ctx: AgentTurnContext) -> ToolResult:
    """Project only completed owner profile fields into a bounded tool result."""
    profile = ctx.user_profile or profiles_coll.find_one({"user_id": ctx.user_id}, {"_id": 0}) or {}
    big_five = profile.get("big_five") if isinstance(profile.get("big_five"), dict) else {}
    deep = profile.get("deep_profile") if isinstance(profile.get("deep_profile"), dict) else {}
    scores = {
        "openness": big_five.get("O"),
        "conscientiousness": big_five.get("C"),
        "extraversion": big_five.get("E"),
        "agreeableness": big_five.get("A"),
        "neuroticism": big_five.get("N"),
    }
    normalized_scores = {
        name: float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        for name, value in scores.items()
    }
    data = {
        "display_name": _owner_profile_text(profile.get("display_name") or profile.get("nickname") or profile.get("name"), 30),
        "initial_interest": _owner_profile_text(profile.get("initial_interest"), 120),
        "personality_summary": _owner_profile_text(big_five.get("summary"), 140),
        **normalized_scores,
        "values": _owner_profile_list(deep.get("values")),
        "life_goals": _owner_profile_list(deep.get("life_goals")),
        "relationship_needs": _owner_profile_list(deep.get("relationship_needs")),
        "stress_coping": _owner_profile_text(deep.get("stress_coping"), 100),
        "ideal_future": _owner_profile_text(deep.get("ideal_future"), 120),
        "deep_profile_summary": _owner_profile_text(deep.get("summary"), 140),
        "recent_context": safe_recent_context(profile.get("current_context"), ""),
        "location": safe_profile_location(profile).get("display_name", ""),
        "preferences": _owner_memory_preferences(profile.get("profile_memory_preview"), limit=8),
        "missing_sections": [],
    }
    if not data["initial_interest"]:
        data["missing_sections"].append("興趣與認識方向")
    if not data["personality_summary"] and not any(value is not None for value in normalized_scores.values()):
        data["missing_sections"].append("基礎性格")
    if not any((data["values"], data["life_goals"], data["relationship_needs"], data["stress_coping"], data["ideal_future"], data["deep_profile_summary"])):
        data["missing_sections"].append("深層資料")
    if not data["recent_context"]:
        data["missing_sections"].append("近期情境")
    return ToolResult(ok=True, data=data)


def _web_search(ctx: AgentTurnContext, arguments: dict[str, Any]) -> ToolResult:
    location = ""
    if arguments.get("use_saved_location"):
        location = safe_profile_location(ctx.user_profile).get("display_name", "")
    data, error_code = search_web(
        str(arguments.get("query") or ""),
        recency=str(arguments.get("recency") or "none"),
        location=location,
    )
    if error_code:
        return ToolResult(ok=False, error_code=error_code, user_message="我現在暫時查不到公開資訊。")
    return ToolResult(ok=True, data=data or {"results": []})


def _web_extract(arguments: dict[str, Any]) -> ToolResult:
    data, error_code = extract_web(
        [str(item) for item in (arguments.get("urls") or [])],
        query=str(arguments.get("query") or ""),
    )
    if error_code:
        return ToolResult(ok=False, error_code=error_code, user_message="我現在暫時打不開這個公開來源。")
    return ToolResult(ok=True, data=data or {"pages": []})


def _saved_location(ctx: AgentTurnContext) -> str:
    return str(safe_profile_location(ctx.user_profile).get("display_name") or "").strip()


def _canonical_place_anchor(ctx: AgentTurnContext, anchor: str) -> str:
    """Disambiguate a short district against the owner's saved city.

    This is location normalization, not intent routing: a bare district that
    exactly matches the saved profile district is expanded before geocoding so
    names shared by multiple Taiwanese cities do not resolve elsewhere.
    """
    value = re.sub(r"\s+", "", str(anchor or "")).strip()
    location = safe_profile_location(ctx.user_profile)
    saved = str(location.get("display_name") or "").strip()
    city = str(location.get("city") or "").strip()
    district = str(location.get("district") or "").strip()
    if not value or not saved or not district:
        return value
    district_root = district[:-1] if district.endswith(("區", "縣", "市")) else district
    if value in {district, district_root, f"{city}{district}", f"{city}{district_root}"}:
        return saved
    return value


_PLACE_FAILURE_MESSAGES = {
    "location_not_found": "\u7121\u6cd5\u89e3\u6790\u9019\u500b\u5730\u9ede",
    "location_required": "\u8acb\u63d0\u4f9b\u4e00\u500b\u5730\u9ede",
    "map_timeout": "\u5730\u5716\u67e5\u8a62\u903e\u6642",
    "google_places_timeout": "\u5730\u5716\u67e5\u8a62\u903e\u6642",
    "map_unavailable": "\u5730\u5716\u670d\u52d9\u66ab\u6642\u7121\u6cd5\u4f7f\u7528",
    "google_places_unavailable": "\u5730\u5716\u670d\u52d9\u66ab\u6642\u7121\u6cd5\u4f7f\u7528",
    "map_invalid_response": "\u5730\u5716\u670d\u52d9\u66ab\u6642\u7121\u6cd5\u4f7f\u7528",
    "google_places_invalid_response": "\u5730\u5716\u670d\u52d9\u66ab\u6642\u7121\u6cd5\u4f7f\u7528",
    "map_rate_limited": "\u5730\u5716\u670d\u52d9\u76ee\u524d\u8f03\u5fd9\uff0c\u8acb\u7a0d\u5f8c\u518d\u8a66",
    "google_places_rate_limited": "\u5730\u5716\u670d\u52d9\u76ee\u524d\u8f03\u5fd9\uff0c\u8acb\u7a0d\u5f8c\u518d\u8a66",
    "map_access_denied": "\u76ee\u524d\u7121\u6cd5\u4f7f\u7528\u5730\u5716\u670d\u52d9",
    "google_places_access_denied": "\u76ee\u524d\u7121\u6cd5\u4f7f\u7528\u5730\u5716\u670d\u52d9",
    "maps_disabled": "\u5730\u5716\u67e5\u8a62\u76ee\u524d\u672a\u555f\u7528",
    "google_places_disabled": "\u5730\u5716\u67e5\u8a62\u76ee\u524d\u672a\u555f\u7528",
}
_PLACE_FAILURE_SUBJECT_FIELDS = {
    "places.search_nearby": "anchor",
    "places.resolve_place": "query",
    "places.measure_distance": "destination",
}
_PLACE_INTERNAL_SUBJECT_RE = re.compile(
    r"(?:objectid|place_candidate_|seed_user_|demo_user|user[_-]?\d+|[0-9a-f]{24}|"
    r"[0-9a-f]{8}-[0-9a-f-]{27,})",
    re.IGNORECASE,
)


def _safe_place_failure_subject(tool_name: str, arguments: dict[str, Any]) -> str:
    field = _PLACE_FAILURE_SUBJECT_FIELDS.get(tool_name)
    if not field:
        return ""
    subject = re.sub(r"\s+", " ", str(arguments.get(field) or "")).strip()[:80]
    if not subject or contains_internal_identifier(subject) or _PLACE_INTERNAL_SUBJECT_RE.search(subject):
        return ""
    return subject


def _place_failure_observation(
    tool_name: str, arguments: dict[str, Any], error_code: str | None,
) -> dict[str, Any] | None:
    if tool_name not in {
        "places.search_nearby", "places.measure_distance", "places.resolve_place",
    }:
        return None
    code = str(error_code or "")
    message = _PLACE_FAILURE_MESSAGES.get(code)
    if not message:
        return None
    failure: dict[str, Any] = {"code": code}
    subject = _safe_place_failure_subject(tool_name, arguments)
    if subject:
        failure["subject"] = subject
    failure["message"] = message
    return {"failure": failure}


def _places_nearby(ctx: AgentTurnContext, arguments: dict[str, Any]) -> ToolResult:
    anchor = str(arguments.get("anchor") or "").strip()
    origin_kind = "explicit"
    if not anchor and arguments.get("use_saved_location"):
        anchor = _saved_location(ctx)
        origin_kind = "saved_profile"
    if not anchor:
        return ToolResult(ok=False, error_code="location_required", user_message="你想從哪個地點開始找？")
    anchor = _canonical_place_anchor(ctx, anchor)
    categories = [str(item) for item in (arguments.get("categories") or [])]
    cuisine = str(arguments.get("cuisine") or "").strip()
    safe_limit = int(arguments.get("limit") or 3)
    data = None
    # Google is an optional presentation enhancement. Its failure must never
    # take away the existing OpenStreetMap place discovery capability.
    if google_place_cards_enabled():
        try:
            point = nominatim_search(anchor)
            google_places = search_nearby_places(
                str(point.get("label") or anchor), float(point["lat"]), float(point["lon"]), categories,
                limit=safe_limit, cuisine=cuisine,
                radius_m=int(arguments.get("radius_m") or 1500),
            )
            data = {
                "anchor_label": str(point.get("label") or anchor),
                "distance_basis": "straight_line",
                "attribution": "Google Maps",
                "attribution_url": "https://www.google.com/maps",
                "requested_categories": categories[:3],
                "requested_cuisine": cuisine[:30],
                "radius_m": int(arguments.get("radius_m") or 1500),
                "requested_limit": safe_limit,
                "ordering": str(arguments.get("ordering") or "distance"),
                "places": google_places,
            }
        except (MapClientError, GooglePlacesError, KeyError, TypeError, ValueError):
            data = None
    if data is None:
        try:
            data = nearby_places(
                anchor, categories,
                radius_m=int(arguments.get("radius_m") or 1500), limit=safe_limit,
            )
        except MapClientError as exc:
            return ToolResult(ok=False, error_code=exc.code, user_message="")
    return ToolResult(ok=True, data={
        **data,
        "origin_kind": origin_kind,
        "requested_categories": categories[:3],
        "requested_cuisine": cuisine[:30],
        "radius_m": int(arguments.get("radius_m") or 1500),
        "requested_limit": safe_limit,
        "ordering": str(arguments.get("ordering") or "distance"),
    })


def _places_distance(ctx: AgentTurnContext, arguments: dict[str, Any]) -> ToolResult:
    origin = str(arguments.get("origin") or "").strip()
    origin_kind = "explicit"
    if not origin and arguments.get("use_saved_origin"):
        origin = _saved_location(ctx)
        origin_kind = "saved_profile"
    if not origin:
        return ToolResult(ok=False, error_code="location_required", user_message="你想從哪裡出發？")
    destination = str(arguments.get("destination") or "")
    # Try Google Routes API for real driving distance; fall back to OSM haversine.
    # The fallback keeps the tool working without Google quota.
    data = None
    if google_routes_enabled():
        try:
            data = measure_distance_matrix(origin, destination)
        except Exception:
            data = None
    if data is None:
        try:
            data = measure_distance(origin, destination)
        except MapClientError as exc:
            return ToolResult(ok=False, error_code=exc.code, user_message="")
    return ToolResult(ok=True, data={**data, "origin_kind": origin_kind})


def _places_resolve(arguments: dict[str, Any]) -> ToolResult:
    query = str(arguments.get("query") or "")
    place = None
    if google_place_cards_enabled():
        try:
            place = resolve_google_place(query)
        except GooglePlacesError:
            place = None
    if place is None:
        try:
            place = resolve_osm_place(query)
        except MapClientError as exc:
            return ToolResult(ok=False, error_code=exc.code, user_message="")
    # Google resolve already carries photo_url from the Text Search response
    # (places.photos is a Pro-tier field). No extra Place Details request is
    # made: rating / opening hours are Enterprise-tier and intentionally absent.
    provider = str((place or {}).get("provider") or "openstreetmap")
    return ToolResult(ok=True, data={
        "found": bool(place), "place": place,
        "attribution": "Google Maps" if provider == "google" else "© OpenStreetMap contributors",
        "attribution_url": "https://www.google.com/maps" if provider == "google" else "https://www.openstreetmap.org/copyright",
    })


def execute_tool(
    call: ToolCall, ctx: AgentTurnContext, *, clock: TurnClockV1 | None = None, dry_run: bool = False,
) -> ToolResult:
    """Execute a read-only tool. Mutations are handled explicitly by runtime actions."""
    spec = get_tool_spec(call.name)
    if spec is None or spec.risk is not ToolRisk.READ:
        return ToolResult(ok=False, error_code="tool_not_allowed", user_message="這個請求目前不能由阿月直接執行。")
    arguments = validate_executor_arguments(spec, call.arguments)
    if arguments is None:
        return ToolResult(ok=False, error_code="invalid_tool_arguments", user_message="這個請求的資訊格式不正確，我沒有執行它。")
    executors = {
        "calendar_events": lambda: _calendar_events(ctx.user_id, clock, arguments),
        "calendar_next_event": lambda: _calendar_next_event(ctx.user_id, clock),
        "calendar_event_find": lambda: _calendar_find_event(ctx, arguments),
        "calendar_mutation_verification": lambda: _calendar_verify_recent_mutation(ctx),
        "current_time": lambda: _current_time(clock),
        "match_status": lambda: _match_status(ctx.user_id),
        "counterparty_summary": lambda: _counterparty_summary(ctx),
        "recent_context": lambda: _recent_context(ctx),
        "relationship_evidence": lambda: _relationship_evidence(ctx, arguments.get("other_id")),
        "mentioned_contact_summary": lambda: _mentioned_contact_summary(ctx, arguments.get("other_ids") or []),
        "accepted_contact_list": lambda: _accepted_contact_list(ctx),
        "memory_profile": lambda: _memory_profile(ctx),
        "self_profile": lambda: _self_profile(ctx),
        "web_search": lambda: _web_search(ctx, arguments),
        "web_extract": lambda: _web_extract(arguments),
        "places_nearby": lambda: _places_nearby(ctx, arguments),
        "places_distance": lambda: _places_distance(ctx, arguments),
        "places_resolve": lambda: _places_resolve(arguments),
    }
    executor = executors.get(spec.executor_key)
    if executor is not None:
        result = executor()
        if not result.ok:
            failure = _place_failure_observation(call.name, arguments, result.error_code)
            if failure is not None:
                return result.model_copy(update={"data": failure})
        if result.ok:
            try:
                spec.output_model.model_validate(result.data)
            except Exception:
                return ToolResult(ok=False, error_code="invalid_tool_output", user_message="我現在無法安全地整理這項資訊。")
        return result
    return ToolResult(ok=False, error_code="tool_not_allowed", user_message="這個請求目前不能由阿月直接執行。")

