"""The single fail-closed public Ayue V2 agent loop."""

from __future__ import annotations

import os
import re
import time
import uuid
from typing import Any, Callable

from database import db, profiles_coll
from services.match_action_service import decide_active_proposal, start_match_search

from .context import build_agent_turn_context_v2
from .contracts import AgentResult, AgentTurnContext, DecisionKind, ToolCall
from .capabilities import CAPABILITY_MANIFEST_VERSION, matching_truth_reply
from .match_opportunity import (
    assess_match_opportunity,
    missing_basis_question,
    record_guidance_declined,
    record_guidance_shown,
)
from .router import (
    confirmation_choice,
    generate_clarification_reply_v2,
    generate_final_reply_v2,
    guard_v2_decision,
    planner_final_reply_v2,
    plan_turn_v2,
    tool_policy_for_turn,
)
from .time_context import build_turn_clock
from .tool_registry import ToolRisk, executor_arguments_for_turn, get_tool_spec, tool_call_key
from .tools import execute_tool
from .public_relationship_projection import validated_mentioned_contact_ids
from .web_tools import is_safe_public_url


RUNS = db["agent_runs"]
TOOL_CALLS = db["agent_tool_calls"]
MAX_STEPS = max(1, min(int(os.getenv("AYUE_AGENT_MAX_STEPS", "3")), 3))
ProgressCallback = Callable[[dict[str, Any]], Any]


def agent_mode_for_user(user_id: str) -> str:
    """V2 is manual-rollout only; `off` is the emergency legacy rollback."""
    mode = os.getenv("AYUE_AGENT_V2_MODE", "off").strip().lower()
    if mode not in {"off", "on"}:
        mode = "off"
    allowlist = {value.strip() for value in os.getenv("AYUE_AGENT_V2_USER_ALLOWLIST", "").split(",") if value.strip()}
    return mode if not allowlist or user_id in allowlist else "off"


def ensure_indexes() -> None:
    try:
        RUNS.create_index("created_at", expireAfterSeconds=14 * 86400)
        RUNS.create_index([("user_id", 1), ("created_at", -1)])
        TOOL_CALLS.create_index("idempotency_key", unique=True)
    except Exception as exc:
        print(f"Agent run index setup skipped: {exc}")


def _persist_trace(run_id: str, ctx: AgentTurnContext, payload: dict[str, Any]) -> None:
    """Persist an allowlisted, privacy-safe V2 trace only."""
    composer_reason = str((payload.get("composer_outcome") or {}).get("reason") or "not_used")
    if composer_reason not in {
        "not_used", "planner_invalid", "planner_final", "clarification", "manifest_reply",
        "duplicate_observation_reused", "read_limit_composed",
    }:
        composer_reason = "unknown"
    composer_result_code = str((payload.get("composer_outcome") or {}).get("result_code") or "unknown")
    if composer_result_code not in {
        "not_used", "unknown", "llm_reply", "planner_reply",
        "deterministic_fallback:internal_meta_rejected",
        "deterministic_fallback:empty_reply",
        "deterministic_fallback:provider_error",
    }:
        composer_result_code = "unknown"
    planner_decisions = [
        {
            "kind": str(item.get("kind") or ""),
            "tool_name": str(item.get("tool_name") or "") or None,
            "confidence": round(float(item.get("confidence") or 0), 3),
        }
        for item in (payload.get("planner_decisions") or [])
        if isinstance(item, dict)
    ]
    tool_results = [
        {
            "tool": str(item.get("tool") or ""),
            "ok": bool(item.get("ok")),
            "code": str(item.get("code") or "") or None,
        }
        for item in (payload.get("tool_results") or [])
        if isinstance(item, dict)
    ]
    composer = payload.get("composer_outcome") or {}
    safe_payload = {
        "context_version": str(payload.get("context_version") or "v2"),
        "mentioned_contact_count": min(3, max(0, int(payload.get("mentioned_contact_count") or 0))),
        "capability_manifest_version": (
            CAPABILITY_MANIFEST_VERSION
            if str(payload.get("capability_manifest_version") or "") == CAPABILITY_MANIFEST_VERSION
            else "unknown"
        ),
        "opportunity_state": (
            str(payload.get("opportunity_state") or "not_evaluated")
            if str(payload.get("opportunity_state") or "not_evaluated") in {
                "not_evaluated", "not_ready", "ready", "suppressed", "active_match_blocked",
            }
            else "unknown"
        ),
        "opportunity_reason_codes": [
            str(code) for code in (payload.get("opportunity_reason_codes") or [])
            if str(code) in {
                "active_match", "recent_decline", "user_declined", "same_fingerprint",
                "cooldown", "profile_basis_insufficient",
            }
        ],
        "guidance_shown": bool(payload.get("guidance_shown")),
        "visible_tools": [
            str(name) for name in (payload.get("visible_tools") or [])
            if get_tool_spec(str(name)) is not None
        ],
        "planner_decisions": planner_decisions,
        "guard_results": [str(reason) for reason in (payload.get("guard_results") or [])],
        "tool_results": tool_results,
        "event_sequence": [
            str(event) for event in (payload.get("event_sequence") or [])
            if event in {"run_started", "tool_started", "tool_finished", "final", "error"}
        ],
        "tool_cache_hits": [
            str(tool) for tool in (payload.get("tool_cache_hits") or [])
            if get_tool_spec(str(tool)) is not None
        ],
        "composer_outcome": {
            "reason": composer_reason,
            "observation_count": max(0, int(composer.get("observation_count") or 0)),
            "result_code": composer_result_code,
        },
        "public_progress_result_codes": [
            str(code)
            for code in (payload.get("public_progress_result_codes") or [])
            if str(code) in {
                f"{event}:{result}"
                for event in ("run_started", "tool_started", "tool_finished")
                for result in ("emitted", "dropped", "no_listener", "callback_error")
            }
        ],
        "confirmation": str(payload.get("confirmation") or "none"),
        "clarification": (
            str(payload.get("clarification") or "none")
            if str(payload.get("clarification") or "none") in {"none", "calendar_action", "recent_context"}
            else "unknown"
        ),
        "profile_input": (
            str(payload.get("profile_input") or "casual")
            if str(payload.get("profile_input") or "casual") in {
                "casual", "calendar_action", "relationship", "match_action", "match_status",
                "time", "recent_context_prompt", "unknown",
            }
            else "unknown"
        ),
        "exception": str(payload.get("exception") or "") or None,
        "result": {
            "handled": bool((payload.get("result") or {}).get("handled")),
            "conversation_intent": str((payload.get("result") or {}).get("conversation_intent") or ""),
            "fallback_reason": str((payload.get("result") or {}).get("fallback_reason") or "") or None,
        },
        "latency_ms": max(0, int(payload.get("latency_ms") or 0)),
        "context_ms": max(0, int(payload.get("context_ms") or 0)),
        "model_ms": [max(0, int(value)) for value in (payload.get("model_ms") or [])[:4]],
        "tool_ms": [max(0, int(value)) for value in (payload.get("tool_ms") or [])[:3]],
    }
    try:
        RUNS.insert_one({
            "run_id": run_id, "user_id": ctx.user_id, "room_id": ctx.room_id,
            "agent_version": "v2", "created_at": time.time(), **safe_payload,
        })
    except Exception as exc:
        print(f"Agent trace skipped: {type(exc).__name__}")


def _save_trace(run_id: str, ctx: AgentTurnContext, payload: dict[str, Any]) -> None:
    """Trace failures are always non-fatal to the user turn."""
    try:
        _persist_trace(run_id, ctx, payload)
    except Exception as exc:
        print(f"Agent trace skipped: {type(exc).__name__}")


def _emit_progress(
    callback: ProgressCallback | None, event_type: str, *, trace: dict[str, Any] | None = None,
    **payload: Any,
) -> None:
    """Best-effort public progress event; never let stream delivery affect a run."""
    if trace is not None:
        trace["event_sequence"].append(event_type)
    delivery_code = "no_listener"
    if callback is None:
        if trace is not None:
            trace["public_progress_result_codes"].append(f"{event_type}:{delivery_code}")
        return
    try:
        delivered = callback({"type": event_type, **payload})
        delivery_code = "dropped" if delivered is False else "emitted"
    except Exception:
        delivery_code = "callback_error"
    if trace is not None:
        trace["public_progress_result_codes"].append(f"{event_type}:{delivery_code}")


def _compose_final_reply(turn: Any, observations: list[dict], trace: dict[str, Any], reason: str) -> str:
    """Record composer metadata without recording user text or observations."""
    result_code = "unknown"

    def record_outcome(value: str) -> None:
        nonlocal result_code
        result_code = value

    started = time.perf_counter()
    reply = generate_final_reply_v2(turn, observations, outcome_sink=record_outcome)
    trace.setdefault("model_ms", []).append(round((time.perf_counter() - started) * 1000))
    trace["composer_outcome"] = {
        "reason": reason,
        "observation_count": len(observations),
        "result_code": result_code,
    }
    return reply


def _final_reply_for_decision(
    turn: Any, decision: Any, observations: list[dict], trace: dict[str, Any], reason: str,
) -> str:
    """Prefer the terminal planner reply; retain Composer as a safe fallback."""
    reply = planner_final_reply_v2(turn, decision)
    if reply:
        trace["composer_outcome"] = {
            "reason": reason,
            "observation_count": len(observations),
            "result_code": "planner_reply",
        }
        return reply
    return _compose_final_reply(turn, observations, trace, reason)


def _public_sources(observations: list[dict]) -> list[dict[str, str]]:
    """Keep only display-safe citations; never persist web page content."""
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for observation in observations:
        tool_name = str(observation.get("tool") or "")
        data = observation.get("result") or {}
        if tool_name == "web.search":
            candidates = data.get("results") or []
        elif tool_name == "web.extract":
            candidates = data.get("pages") or []
        elif tool_name == "places.search_nearby":
            candidates = [
                {"title": str(item.get("name") or "地圖"), "url": str(item.get("map_url") or "")}
                for item in (data.get("places") or [])
            ]
        elif tool_name == "places.measure_distance":
            candidates = [{
                "title": str(data.get("attribution") or "OpenStreetMap"),
                "url": str(data.get("attribution_url") or ""),
            }]
        else:
            continue
        for item in candidates:
            url = str((item or {}).get("url") or "")
            if not is_safe_public_url(url) or url in seen:
                continue
            seen.add(url)
            title = re.sub(r"\s+", " ", str((item or {}).get("title") or "")).strip()[:140]
            sources.append({"title": title or url, "url": url})
            if len(sources) == 5:
                return sources
    return sources


def _web_extract_urls_allowed(ctx: AgentTurnContext, observations: list[dict], urls: list[str]) -> bool:
    """Bind extraction to a search result or an URL the owner actually supplied."""
    allowed = set()
    for observation in observations:
        if observation.get("tool") != "web.search":
            continue
        for item in ((observation.get("result") or {}).get("results") or []):
            url = str((item or {}).get("url") or "")
            if is_safe_public_url(url):
                allowed.add(url)
    for raw in re.findall(r"https?://[^\s<>\]\[\"']+", ctx.message or ""):
        url = raw.rstrip(".,，。!?！？:：;；)")
        if is_safe_public_url(url):
            allowed.add(url)
    return bool(urls) and all(str(url) in allowed for url in urls)


def _clarification_topic(
    decision: Any | None = None, *, tool_name: str | None = None, error_code: str | None = None,
) -> str:
    name = tool_name or str(getattr(decision, "tool_name", "") or "")
    intent = str(getattr(getattr(decision, "intent", None), "value", "") or "")
    if name == "match.start_search" or intent == "match_action":
        return "match_target"
    if error_code == "calendar_access_denied":
        return "calendar_access"
    if name in {"calendar.list_my_events", "calendar.create_my_event", "calendar.update_my_event", "calendar.cancel_my_event", "system.get_current_time"} or intent in {"calendar", "calendar_action", "time"}:
        return "schedule"
    if name in {"match.get_counterparty_summary", "relationship.get_verified_evidence", "relationship.get_mentioned_contact_summary"} or intent == "relationship":
        return "relationship"
    if error_code == "location_required" or str(getattr(decision, "clarification_goal", "") or "") == "location":
        return "location"
    return "request"


def _compose_clarification(
    turn: Any,
    observations: list[dict],
    trace: dict[str, Any],
    *,
    topic: str,
) -> str:
    result_code = "unknown"

    def record_outcome(value: str) -> None:
        nonlocal result_code
        result_code = value

    reply = generate_clarification_reply_v2(
        turn, topic=topic, observations=observations, outcome_sink=record_outcome,
    )
    trace["composer_outcome"] = {
        "reason": "clarification",
        "observation_count": len(observations),
        "result_code": result_code,
    }
    return reply


def _record_opportunity_trace(trace: dict[str, Any], assessment: Any) -> None:
    trace["opportunity_state"] = str(getattr(assessment, "state", "not_ready"))
    trace["opportunity_reason_codes"] = [str(item) for item in getattr(assessment, "reason_codes", ())]


def _match_offer_reply(turn: Any, decision: Any) -> str:
    evidence = str(getattr(decision, "opportunity_evidence_span", "") or "").strip()
    lead = f"你提到「{evidence}」。" if evidence and evidence in turn.message else ""
    return (
        f"{lead}感覺這件事有人一起也不錯。"
        "我可以依你的近況和個性找一位合適人選，不會隨機配；要試試看嗎？"
    )


def _handle_match_opportunity(
    ctx: AgentTurnContext, turn: Any, decision: Any, trace: dict[str, Any], run_id: str,
) -> AgentResult | None:
    """Create a safe confirmation from a planner-grounded typed decision."""
    is_explicit_action = (
        decision.kind == DecisionKind.CONFIRMATION
        and str(getattr(getattr(decision, "intent", None), "value", "")) == "match_action"
        and str(getattr(decision, "tool_name", "")) == "match.start_search"
        and bool(getattr(decision, "evidence_span", ""))
        and decision.evidence_span in turn.message
    )
    is_social_opening = (
        decision.kind == DecisionKind.FINAL
        and decision.opportunity_signal == "social_opening"
        and decision.opportunity_confidence >= 0.8
        and bool(decision.opportunity_evidence_span)
        and decision.opportunity_evidence_span in turn.message
    )
    if not is_explicit_action and not is_social_opening:
        return None
    assessment = assess_match_opportunity(ctx.user_profile or {}, ctx.user_id, explicit_search=is_explicit_action)
    turn.match_opportunity_state = assessment.state
    _record_opportunity_trace(trace, assessment)
    if is_explicit_action:
        if assessment.state == "not_ready":
            return AgentResult(
                handled=True,
                reply="我想先多了解你的方向，才能幫你找得更準。" + missing_basis_question(assessment),
                conversation_intent="match_clarification",
                agent_run_id=run_id,
                agent_mode="v2",
                match_readiness_state=assessment.state,
            )
        if assessment.state == "active_match_blocked":
            return AgentResult(
                handled=True,
                reply="你目前還有一段配對正在進行，我先不重複開新搜尋。",
                conversation_intent="match_status",
                agent_run_id=run_id,
                agent_mode="v2",
                match_readiness_state=assessment.state,
            )
        pending = _new_pending_search(source="explicit_request")
        claimed = profiles_coll.update_one(
            {"user_id": ctx.user_id, "$or": [
                {"agentic_pending_confirmation": {"$exists": False}},
                {"agentic_pending_confirmation": None},
            ]},
            {"$set": {"agentic_pending_confirmation": pending}},
        )
        if not getattr(claimed, "modified_count", 0):
            return AgentResult(
                handled=True,
                reply="上一個找人確認還在等你決定；你可以回覆「確認」或「取消」。",
                conversation_intent="match_confirmation",
                agent_run_id=run_id,
                agent_mode="v2",
                match_readiness_state=assessment.state,
            )
        trace["confirmation"] = "created"
        return AgentResult(
            handled=True,
            reply=(
                "我會依你的近況、偏好和個性挑選，不會隨機配對。"
                "要我現在開始找就回覆「確認」；也可以先補充條件。"
            ),
            conversation_intent="match_confirmation",
            agent_run_id=run_id,
            agent_mode="v2",
            match_readiness_state=assessment.state,
        )
    if assessment.state != "ready":
        return None
    pending = _new_pending_search(source="opportunity_guidance", guidance_fingerprint=assessment.fingerprint)
    claimed = profiles_coll.update_one(
        {
            "user_id": ctx.user_id,
            "$or": [
                {"agentic_pending_confirmation": {"$exists": False}},
                {"agentic_pending_confirmation": None},
            ],
            "match_guidance.last_fingerprint": {"$ne": assessment.fingerprint},
        },
        {"$set": {
            "agentic_pending_confirmation": pending,
            "match_guidance": record_guidance_shown(ctx.user_id, assessment.fingerprint),
        }},
    )
    if not getattr(claimed, "modified_count", 0):
        return None
    trace["guidance_shown"] = True
    return AgentResult(
        handled=True,
        reply=_match_offer_reply(turn, decision),
        conversation_intent="match_guidance",
        agent_run_id=run_id,
        agent_mode="v2",
        match_readiness_state="ready",
        match_guidance_shown=True,
    )


def _reply_from_observation(tool_name: str, data: dict[str, Any]) -> str:
    if tool_name == "system.get_current_time":
        date = str(data.get("local_date") or "")
        local_time = str(data.get("local_time") or "")
        weekday = str(data.get("weekday_zh_tw") or "")
        refs = data.get("temporal_references") or {}
        if refs:
            term, value = next(iter(refs.items()))
            return f"以{data.get('timezone', 'Asia/Taipei')}時間來看，{term}是 {value}，{weekday}。"
        return f"現在是{data.get('timezone', 'Asia/Taipei')}時間 {date} {weekday} {local_time}。"
    if tool_name == "match.get_status":
        state = data.get("state")
        counterpart = str(data.get("counterparty") or "對方")
        templates = {
            "idle": "目前沒有進行中的配對。",
            "searching": "我正在幫你找人，目前還沒有產生新的提案。",
            "waiting_user": "目前有一張牽線提案正在等你決定。",
            "waiting_other": f"你已經表示有興趣，目前正在等{counterpart}回覆。",
            "incoming_decision": "目前有一張牽線提案正在等你決定。",
            "accepted": f"有，{counterpart}也已經接受了，聊天室已經開啟。",
            "declined": f"這次提案沒有成功，{counterpart}已婉拒。",
            "expired": "這張提案已經過期，現在不能再操作。",
            "no_candidates": "這一輪暫時沒有找到合適的新對象。",
            "failed": "這次配對沒有成功，我沒有把它當成已完成。",
            "cancelled": "這次找人的請求已取消。",
        }
        return templates.get(state, "我目前找不到可確認的配對狀態。")
    if tool_name == "match.get_latest_outcome":
        if not data.get("found"):
            return "我目前找不到一筆可確認的媒合結果。"
        if data.get("status") == "accepted":
            return "對方已經接受了，聊天室也已經開啟。"
        return "這次對方先婉拒了，但沒有留下可確認的具體原因，所以我不能替對方亂猜。"
    if tool_name == "match.get_active_state":
        active = data.get("active") or []
        return "目前有一張進行中的牽線提案。" if active else "目前沒有進行中的牽線提案。"
    if tool_name == "calendar.list_my_events":
        events = data.get("events") or []
        if not events:
            return "接下來 90 天我沒有讀到你的行程。"
        lines = [f"{event['date'][5:].replace('-', '/')} {event['start_time']}–{event['end_time']} {event['activity']}" for event in events[:5]]
        return "我讀到你的行程：\n" + "\n".join(lines)
    return "我查到一些和你這題相關的資訊。"


def _start_search(
    ctx: AgentTurnContext, run_id: str, index: int, *, idempotency_key: str | None = None,
) -> tuple[bool, str, str | None]:
    """One idempotent side effect. The matching domain still owns creation."""
    idempotency_key = idempotency_key or f"{run_id}:{index}"
    prior = TOOL_CALLS.find_one_and_update(
        {"idempotency_key": idempotency_key},
        {"$setOnInsert": {"idempotency_key": idempotency_key, "created_at": time.time(), "state": "running"}},
        upsert=True,
    )
    if prior:
        return True, str((prior.get("result") or {}).get("reply") or "我已經處理過這次搜尋。"), None
    try:
        result = start_match_search(ctx.user_id, source="agent_v2", force_new=True)
        status = result.get("status", "failed")
        reply = {
            "queued": "我已經開始幫你找人，找到結果會回來告訴你。",
            "already_active": "你目前還有一張進行中的提案，我先不重複開新搜尋。",
            "already_searching": "我正在幫你找，先不用重複送出。",
            "no_candidates": "這輪暫時沒有合適的新對象。",
        }.get(status, "這次搜尋沒有成功啟動，我沒有把它當作已完成。")
        TOOL_CALLS.update_one({"idempotency_key": idempotency_key}, {"$set": {"state": "done", "result": {"status": status, "reply": reply}}})
        return status in {"queued", "already_active", "already_searching", "no_candidates"}, reply, None
    except Exception as exc:
        return False, "我現在不能安全地開始搜尋，請稍後再試。", type(exc).__name__


def _new_pending_search(*, source: str = "explicit_request", guidance_fingerprint: str | None = None) -> dict[str, Any]:
    now = time.time()
    return {
        "version": "v2", "confirmation_id": uuid.uuid4().hex,
        "action": "match.start_search", "arguments": {}, "status": "pending",
        "created_at": now, "expires_at": now + 15 * 60, "proposal_revision": None,
        "source": source, "guidance_fingerprint": guidance_fingerprint,
    }


_CALENDAR_WRITE_ACTIONS = {
    "calendar.create_my_event", "calendar.update_my_event", "calendar.cancel_my_event",
}


def _calendar_event_label(event: dict) -> str:
    from datetime import datetime
    from services.calendar_service import as_utc, get_timezone
    zone = get_timezone(event.get("timezone") or "Asia/Taipei")
    start_value = event["start_at"]
    end_value = event["end_at"]
    if isinstance(start_value, str):
        start_value = datetime.fromisoformat(start_value.replace("Z", "+00:00"))
    if isinstance(end_value, str):
        end_value = datetime.fromisoformat(end_value.replace("Z", "+00:00"))
    start = as_utc(start_value).astimezone(zone)
    end = as_utc(end_value).astimezone(zone)
    if event.get("source_type") == "date":
        title = str(event.get("activity") or event.get("title") or "共同約會").strip()
    else:
        title = str(event.get("title") or event.get("activity") or "這筆行程").strip()
    # %-m / %-d are not portable to the Windows runtime used by this app.
    return f"{start.month}/{start.day} {start:%H:%M}–{end:%H:%M} {title}"


def _calendar_action_error(message: str, run_id: str) -> AgentResult:
    return AgentResult(
        handled=True, reply=message, conversation_intent="calendar_action",
        agent_run_id=run_id, agent_mode="v2",
        profile_write_allowed=False, profile_write_reason="calendar_action",
    )


def _save_action_draft(ctx: AgentTurnContext, turn: Any, decision: Any) -> None:
    """Keep only public, model-safe clarification state; never persist event IDs."""
    existing = turn.action_draft or {}
    action = str(getattr(decision, "tool_name", "") or existing.get("action") or "calendar")
    if action not in _CALENDAR_WRITE_ACTIONS:
        action = "calendar"
    missing = [
        str(field) for field in (getattr(decision, "missing_fields", None) or [])
        if str(field) in {"event_hint", "title", "date", "start_time", "end_time", "changes"}
    ][:4]
    profiles_coll.update_one(
        {"user_id": ctx.user_id},
        {"$set": {"agentic_action_draft": {
            "version": "v1", "domain": "calendar", "action": action,
            "missing_fields": missing, "created_at": time.time(),
        }}},
        upsert=True,
    )


def _clear_action_draft(user_id: str) -> None:
    profiles_coll.update_one({"user_id": user_id}, {"$unset": {"agentic_action_draft": ""}})


def _open_recent_context_draft(ctx: AgentTurnContext) -> None:
    profiles_coll.update_one(
        {"user_id": ctx.user_id},
        {"$set": {"recent_context_draft": {
            "version": "v1", "goal": "activity_or_destination", "created_at": time.time(),
        }}},
        upsert=True,
    )


def _calendar_details_question(action: str | None, arguments: dict[str, Any] | None = None) -> str:
    """Ask only for the minimum missing information; never invent a duration."""
    values = dict(arguments or {})
    if action == "calendar.create_my_event":
        labels = {
            "title": "行程名稱", "date": "日期", "start_time": "開始時間", "end_time": "結束時間",
        }
        missing = [label for key, label in labels.items() if not str(values.get(key) or "").strip()]
        if missing:
            return f"可以，還需要{'、'.join(missing)}，才能幫你新增這筆行程。"
    if action == "calendar.update_my_event":
        if not str(values.get("event_hint") or "").strip():
            return "你想修改哪一筆自己的行程？可以告訴我名稱或日期。"
        changes = [key for key, value in values.items() if key != "event_hint" and value is not None]
        if not changes:
            return "你想把這筆行程改成什麼呢？"
    if action == "calendar.cancel_my_event":
        return "你想取消哪一筆自己的行程？可以告訴我名稱或日期。"
    return "你想新增、修改，還是取消哪一筆自己的行程？"


def _prepare_calendar_confirmation(
    ctx: AgentTurnContext, turn: Any, decision: Any, trace: dict[str, Any], run_id: str,
) -> AgentResult:
    """Validate a proposed personal-calendar change and persist only a confirmation draft."""
    from fastapi import HTTPException
    from services.calendar_service import (
        _parse_local_interval, as_utc, calendar_access_enabled, conflicts_for_viewer,
        get_timezone, normalize_form, resolve_owned_event,
    )

    action = str(decision.tool_name or "")
    spec = get_tool_spec(action)
    if action not in _CALENDAR_WRITE_ACTIONS or spec is None:
        return _calendar_action_error(_calendar_details_question(action, decision.arguments), run_id)
    if not calendar_access_enabled(ctx.user_id):
        return _calendar_action_error("我目前不能存取你的行事曆；你可以先到日曆設定確認是否已授權。", run_id)
    arguments = executor_arguments_for_turn(spec, ctx.mentioned_ids, decision.arguments)
    event: dict | None = None
    conflicts: list[dict] = []
    try:
        if action == "calendar.create_my_event":
            form = normalize_form(arguments)
            start_at, end_at, _ = _parse_local_interval(form)
            arguments = {**arguments, **form}
            conflicts = conflicts_for_viewer(ctx.user_id, [ctx.user_id], start_at, end_at)
            preview = f"要新增 {form['date'][5:].replace('-', '/')} {form['start_time']}–{form['end_time']}「{form['title']}」嗎？"
        else:
            event, resolution = resolve_owned_event(ctx.user_id, arguments.get("event_hint", ""))
            if resolution == "ambiguous":
                return _calendar_action_error("我找到不只一筆符合的行程。你可以補上日期或完整名稱嗎？", run_id)
            if not event:
                return _calendar_action_error("我找不到這筆自己的行程。你可以補上日期或名稱嗎？", run_id)
            is_shared = event.get("source_type") == "date"
            if action == "calendar.cancel_my_event":
                preview = f"要取消「{_calendar_event_label(event)}」嗎？"
                if is_shared:
                    preview += " 這是共同約會，取消後會同步雙方行事曆並通知對方。"
            else:
                if is_shared and event.get("status") != "confirmed":
                    return _calendar_action_error("這筆共同約會正在等待重新確認；你可以先取消目前改期，或直接取消整筆約會。", run_id)
                zone = get_timezone(event.get("timezone") or "Asia/Taipei")
                start = as_utc(event["start_at"]).astimezone(zone)
                end = as_utc(event["end_at"]).astimezone(zone)
                current = {
                    "title": (
                        event.get("activity") if is_shared
                        else event.get("title") or event.get("activity")
                    ) or "行程",
                    "date": start.date().isoformat(), "start_time": start.strftime("%H:%M"),
                    "end_time": end.strftime("%H:%M"), "timezone": event.get("timezone") or "Asia/Taipei",
                    "location": event.get("location") or "", "notes": event.get("notes") or "",
                }
                changes = {key: value for key, value in arguments.items() if key != "event_hint" and value is not None}
                if not changes:
                    return _calendar_action_error("你想把「%s」改成什麼呢？" % _calendar_event_label(event), run_id)
                if is_shared:
                    shared_changes = dict(changes)
                    if "title" in shared_changes:
                        shared_changes["activity"] = shared_changes.pop("title")
                    proposed = normalize_form({
                        **current,
                        "activity": current["title"],
                        "budget": event.get("budget") or "",
                        **shared_changes,
                    })
                else:
                    proposed = normalize_form({**current, **changes})
                start_at, end_at, _ = _parse_local_interval(proposed)
                conflicts = conflicts_for_viewer(
                    ctx.user_id,
                    list(event.get("participants") or [ctx.user_id]) if is_shared else [ctx.user_id],
                    start_at, end_at, event.get("event_id"),
                )
                arguments = {"event_hint": arguments["event_hint"], **changes}
                proposed_title = proposed["activity"] if is_shared else proposed["title"]
                preview = f"要把「{_calendar_event_label(event)}」改成 {proposed['date'][5:].replace('-', '/')} {proposed['start_time']}–{proposed['end_time']}「{proposed_title}」嗎？"
                if is_shared:
                    preview += " 對方會收到改期通知，重新確認後才會正式變更。"
    except HTTPException as exc:
        return _calendar_action_error(f"我還需要補齊行程資訊：{exc.detail}。", run_id)
    if conflicts:
        preview += f" 這會和你現有的 {len(conflicts)} 筆行程重疊；仍要這樣安排嗎？"
    now = time.time()
    pending = {
        "version": "v2", "confirmation_id": uuid.uuid4().hex,
        "action": action, "arguments": arguments, "status": "pending",
        "created_at": now, "expires_at": now + 15 * 60,
        "event_id": event.get("event_id") if event else None,
        "event_revision": int(event.get("revision", 1)) if event else None,
        "event_source_type": event.get("source_type") if event else None,
        "event_other_id": (
            next((person for person in (event.get("participants") or []) if person != ctx.user_id), None)
            if event and event.get("source_type") == "date" else None
        ),
        "coordination_id": event.get("coordination_id") if event else None,
        "proposed_form": proposed if event and action == "calendar.update_my_event" and event.get("source_type") == "date" else None,
        "conflict_count": len(conflicts),
    }
    claimed = profiles_coll.update_one(
        {"user_id": ctx.user_id, "$or": [
            {"agentic_pending_confirmation": {"$exists": False}},
            {"agentic_pending_confirmation": None},
        ]},
        {"$set": {"agentic_pending_confirmation": pending}, "$unset": {"agentic_action_draft": ""}},
    )
    if not getattr(claimed, "modified_count", 0):
        return _calendar_action_error("上一個確認還在等你決定；你可以回覆「確認」或「取消」。", run_id)
    trace["confirmation"] = "created"
    return AgentResult(
        handled=True, reply=preview + " 回覆「確認」才會真的變更。",
        conversation_intent="calendar_confirmation", agent_run_id=run_id, agent_mode="v2",
        profile_write_allowed=False, profile_write_reason="calendar_action",
    )


def _execute_calendar_pending(
    ctx: AgentTurnContext, pending: dict[str, Any], confirmation_id: str,
) -> tuple[bool, str, str | None]:
    from fastapi import HTTPException
    from services.calendar_service import cancel_event, create_personal_event, update_personal_event
    from services.date_coordination_service import cancel_coordination_or_event, request_reschedule

    action = str(pending.get("action") or "")
    arguments = dict(pending.get("arguments") or {})
    key = f"calendar-confirmation:{confirmation_id}"
    try:
        if action == "calendar.create_my_event":
            event = create_personal_event(ctx.user_id, arguments, agent_action_key=key)
            return True, f"已加入行程：{_calendar_event_label(event)}。", None
        event_id = str(pending.get("event_id") or "")
        revision = int(pending.get("event_revision", 0) or 0)
        is_shared = pending.get("event_source_type") == "date"
        other_id = str(pending.get("event_other_id") or "")
        if action == "calendar.update_my_event":
            if is_shared:
                coordination, event = request_reschedule(
                    ctx.user_id,
                    other_id,
                    event_id,
                    dict(pending.get("proposed_form") or {}),
                    expected_revision=revision,
                    idempotency_key=key,
                )
                form = coordination.get("form") or {}
                proposed_label = (
                    f"{str(form.get('date') or '')[5:].replace('-', '/')} "
                    f"{form.get('start_time', '')}–{form.get('end_time', '')} "
                    f"{form.get('activity') or '共同約會'}"
                ).strip()
                return (
                    True,
                    f"已提出改期：{proposed_label}。對方已收到通知，確認後才會正式變更。",
                    None,
                )
            changes = {key: value for key, value in arguments.items() if key != "event_hint" and value is not None}
            event = update_personal_event(
                ctx.user_id, event_id, changes, expected_revision=revision, agent_action_key=key,
            )
            return True, f"已更新行程：{_calendar_event_label(event)}。", None
        if action == "calendar.cancel_my_event":
            if is_shared:
                coordination = cancel_coordination_or_event(
                    ctx.user_id,
                    other_id,
                    str(pending.get("coordination_id") or ""),
                    expected_revision=revision,
                    idempotency_key=key,
                )
                title = str((coordination.get("form") or {}).get("activity") or "共同約會")
                return True, f"已取消共同約會「{title}」，對方已收到通知，雙方行事曆也已同步。", None
            event = cancel_event(
                ctx.user_id, event_id, personal_only=True, expected_revision=revision, agent_action_key=key,
            )
            return True, f"已取消行程：{_calendar_event_label(event)}。", None
        return False, "這個行程確認已失效，請重新告訴我你想怎麼安排。", "unknown_calendar_action"
    except HTTPException as exc:
        if exc.status_code == 409:
            return False, "這筆行程剛剛有變動，我沒有覆寫它。請告訴我最新想怎麼改。", "stale_revision"
        return False, "這筆行程現在無法變更；你可以再告訴我想處理哪一筆嗎？", "calendar_write_failed"
    except Exception as exc:
        return False, "我這次沒有改動你的行程，請稍後再試。", type(exc).__name__


def _handle_calendar_pending_confirmation(
    ctx: AgentTurnContext, pending: dict[str, Any], run_id: str, trace: dict[str, Any],
    on_progress: ProgressCallback | None,
) -> AgentResult | None:
    choice = confirmation_choice(ctx.message)
    if choice == "none":
        return None
    created_at = float(pending.get("created_at", 0) or 0)
    confirmation_id = str(pending.get("confirmation_id") or f"legacy-{int(created_at * 1000)}")
    base_query = {"user_id": ctx.user_id, "agentic_pending_confirmation.created_at": created_at}
    if choice == "cancel":
        profiles_coll.update_one(base_query, {"$unset": {"agentic_pending_confirmation": ""}})
        trace["confirmation"] = "cancelled"
        return AgentResult(handled=True, reply="好，這次先不更動行程。", conversation_intent="calendar_confirmation", agent_run_id=run_id, agent_mode="v2", profile_write_allowed=False, profile_write_reason="calendar_action")
    claimed = profiles_coll.update_one(
        {**base_query, "$or": [
            {"agentic_pending_confirmation.status": "pending"},
            {"agentic_pending_confirmation.status": {"$exists": False}},
        ]},
        {"$set": {"agentic_pending_confirmation.status": "executing"}},
    )
    if not getattr(claimed, "modified_count", 0):
        return AgentResult(handled=True, reply="這個行程確認正在處理，我不會重複送出。", conversation_intent="calendar_confirmation", agent_run_id=run_id, agent_mode="v2", profile_write_allowed=False, profile_write_reason="calendar_action")
    spec = get_tool_spec(str(pending.get("action") or ""))
    if spec:
        _emit_progress(on_progress, "tool_started", trace=trace, agent_run_id=run_id, step_id="confirmation:0", text=spec.progress_text)
    ok, reply, code = _execute_calendar_pending(ctx, pending, confirmation_id)
    if spec:
        _emit_progress(on_progress, "tool_finished", trace=trace, agent_run_id=run_id, step_id="confirmation:0", outcome="ok" if ok else "error")
    profiles_coll.update_one(base_query, {"$unset": {"agentic_pending_confirmation": ""}})
    trace["confirmation"] = "executed" if ok else "invalidated"
    trace["tool_results"].append({"tool": pending.get("action"), "ok": ok, "code": code})
    _clear_action_draft(ctx.user_id)
    return AgentResult(handled=True, reply=reply, conversation_intent="calendar_action", agent_run_id=run_id, agent_mode="v2", profile_write_allowed=False, profile_write_reason="calendar_action")


def _handle_pending_confirmation(
    ctx: AgentTurnContext, turn: Any, run_id: str, trace: dict[str, Any],
    on_progress: ProgressCallback | None = None,
) -> AgentResult | None:
    """Execute the small explicit confirmation protocol before asking the planner."""
    pending = turn.pending_confirmation or {}
    action = pending.get("action", pending.get("tool"))
    if action in _CALENDAR_WRITE_ACTIONS:
        return _handle_calendar_pending_confirmation(ctx, pending, run_id, trace, on_progress)
    if action != "match.start_search":
        return None
    choice = confirmation_choice(turn.message)
    if choice == "none":
        return None
    created_at = float(pending.get("created_at", 0) or 0)
    confirmation_id = str(pending.get("confirmation_id") or f"legacy-{int(created_at * 1000)}")
    base_query = {"user_id": ctx.user_id, "agentic_pending_confirmation.created_at": created_at}
    if choice == "cancel":
        profiles_coll.update_one(base_query, {"$unset": {"agentic_pending_confirmation": ""}})
        if pending.get("source") == "opportunity_guidance":
            profiles_coll.update_one(
                {"user_id": ctx.user_id},
                {"$set": {"match_guidance": record_guidance_declined(ctx.user_id, pending.get("guidance_fingerprint"))}},
            )
        trace["confirmation"] = "cancelled"
        return AgentResult(handled=True, reply="好，這次先不找人。", conversation_intent="match_confirmation", agent_run_id=run_id, agent_mode="v2")
    current_assessment = assess_match_opportunity(
        ctx.user_profile or {}, ctx.user_id, explicit_search=True,
    )
    if current_assessment.state == "active_match_blocked":
        profiles_coll.update_one(base_query, {"$unset": {"agentic_pending_confirmation": ""}})
        _record_opportunity_trace(trace, current_assessment)
        trace["confirmation"] = "invalidated_active_match"
        return AgentResult(
            handled=True,
            reply="你的配對狀態已經有新進展，我先取消這個舊搜尋確認，不會重複開新搜尋。",
            conversation_intent="match_status",
            agent_run_id=run_id,
            agent_mode="v2",
            match_readiness_state="active_match_blocked",
        )
    claim_query = {**base_query, "$or": [
        {"agentic_pending_confirmation.status": "pending"},
        {"agentic_pending_confirmation.status": {"$exists": False}},
    ]}
    claimed = profiles_coll.update_one(claim_query, {"$set": {
        "agentic_pending_confirmation.status": "executing",
        "agentic_pending_confirmation.confirmation_id": confirmation_id,
    }})
    if not getattr(claimed, "modified_count", 0):
        trace["confirmation"] = "already_executing"
        return AgentResult(handled=True, reply="這個確認正在處理，我不會重複送出。", conversation_intent="match_confirmation", agent_run_id=run_id, agent_mode="v2")
    search_spec = get_tool_spec("match.start_search")
    step_id = "confirmation:0"
    if search_spec is not None:
        _emit_progress(on_progress, "tool_started", trace=trace, agent_run_id=run_id, step_id=step_id, text=search_spec.progress_text)
    try:
        ok, reply, code = _start_search(ctx, run_id, 0, idempotency_key=f"confirmation:{confirmation_id}")
    except Exception:
        if search_spec is not None:
            _emit_progress(on_progress, "tool_finished", trace=trace, agent_run_id=run_id, step_id=step_id, outcome="error")
        raise
    if search_spec is not None:
        _emit_progress(on_progress, "tool_finished", trace=trace, agent_run_id=run_id, step_id=step_id, outcome="ok" if ok else "error")
    profiles_coll.update_one(
        {**base_query, "agentic_pending_confirmation.confirmation_id": confirmation_id},
        {"$unset": {"agentic_pending_confirmation": ""}},
    )
    trace["confirmation"] = "executed"
    trace["tool_results"].append({"tool": "match.start_search", "ok": ok, "code": code})
    return AgentResult(handled=True, reply=reply, conversation_intent="agentic_match", agent_run_id=run_id, agent_mode="v2")


def _decide_active_proposal(
    ctx: AgentTurnContext, turn: Any, run_id: str, index: int, arguments: dict[str, Any],
) -> tuple[bool, str, str | None]:
    proposal = turn.active_proposal or {}
    decision = str(arguments.get("decision") or "")
    if not proposal.get("user_can_decide") or not decision:
        return False, "我需要你對目前這張提案明確表示有興趣或婉拒。", "decision_not_actionable"
    try:
        outcome = decide_active_proposal(
            user_id=ctx.user_id,
            decision=decision,
            expected_revision=int(proposal.get("proposal_revision", 0)),
            idempotency_key=f"{run_id}:{index}",
        )
        if outcome.get("stale"):
            latest = str(outcome.get("current_status") or "")
            reply = {
                "accepted": "這張提案剛剛已更新：你們已經互相接受，聊天室也已開啟。",
                "declined": "這張提案剛剛已更新為婉拒，我沒有覆寫最新結果。",
                "pending": "這張提案剛剛已更新，現在正在等待對方回覆。",
                "draft": "這張提案剛剛已更新，請以最新提案狀態為準。",
            }.get(latest, "這張提案剛剛已更新，我沒有覆寫最新結果。")
            return True, reply, "stale_revision"
        if outcome.get("status") != "success":
            return False, "我現在不能安全地更新這張提案。", str(outcome.get("status") or "decision_failed")
        return True, "好，我已更新這張牽線提案。" if decision == "interested" else "好，這張提案已替你婉拒。", None
    except Exception as exc:
        return False, "我現在不能安全地更新這張提案。", type(exc).__name__


_WRITE_EXECUTORS = {
    "decide_active_proposal": _decide_active_proposal,
}


def _execute_write_tool(
    spec: Any, ctx: AgentTurnContext, turn: Any, run_id: str, index: int, arguments: dict[str, Any],
) -> tuple[bool, str, str | None]:
    """Resolve write behavior by registry executor key, never planner tool name."""
    if spec.requires_confirmation:
        return False, "我會先取得你的確認，再開始找人。", "confirmation_required"
    executor = _WRITE_EXECUTORS.get(spec.executor_key)
    if executor is None:
        return False, "我現在不能安全地處理這個操作。", "write_executor_not_registered"
    return executor(ctx, turn, run_id, index, arguments)


def run_public_agent_turn(
    ctx: AgentTurnContext, *, mode: str = "on", on_progress: ProgressCallback | None = None,
) -> AgentResult:
    """Run V2 only. Failure is a handled, no-tool response, never legacy routing."""
    # Never trust a client-provided mention binding, including callers that do
    # not pass through the HTTP adapter (tests, future app clients, jobs).
    mentioned_ids, mention_overflow = validated_mentioned_contact_ids(ctx.user_id, ctx.mentioned_ids)
    ctx = ctx.model_copy(update={
        "mentioned_ids": mentioned_ids,
        "mention_overflow": bool(ctx.mention_overflow or mention_overflow),
    })
    run_id, started = uuid.uuid4().hex, time.perf_counter()
    trace: dict[str, Any] = {
        "context_version": "v2", "visible_tools": [], "planner_decisions": [],
        "guard_results": [], "tool_results": [], "event_sequence": [],
        "tool_cache_hits": [], "composer_outcome": {
            "reason": "not_used", "observation_count": 0, "result_code": "not_used",
        },
        "public_progress_result_codes": [],
        "capability_manifest_version": CAPABILITY_MANIFEST_VERSION,
        "opportunity_state": "not_evaluated",
        "opportunity_reason_codes": [],
        "guidance_shown": False,
        "clarification": "none",
        "profile_input": "casual",
        "mentioned_contact_count": 0,
        "context_ms": 0,
        "model_ms": [],
        "tool_ms": [],
    }
    observations: list[dict] = []
    try:
        _emit_progress(on_progress, "run_started", trace=trace, agent_run_id=run_id)
        context_started = time.perf_counter()
        clock = build_turn_clock(ctx.message)
        trace["clock"] = {"timezone": clock.timezone, "local_date": clock.local_date, "local_time": clock.local_time}
        turn = build_agent_turn_context_v2(ctx, clock=clock)
        trace["context_ms"] = round((time.perf_counter() - context_started) * 1000)
        trace["mentioned_contact_count"] = len(turn.mentioned_contacts)
        result: AgentResult | None = None
        pending_result = _handle_pending_confirmation(ctx, turn, run_id, trace, on_progress)
        if pending_result:
            result = pending_result
        else:
            visible = tool_policy_for_turn(turn)
            trace["visible_tools"] = sorted(visible)
            side_effects = 0
            seen: set[tuple[str, str]] = set()
            for index in range(MAX_STEPS):
                model_started = time.perf_counter()
                decision = plan_turn_v2(turn, visible, observations)
                trace["model_ms"].append(round((time.perf_counter() - model_started) * 1000))
                if not decision:
                    result = AgentResult(
                        handled=True,
                        reply=_compose_final_reply(turn, observations, trace, "planner_invalid"),
                        agent_run_id=run_id,
                        agent_mode="v2",
                        fallback_reason="planner_invalid",
                    )
                    break
                trace["planner_decisions"].append({"kind": decision.kind.value, "tool_name": decision.tool_name, "confidence": decision.confidence})
                intent_name = str(getattr(getattr(decision, "intent", None), "value", "") or "")
                # A social opening is non-mutating apart from storing its own
                # pending confirmation.  Explicit search confirmations must
                # pass the deterministic guard below before any state write.
                if decision.kind == DecisionKind.FINAL:
                    if decision.recent_context_followup == "ask_activity":
                        _open_recent_context_draft(ctx)
                        trace["clarification"] = "recent_context"
                        result = AgentResult(
                            handled=True,
                            reply=_compose_clarification(turn, observations, trace, topic="recent_context"),
                            conversation_intent="recent_context_clarification",
                            agent_run_id=run_id, agent_mode="v2",
                            profile_write_allowed=False, profile_write_reason="recent_context_prompt",
                        )
                        break
                    opportunity_result = _handle_match_opportunity(ctx, turn, decision, trace, run_id)
                    if opportunity_result is not None:
                        result = opportunity_result
                        break
                has_match_observation = any(item.get("tool") == "match.get_status" for item in observations)
                has_time_observation = any(item.get("tool") == "system.get_current_time" for item in observations)
                has_calendar_observation = any(item.get("tool") == "calendar.list_my_events" for item in observations)
                ok, reason = guard_v2_decision(
                    turn, visible, decision, has_match_observation=has_match_observation,
                    has_time_observation=has_time_observation,
                    has_calendar_observation=has_calendar_observation,
                )
                trace["guard_results"].append(reason)
                if not ok:
                    if reason in {"match_status_requires_read", "time_requires_read", "calendar_target_requires_read"}:
                        required_tool = {
                            "match_status_requires_read": "match.get_status",
                            "time_requires_read": "system.get_current_time",
                            "calendar_target_requires_read": "calendar.list_my_events",
                        }[reason]
                        required_spec = get_tool_spec(required_tool)
                        if required_spec is None:
                            result = AgentResult(
                                handled=True,
                                reply=_compose_clarification(
                                    turn, observations, trace,
                                    topic=_clarification_topic(decision, tool_name=required_tool),
                                ),
                                agent_run_id=run_id, agent_mode="v2",
                                fallback_reason="guard:tool_not_registered",
                            )
                            break
                        required_arguments = executor_arguments_for_turn(required_spec, ctx.mentioned_ids)
                        step_id = f"{index}:required"
                        _emit_progress(on_progress, "tool_started", trace=trace, agent_run_id=run_id, step_id=step_id, text=required_spec.progress_text)
                        try:
                            tool_started = time.perf_counter()
                            tool_result = execute_tool(ToolCall(name=required_tool, arguments=required_arguments), ctx, clock=turn.clock)
                            trace["tool_ms"].append(round((time.perf_counter() - tool_started) * 1000))
                        except Exception:
                            _emit_progress(on_progress, "tool_finished", trace=trace, agent_run_id=run_id, step_id=step_id, outcome="error")
                            raise
                        _emit_progress(on_progress, "tool_finished", trace=trace, agent_run_id=run_id, step_id=step_id, outcome="ok" if tool_result.ok else "error")
                        trace["tool_results"].append({"tool": required_tool, "ok": tool_result.ok, "code": tool_result.error_code})
                        if tool_result.ok:
                            seen.add(tool_call_key(required_spec, required_arguments))
                            observations.append({"tool": required_tool, "result": tool_result.data})
                            continue
                        result = AgentResult(
                            handled=True,
                            reply=_compose_clarification(
                                turn, observations, trace,
                                topic=_clarification_topic(
                                    decision, tool_name=required_tool,
                                    error_code=tool_result.error_code,
                                ),
                            ),
                            agent_run_id=run_id, agent_mode="v2",
                            fallback_reason=f"tool:{tool_result.error_code or 'read_failed'}",
                        )
                        break
                    if (
                        reason == "model_arguments_not_allowed"
                        and str(getattr(getattr(decision, "intent", None), "value", "")) == "calendar_action"
                    ):
                        _save_action_draft(ctx, turn, decision)
                        trace["clarification"] = "calendar_action"
                        result = AgentResult(
                            handled=True,
                            reply=_compose_clarification(turn, observations, trace, topic="calendar_action"),
                            conversation_intent="calendar_action", agent_run_id=run_id, agent_mode="v2",
                            profile_write_allowed=False, profile_write_reason="calendar_action",
                        )
                        break
                    result = AgentResult(
                        handled=True,
                        reply=_compose_clarification(
                            turn, observations, trace,
                            topic=_clarification_topic(decision),
                        ),
                        agent_run_id=run_id, agent_mode="v2",
                        fallback_reason=f"guard:{reason}",
                    )
                    break
                if decision.kind == DecisionKind.CONFIRMATION:
                    if decision.tool_name in _CALENDAR_WRITE_ACTIONS:
                        result = _prepare_calendar_confirmation(ctx, turn, decision, trace, run_id)
                    else:
                        result = _handle_match_opportunity(ctx, turn, decision, trace, run_id)
                    if result is None:
                        result = AgentResult(
                            handled=True,
                            reply=_compose_clarification(
                                turn, observations, trace,
                                topic=_clarification_topic(decision),
                            ),
                            agent_run_id=run_id,
                            agent_mode="v2",
                            fallback_reason="guard:confirmation_not_grounded",
                        )
                    break
                if decision.kind == DecisionKind.FINAL:
                    # The terminal planner receives the same verified
                    # observations as the old Composer. Its validated reply is
                    # therefore the normal low-latency path; Composer remains
                    # the provider/fact-safety fallback.
                    if intent_name == "calendar_action":
                        _save_action_draft(ctx, turn, decision)
                        trace["clarification"] = "calendar_action"
                        result = AgentResult(
                            handled=True,
                            reply=_compose_clarification(turn, observations, trace, topic="calendar_action"),
                            conversation_intent="calendar_action", agent_run_id=run_id, agent_mode="v2",
                            profile_write_allowed=False, profile_write_reason="calendar_action",
                        )
                        break
                    result = AgentResult(
                        handled=True, reply=_final_reply_for_decision(turn, decision, observations, trace, "planner_final"),
                        agent_run_id=run_id, agent_mode="v2",
                        profile_write_allowed=intent_name not in {"calendar", "match_action", "match_status", "relationship", "time"},
                        profile_write_reason=("casual" if intent_name in {"", "chat", "memory", "unclear"} else intent_name),
                    )
                    break
                spec = get_tool_spec(decision.tool_name)
                if spec is None:
                    result = AgentResult(
                        handled=True,
                        reply=_compose_clarification(
                            turn, observations, trace,
                            topic=_clarification_topic(decision),
                        ),
                        agent_run_id=run_id, agent_mode="v2",
                        fallback_reason="guard:tool_not_registered",
                    )
                    break
                safe_arguments = executor_arguments_for_turn(spec, ctx.mentioned_ids, decision.arguments)
                if spec.name == "web.extract" and not _web_extract_urls_allowed(
                    ctx, observations, list(safe_arguments.get("urls") or []),
                ):
                    result = AgentResult(
                        handled=True,
                        reply=_compose_clarification(turn, observations, trace, topic="request"),
                        agent_run_id=run_id, agent_mode="v2",
                        fallback_reason="guard:web_extract_url_not_bound",
                    )
                    break
                key = tool_call_key(spec, safe_arguments)
                if key in seen:
                    # A repeated read has no new information. Reuse the first
                    # observation and finish the turn without leaking a guard
                    # message into the conversation.
                    trace["guard_results"].append("duplicate_observation_reused")
                    trace["tool_cache_hits"].append(spec.name)
                    result = AgentResult(
                        handled=True,
                        reply=_compose_final_reply(turn, observations, trace, "duplicate_observation_reused"),
                        agent_run_id=run_id,
                        agent_mode="v2",
                        fallback_reason="duplicate_observation_reused",
                    )
                    break
                seen.add(key)
                if spec.risk is ToolRisk.WRITE:
                    if side_effects:
                        result = AgentResult(handled=True, reply="這回合已經執行過一項操作，我先不再變更其他狀態。", agent_run_id=run_id, agent_mode="v2")
                        break
                    side_effects += 1
                    step_id = f"{index}:write"
                    _emit_progress(on_progress, "tool_started", trace=trace, agent_run_id=run_id, step_id=step_id, text=spec.progress_text)
                    try:
                        ok, reply, code = _execute_write_tool(spec, ctx, turn, run_id, index, safe_arguments)
                    except Exception:
                        _emit_progress(on_progress, "tool_finished", trace=trace, agent_run_id=run_id, step_id=step_id, outcome="error")
                        raise
                    _emit_progress(on_progress, "tool_finished", trace=trace, agent_run_id=run_id, step_id=step_id, outcome="ok" if ok else "error")
                    trace["tool_results"].append({"tool": decision.tool_name, "ok": ok, "code": code})
                    result = AgentResult(handled=True, reply=reply, conversation_intent="agentic_match", agent_run_id=run_id, agent_mode="v2")
                    break
                step_id = f"{index}:read"
                _emit_progress(on_progress, "tool_started", trace=trace, agent_run_id=run_id, step_id=step_id, text=spec.progress_text)
                try:
                    tool_started = time.perf_counter()
                    tool_result = execute_tool(ToolCall(name=decision.tool_name or "", arguments=safe_arguments), ctx, clock=turn.clock)
                    trace["tool_ms"].append(round((time.perf_counter() - tool_started) * 1000))
                except Exception:
                    _emit_progress(on_progress, "tool_finished", trace=trace, agent_run_id=run_id, step_id=step_id, outcome="error")
                    raise
                _emit_progress(on_progress, "tool_finished", trace=trace, agent_run_id=run_id, step_id=step_id, outcome="ok" if tool_result.ok else "error")
                trace["tool_results"].append({"tool": decision.tool_name, "ok": tool_result.ok, "code": tool_result.error_code})
                if not tool_result.ok:
                    result = AgentResult(
                        handled=True,
                        reply=_compose_clarification(
                            turn, observations, trace,
                            topic=_clarification_topic(
                                decision, tool_name=decision.tool_name,
                                error_code=tool_result.error_code,
                            ),
                        ),
                        agent_run_id=run_id, agent_mode="v2",
                        fallback_reason=f"tool:{tool_result.error_code or 'read_failed'}",
                    )
                    break
                observations.append({"tool": decision.tool_name, "result": tool_result.data})
                # Read quota exhaustion still composes from every verified
                # observation; it must not downgrade to a last-tool template.
                if index == MAX_STEPS - 1:
                    result = AgentResult(
                        handled=True, reply=_compose_final_reply(turn, observations, trace, "read_limit_composed"),
                        agent_run_id=run_id, agent_mode="v2",
                        fallback_reason="read_limit_composed",
                    )
            if result is None:
                result = AgentResult(handled=True, reply="我目前不會執行任何操作。", agent_run_id=run_id, agent_mode="v2", fallback_reason="loop_exhausted")
    except Exception as exc:
        trace["exception"] = type(exc).__name__
        result = AgentResult(handled=True, reply="我現在沒辦法安全地處理這件事，請稍後再試。", agent_run_id=run_id, agent_mode="v2", fallback_reason=type(exc).__name__)
    result.sources = _public_sources(observations)
    trace["latency_ms"] = round((time.perf_counter() - started) * 1000)
    trace["result"] = {
        "handled": result.handled,
        "conversation_intent": result.conversation_intent,
        "fallback_reason": result.fallback_reason,
    }
    trace["profile_input"] = result.profile_write_reason
    trace["event_sequence"].append("final")
    _save_trace(run_id, ctx, trace)
    return result
