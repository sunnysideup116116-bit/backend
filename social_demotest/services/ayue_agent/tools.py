"""Narrow tool facades for the public-Ayue agent.

Tools deliberately expose only user-safe, task-specific data.  They reuse the
existing domain services instead of owning matching, calendar, or memory data.
"""

from __future__ import annotations

import re
from datetime import datetime, time as time_value, timedelta, timezone
from typing import Any

from database import matches_coll, profiles_coll
from services.calendar_service import calendar_access_enabled, get_calendar_context, get_timezone
from services.match_state_service import (
    get_counterparty_match_source,
    get_match_status_snapshot,
    verified_accepted_match_query,
)
from services.profile_projection import safe_recent_context
from services.profile_location import safe_profile_location

from .contracts import AgentTurnContext, ToolCall, ToolResult, TurnClockV1
from .time_context import clock_utc
from .tool_registry import ToolRisk, get_tool_spec, validate_executor_arguments
from .public_relationship_projection import (
    display_name as _display_name,
    mentioned_contact_summary,
    other_id as _other_id,
    public_text as _public_text,
    safe_match_reason as _safe_proposal_summary,
    safe_public_profile as _public_counterparty_profile,
    verified_common_ground as _verified_common_ground,
)
from .web_tools import extract_web, search_web
from .maps_client import MapClientError, measure_distance, nearby_places


def _calendar_events(user_id: str, clock: TurnClockV1 | None = None) -> ToolResult:
    if not calendar_access_enabled(user_id):
        return ToolResult(ok=False, error_code="calendar_access_denied", user_message="你目前沒有授權我讀取行事曆。")
    now = clock_utc(clock) if clock else datetime.now(timezone.utc)
    end = now + timedelta(days=90)
    range_label = "next_90_days"
    if clock and clock.temporal_references:
        target_date = next(iter(clock.temporal_references.values()))
        zone = get_timezone(clock.timezone)
        local_start = datetime.combine(datetime.fromisoformat(target_date).date(), time_value.min, zone)
        now, end = local_start.astimezone(timezone.utc), (local_start + timedelta(days=1)).astimezone(timezone.utc)
        range_label = target_date
    events = get_calendar_context(user_id, None, now, end).get("viewer_events", [])
    safe_events = []
    for event in events:
        if event.get("status") == "cancelled":
            continue
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
    return ToolResult(ok=True, data={"events": safe_events, "range": range_label})


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
    return ToolResult(ok=True, data={
        "found": True,
        "match_state": status,
        "display_name": _display_name(other_id),
        "safe_summary": _safe_proposal_summary(match, ctx.user_id),
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


def _memory_profile(ctx: AgentTurnContext) -> ToolResult:
    profile = ctx.user_profile or profiles_coll.find_one({"user_id": ctx.user_id}, {"_id": 0}) or {}
    return ToolResult(ok=True, data={
        "summary": profile.get("profile_memory_summary", ""),
        "current_context": safe_recent_context(profile.get("current_context"), ""),
        "preferences": profile.get("profile_memory_preview", [])[:8],
    })


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


def _places_nearby(ctx: AgentTurnContext, arguments: dict[str, Any]) -> ToolResult:
    anchor = str(arguments.get("anchor") or "").strip()
    origin_kind = "explicit"
    if not anchor and arguments.get("use_saved_location"):
        anchor = _saved_location(ctx)
        origin_kind = "saved_profile"
    if not anchor:
        return ToolResult(ok=False, error_code="location_required", user_message="你想從哪個地點開始找？")
    try:
        data = nearby_places(
            anchor,
            [str(item) for item in (arguments.get("categories") or [])],
            radius_m=int(arguments.get("radius_m") or 1500),
            limit=int(arguments.get("limit") or 8),
        )
    except MapClientError as exc:
        return ToolResult(ok=False, error_code=exc.code, user_message="")
    return ToolResult(ok=True, data={**data, "origin_kind": origin_kind})


def _places_distance(ctx: AgentTurnContext, arguments: dict[str, Any]) -> ToolResult:
    origin = str(arguments.get("origin") or "").strip()
    origin_kind = "explicit"
    if not origin and arguments.get("use_saved_origin"):
        origin = _saved_location(ctx)
        origin_kind = "saved_profile"
    if not origin:
        return ToolResult(ok=False, error_code="location_required", user_message="你想從哪裡出發？")
    try:
        data = measure_distance(origin, str(arguments.get("destination") or ""))
    except MapClientError as exc:
        return ToolResult(ok=False, error_code=exc.code, user_message="")
    return ToolResult(ok=True, data={**data, "origin_kind": origin_kind})


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
        "calendar_events": lambda: _calendar_events(ctx.user_id, clock),
        "current_time": lambda: _current_time(clock),
        "match_status": lambda: _match_status(ctx.user_id),
        "counterparty_summary": lambda: _counterparty_summary(ctx),
        "recent_context": lambda: _recent_context(ctx),
        "relationship_evidence": lambda: _relationship_evidence(ctx, arguments.get("other_id")),
        "mentioned_contact_summary": lambda: _mentioned_contact_summary(ctx, arguments.get("other_ids") or []),
        "memory_profile": lambda: _memory_profile(ctx),
        "web_search": lambda: _web_search(ctx, arguments),
        "web_extract": lambda: _web_extract(arguments),
        "places_nearby": lambda: _places_nearby(ctx, arguments),
        "places_distance": lambda: _places_distance(ctx, arguments),
    }
    executor = executors.get(spec.executor_key)
    if executor is not None:
        result = executor()
        if result.ok:
            try:
                spec.output_model.model_validate(result.data)
            except Exception:
                return ToolResult(ok=False, error_code="invalid_tool_output", user_message="我現在無法安全地整理這項資訊。")
        return result
    return ToolResult(ok=False, error_code="tool_not_allowed", user_message="這個請求目前不能由阿月直接執行。")

