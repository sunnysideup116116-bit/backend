"""V3 write executors: the only path that performs confirmed side effects.

Every write goes through a canonical domain service.  Planner/sub-agent
arguments never contain IDs or revisions; executors inject them from the
turn context and canonical state.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from database import db
from services.match_action_service import decide_active_proposal, start_match_search
from services.assessment_session_service import start_assessment_session
from services.ayue_agent.match_opportunity import (
    assess_match_opportunity, missing_basis_question,
)
from services.calendar_service import (
    _parse_local_interval, as_utc, calendar_access_enabled, conflicts_for_viewer,
    get_timezone, normalize_form, resolve_owned_event, resolve_owned_events_for_cancel,
)

TOOL_CALLS = db["agent_tool_calls"]


def _idempotency_key(confirmation_id: str | None, run_id: str, index: int, suffix: str = "") -> str:
    base = f"confirmation:{confirmation_id}" if confirmation_id else f"{run_id}:{index}"
    return f"{base}:{suffix}" if suffix else base


def _claim_once(key: str) -> bool:
    """Return True if this key has not been executed before (idempotency)."""
    prior = TOOL_CALLS.find_one_and_update(
        {"idempotency_key": key},
        {"$setOnInsert": {"idempotency_key": key, "created_at": time.time(), "state": "running"}},
        upsert=True,
    )
    return prior is None


def _finish(key: str, status: str, result: dict[str, Any]) -> None:
    try:
        TOOL_CALLS.update_one({"idempotency_key": key}, {"$set": {"state": status, "result": result}})
    except Exception:
        pass


def _start_search(ctx: Any, run_id: str, index: int, *, confirmation_id: str | None) -> tuple[bool, str, str | None]:
    key = _idempotency_key(confirmation_id, run_id, index)
    if not _claim_once(key):
        return True, "我已經處理過這次搜尋。", None
    try:
        result = start_match_search(
            ctx.user_id, source="agent_v3", force_new=True, idempotency_key=key,
        )
        status = result.get("status", "failed")
        reply = {
            "queued": "好，我開始幫你找，通常約需要 1–3 分鐘。你可以先繼續跟我聊，找到後我會回來。",
            "already_queued": "這次搜尋已經排進去了，通常約需要 1–3 分鐘；你可以先繼續跟我聊。",
            "already_active": "你目前還有一張進行中的提案，我先不重複開新搜尋。",
            "already_searching": "我正在幫你找，先不用重複送出。",
            "no_candidates": "這輪暫時沒有合適的新對象。",
        }.get(status, "這次搜尋沒有成功啟動，我沒有把它當作已完成。")
        _finish(key, "done", {"status": status, "reply": reply})
        return status in {"queued", "already_queued", "already_active", "already_searching", "no_candidates"}, reply, None
    except Exception as exc:
        return False, "我現在不能安全地開始搜尋，請稍後再試。", type(exc).__name__


def _decide_active_proposal(ctx: Any, turn: Any, run_id: str, index: int, arguments: dict[str, Any]) -> tuple[bool, str, str | None]:
    proposal = turn.active_proposal or {}
    decision = str(arguments.get("decision") or "")
    if not proposal.get("user_can_decide") or not decision:
        return False, "我需要你對目前這張提案明確表示有興趣或婉拒。", "decision_not_actionable"
    try:
        outcome = decide_active_proposal(
            user_id=ctx.user_id,
            decision=decision,
            expected_revision=int(proposal.get("proposal_revision", 0)),
            idempotency_key=_idempotency_key(None, run_id, index),
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


def _start_assessment(ctx: Any, arguments: dict[str, Any], *, confirmation_id: str | None) -> tuple[bool, str, str | None]:
    kind = {"basic": "big_five", "deep": "deep_profile"}.get(str(arguments.get("kind") or ""))
    if kind is None:
        return False, "我沒有找到要開始的探索類型，你可以告訴我想做基本性格還是深層探索。", "assessment_unknown_kind"
    key = _idempotency_key(confirmation_id, "assessment", 0, suffix=kind)
    if not _claim_once(key):
        return True, "這個探索確認正在處理，我不會重複開始。", None
    try:
        outcome = start_assessment_session(ctx.user_id, kind, idempotency_key=key)
    except Exception as exc:
        return False, "剛剛沒有成功開始，我沒有改動原本的資料。你想再試一次時跟我說。", type(exc).__name__
    ok = outcome.get("status") in {"started", "already_started"}
    _finish(key, "done", {"status": outcome.get("status")})
    return ok, str(outcome.get("reply") or "我們可以從一個輕鬆的問題開始。"), None if ok else str(outcome.get("status") or "assessment_start_failed")


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
    return f"{start.month}/{start.day} {start:%H:%M}–{end:%H:%M} {title}"


def _calendar_execute(ctx: Any, tool_name: str, arguments: dict[str, Any], payload: dict[str, Any] | None, *, confirmation_id: str) -> tuple[bool, str, str | None]:
    from fastapi import HTTPException
    from services.calendar_service import (
        cancel_event, cancel_targets_are_current, create_personal_event,
        update_personal_event,
    )
    from services.date_coordination_service import cancel_coordination_or_event, request_reschedule

    payload = payload or {}
    key = f"calendar-confirmation:{confirmation_id}"
    try:
        batch = payload.get("batch")
        if isinstance(batch, list) and batch:
            # Batch: one confirmation may carry multiple calendar writes
            # (create/update/cancel mixed). Each item is {tool, arguments, data}.
            labels: list[str] = []
            failed = 0
            for index, item in enumerate(batch):
                item_tool = str(item.get("tool") or tool_name)
                item_args = item.get("arguments") or {}
                item_data = item.get("data") or {}
                item_key = f"{key}:{index}"
                try:
                    if item_tool == "calendar.create_my_event":
                        event = create_personal_event(ctx.user_id, item_args, agent_action_key=item_key)
                        labels.append(_calendar_event_label(event))
                        continue
                    event_id = str(item_data.get("event_id") or "")
                    revision = int(item_data.get("event_revision", 0) or 0)
                    is_shared = item_data.get("event_source_type") == "date"
                    other_id = str(item_data.get("event_other_id") or "")
                    if item_tool == "calendar.update_my_event":
                        if is_shared:
                            coordination, event = request_reschedule(
                                ctx.user_id, other_id, event_id,
                                dict(item_data.get("proposed_form") or {}),
                                expected_revision=revision, idempotency_key=item_key,
                            )
                            form = coordination.get("form") or {}
                            proposed_label = (
                                f"{str(form.get('date') or '')[5:].replace('-', '/')} "
                                f"{form.get('start_time', '')}–{form.get('end_time', '')} "
                                f"{form.get('activity') or '共同約會'}"
                            ).strip()
                            labels.append(f"改期：{proposed_label}")
                            continue
                        changes = {k: v for k, v in item_args.items() if k != "event_hint" and v is not None}
                        event = update_personal_event(
                            ctx.user_id, event_id, changes,
                            expected_revision=revision, agent_action_key=item_key,
                        )
                        labels.append(_calendar_event_label(event))
                        continue
                    if item_tool == "calendar.cancel_my_event":
                        if is_shared:
                            coordination = cancel_coordination_or_event(
                                ctx.user_id, other_id, str(item_data.get("coordination_id") or ""),
                                expected_revision=revision, idempotency_key=item_key,
                            )
                            title = str((coordination.get("form") or {}).get("activity") or "共同約會")
                            labels.append(f"取消共同約會「{title}」")
                            continue
                        event = cancel_event(
                            ctx.user_id, event_id, personal_only=True,
                            expected_revision=revision, agent_action_key=item_key,
                        )
                        labels.append(f"取消「{_calendar_event_label(event)}」")
                        continue
                    failed += 1
                except Exception:
                    failed += 1
            if not labels:
                return False, "這些行程現在無法變更；我沒有確認到任何變更結果。", "calendar_write_failed"
            reply = "已處理：" + "、".join(labels) + "。"
            if failed:
                reply += f"另有 {failed} 筆沒有變更，請再查看後重試。"
            return True, reply, "partial" if failed else None
        if tool_name == "calendar.create_my_event":
            event = create_personal_event(ctx.user_id, arguments, agent_action_key=key)
            return True, f"已加入行程：{_calendar_event_label(event)}。", None
        event_id = str(payload.get("event_id") or "")
        revision = int(payload.get("event_revision", 0) or 0)
        is_shared = payload.get("event_source_type") == "date"
        other_id = str(payload.get("event_other_id") or "")
        if tool_name == "calendar.update_my_event":
            if is_shared:
                coordination, event = request_reschedule(
                    ctx.user_id, other_id, event_id,
                    dict(payload.get("proposed_form") or {}),
                    expected_revision=revision, idempotency_key=key,
                )
                form = coordination.get("form") or {}
                proposed_label = (
                    f"{str(form.get('date') or '')[5:].replace('-', '/')} "
                    f"{form.get('start_time', '')}–{form.get('end_time', '')} "
                    f"{form.get('activity') or '共同約會'}"
                ).strip()
                return True, f"已提出改期：{proposed_label}。對方已收到通知，確認後才會正式變更。", None
            changes = {k: v for k, v in arguments.items() if k != "event_hint" and v is not None}
            event = update_personal_event(
                ctx.user_id, event_id, changes, expected_revision=revision, agent_action_key=key,
            )
            return True, f"已更新行程：{_calendar_event_label(event)}。", None
        if tool_name == "calendar.cancel_my_event":
            if is_shared:
                coordination = cancel_coordination_or_event(
                    ctx.user_id, other_id, str(payload.get("coordination_id") or ""),
                    expected_revision=revision, idempotency_key=key,
                )
                title = str((coordination.get("form") or {}).get("activity") or "共同約會")
                return True, f"已取消共同約會「{title}」，對方已收到通知，雙方行事曆也已同步。", None
            event = cancel_event(
                ctx.user_id, event_id, personal_only=True, expected_revision=revision, agent_action_key=key,
            )
            return True, f"已取消行程：{_calendar_event_label(event)}。", None
        if tool_name == "calendar.cancel_my_events":
            targets = list(payload.get("targets") or [])
            if not cancel_targets_are_current(ctx.user_id, targets):
                return False, "其中一筆行程剛剛有變動，我沒有刪除任何行程。請重新確認。", "stale_revision"
            completed: list[str] = []
            failed = 0
            for index, target in enumerate(targets):
                target_key = f"{key}:{index}"
                try:
                    if target.get("event_source_type") == "date":
                        cancel_coordination_or_event(
                            ctx.user_id, str(target.get("event_other_id") or ""),
                            str(target.get("coordination_id") or ""),
                            expected_revision=int(target.get("event_revision", 0) or 0),
                            idempotency_key=target_key,
                        )
                    else:
                        cancel_event(
                            ctx.user_id, str(target.get("event_id") or ""),
                            personal_only=True,
                            expected_revision=int(target.get("event_revision", 0) or 0),
                            agent_action_key=target_key,
                        )
                    completed.append(str(target.get("safe_label") or "這筆行程"))
                except Exception:
                    failed += 1
            if not completed:
                return False, "這些行程現在無法變更；我沒有確認到任何刪除結果。", "calendar_write_failed"
            reply = f"已取消 {len(completed)} 筆行程：" + "、".join(f"「{label}」" for label in completed) + "。"
            if failed:
                reply += f"另有 {failed} 筆沒有刪除，請再查看後重試。"
            return True, reply, "partial" if failed else None
        return False, "這個行程確認已失效，請重新告訴我你想怎麼安排。", "unknown_calendar_action"
    except HTTPException as exc:
        if exc.status_code == 409:
            return False, "這筆行程剛剛有變動，我沒有覆寫它。請告訴我最新想怎麼改。", "stale_revision"
        return False, "這筆行程現在無法變更；你可以再告訴我想處理哪一筆嗎？", "calendar_write_failed"
    except Exception as exc:
        return False, "我這次沒有改動你的行程，請稍後再試。", type(exc).__name__


_WRITE_EXECUTORS = {
    "match.start_search": lambda ctx, turn, run_id, index, args, cid: _start_search(ctx, run_id, index, confirmation_id=cid),
    "match.decide_active_proposal": lambda ctx, turn, run_id, index, args, cid: _decide_active_proposal(ctx, turn, run_id, index, args),
    "profile.start_assessment": lambda ctx, turn, run_id, index, args, cid: _start_assessment(ctx, args, confirmation_id=cid),
}


_CALENDAR_WRITE_ACTIONS = {
    "calendar.create_my_event", "calendar.update_my_event",
    "calendar.cancel_my_event", "calendar.cancel_my_events",
}


def _calendar_pending_target(event: dict, user_id: str) -> dict[str, Any]:
    return {
        "event_id": str(event.get("event_id") or ""),
        "event_revision": int(event.get("revision", 1)),
        "event_source_type": str(event.get("source_type") or "personal"),
        "event_other_id": (
            next((p for p in (event.get("participants") or []) if p != user_id), None)
            if event.get("source_type") == "date" else None
        ),
        "coordination_id": event.get("coordination_id"),
        "safe_label": _calendar_event_label(event),
    }


def _calendar_preview_and_payload(ctx: Any, tool_name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    from fastapi import HTTPException

    if not calendar_access_enabled(ctx.user_id):
        return None, "我目前不能存取你的行事曆；你可以先到日曆設定確認是否已授權。"
    event: dict | None = None
    targets: list[dict[str, Any]] = []
    conflicts: list[dict] = []
    proposed: dict | None = None
    try:
        if tool_name == "calendar.create_my_event":
            form = normalize_form(arguments)
            start_at, end_at, _ = _parse_local_interval(form)
            arguments = {**arguments, **form}
            conflicts = conflicts_for_viewer(ctx.user_id, [ctx.user_id], start_at, end_at)
            preview = f"要新增 {form['date'][5:].replace('-', '/')} {form['start_time']}–{form['end_time']}「{form['title']}」嗎？"
        elif tool_name == "calendar.cancel_my_events":
            events, resolution = resolve_owned_events_for_cancel(
                ctx.user_id,
                mode=str(arguments.get("mode") or ""),
                event_hints=list(arguments.get("event_hints") or []),
            )
            if resolution == "ambiguous":
                return None, "有一筆行程對應到不只一個結果。你可以補上日期或完整名稱嗎？"
            if resolution == "not_found":
                return None, "我找不到其中一筆自己的行程。你可以補上日期或名稱嗎？"
            if resolution == "too_many":
                return None, "接下來的行程超過 10 筆；請先指定想取消哪些日期。"
            if resolution or not events:
                return None, "我還需要更明確的行程名稱或日期，才能一次取消多筆。"
            targets = [_calendar_pending_target(item, ctx.user_id) for item in events]
            labels = "、".join(f"「{target['safe_label']}」" for target in targets)
            preview = f"要取消這 {len(targets)} 筆行程嗎：{labels}？"
            if any(target["event_source_type"] == "date" for target in targets):
                preview += " 其中共同約會會同步雙方行事曆並通知對方。"
        else:
            event, resolution = resolve_owned_event(ctx.user_id, arguments.get("event_hint", ""))
            if resolution == "ambiguous":
                return None, "我找到不只一筆符合的行程。你可以補上日期或完整名稱嗎？"
            if not event:
                return None, "我找不到這筆自己的行程。你可以補上日期或名稱嗎？"
            is_shared = event.get("source_type") == "date"
            if tool_name == "calendar.cancel_my_event":
                targets = [_calendar_pending_target(event, ctx.user_id)]
                preview = f"要取消「{_calendar_event_label(event)}」嗎？"
                if is_shared:
                    preview += " 這是共同約會，取消後會同步雙方行事曆並通知對方。"
            else:
                if is_shared and event.get("status") != "confirmed":
                    return None, "這筆共同約會正在等待重新確認；你可以先取消目前改期，或直接取消整筆約會。"
                zone = get_timezone(event.get("timezone") or "Asia/Taipei")
                start = as_utc(event["start_at"]).astimezone(zone)
                end = as_utc(event["end_at"]).astimezone(zone)
                current = {
                    "title": (event.get("activity") if is_shared else event.get("title") or event.get("activity")) or "行程",
                    "date": start.date().isoformat(), "start_time": start.strftime("%H:%M"),
                    "end_time": end.strftime("%H:%M"), "timezone": event.get("timezone") or "Asia/Taipei",
                    "location": event.get("location") or "", "notes": event.get("notes") or "",
                }
                changes = {k: v for k, v in arguments.items() if k != "event_hint" and v is not None}
                if not changes:
                    return None, f"你想把「{_calendar_event_label(event)}」改成什麼呢？"
                if is_shared:
                    shared_changes = dict(changes)
                    if "title" in shared_changes:
                        shared_changes["activity"] = shared_changes.pop("title")
                    proposed = normalize_form({
                        **current, "activity": current["title"],
                        "budget": event.get("budget") or "", **shared_changes,
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
        return None, f"我還需要補齊行程資訊：{exc.detail}。"
    if conflicts:
        preview += f" 這會和你現有的 {len(conflicts)} 筆行程重疊；仍要這樣安排嗎？"
    payload = {
        "action": tool_name,
        "arguments": arguments,
        "data": {
            "event_id": event.get("event_id") if event else None,
            "event_revision": int(event.get("revision", 1)) if event else None,
            "event_source_type": event.get("source_type") if event else None,
            "event_other_id": (
                next((p for p in (event.get("participants") or []) if p != ctx.user_id), None)
                if event and event.get("source_type") == "date" else None
            ),
            "coordination_id": event.get("coordination_id") if event else None,
            "targets": targets,
            "proposed_form": (
                proposed if event and tool_name == "calendar.update_my_event" and event.get("source_type") == "date" else None
            ),
        },
    }
    return payload, preview + " 回覆「確認」才會真的變更。"


def prepare_write_confirmation(
    tool_name: str, arguments: dict[str, Any], ctx: Any, turn: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a proposed write and return (pending_payload, preview_reply).

    Returns (None, error_reply) when the write cannot be confirmed (not ready,
    blocked, ambiguous, missing fields).  The payload carries executor-only
    data (event IDs, revisions, targets) that never reach the planner.
    """
    if tool_name in _CALENDAR_WRITE_ACTIONS:
        return _calendar_preview_and_payload(ctx, tool_name, arguments)
    if tool_name == "match.start_search":
        assessment = assess_match_opportunity(ctx.user_profile or {}, ctx.user_id, explicit_search=True)
        if assessment.state == "not_ready":
            return None, "我想先多了解你的方向，才能幫你找得更準。" + missing_basis_question(assessment)
        if assessment.state == "active_match_blocked":
            return None, "你目前還有一段配對正在進行，我先不重複開新搜尋。"
        return {"action": tool_name, "arguments": {}, "data": {}}, (
            "我會依你的近況、偏好和個性挑選，不會隨機配對。要我現在開始找就回覆「確認」；也可以先補充條件。"
        )
    if tool_name == "profile.start_assessment":
        kind = {"basic": "big_five", "deep": "deep_profile"}.get(str(arguments.get("kind") or ""))
        if kind is None:
            return None, "我還不確定你想重新做哪一種探索類型。"
        from services.assessment_session_service import assessment_label
        return {"action": tool_name, "arguments": dict(arguments), "data": {}}, (
            f"要重新開始{assessment_label(kind)}嗎？新的結果完成前，原本的資料會保留。回覆「確認」就開始，也可以回覆「取消」。"
        )
    return None, "我現在不能安全地處理這個操作。"


def execute_write(
    tool_name: str,
    arguments: dict[str, Any],
    ctx: Any,
    turn: Any,
    run_id: str,
    index: int,
    *,
    confirmation_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[bool, str, str | None]:
    """Execute one confirmed write through its canonical domain service."""
    if tool_name.startswith("calendar."):
        cid = (payload or {}).get("_confirmation_id") or confirmation_id or uuid.uuid4().hex
        return _calendar_execute(ctx, tool_name, arguments, payload, confirmation_id=cid)
    executor = _WRITE_EXECUTORS.get(tool_name)
    if executor is None:
        return False, "我現在不能安全地處理這個操作。", "write_executor_not_registered"
    return executor(ctx, turn, run_id, index, arguments, confirmation_id)
