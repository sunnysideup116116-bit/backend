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


def _decide_active_proposal(
    ctx: Any, turn: Any, run_id: str, index: int,
    arguments: dict[str, Any], payload: dict[str, Any] | None,
) -> tuple[bool, str, str | None]:
    proposal = turn.active_proposal or {}
    decision = str(arguments.get("decision") or "")
    expected_revision = int((payload or {}).get("proposal_revision", 0) or 0)
    if (
        not proposal.get("user_can_decide")
        or not decision
        or expected_revision <= 0
        or int(proposal.get("proposal_revision", 0) or 0) != expected_revision
    ):
        return False, "我需要你對目前這張提案明確表示有興趣或婉拒。", "decision_not_actionable"
    try:
        outcome = decide_active_proposal(
            user_id=ctx.user_id,
            decision=decision,
            expected_revision=expected_revision,
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


def _execute_calendar_mutation_plans(
    ctx: Any,
    plans_payload: list[dict[str, Any]],
    *,
    confirmation_id: str,
) -> tuple[bool, str, str | None]:
    """Execute server-owned Calendar plans sequentially.

    The plan contains canonical IDs/revisions produced by preflight.  This
    function deliberately never calls a natural-language resolver.  A batch
    stops at the first real failure; already committed operations are retained
    because Calendar writes do not provide a distributed transaction.
    """
    from fastapi import HTTPException
    from services.calendar_service import (
        cancel_event, cancel_targets_are_current, create_personal_event,
        update_personal_event,
    )
    from services.date_coordination_service import cancel_coordination_or_event, request_reschedule
    from .calendar_commands import CalendarMutationPlan

    try:
        plans = [CalendarMutationPlan.model_validate(item) for item in plans_payload]
    except Exception:
        return False, "這次行程確認資料已失效，請重新告訴我想怎麼安排。", "calendar_plan_invalid"
    if not plans:
        return False, "這次沒有可執行的行程變更。", "calendar_plan_empty"

    from .calendar_references import remember_recent_mutation

    operation_records: list[dict[str, Any]] = []
    batch_action = (
        getattr(plans[0].action, "value", str(plans[0].action))
        if len(plans) == 1 else "batch"
    )

    def remember_outcome(outcome: str) -> None:
        try:
            remember_recent_mutation(
                ctx.user_id, action=batch_action, outcome=outcome,
                operations=operation_records,
            )
        except Exception:
            # Verification state is advisory; a storage outage must not turn a
            # committed Calendar mutation into a failed response.
            pass

    def append_operation(
        plan: CalendarMutationPlan,
        event: dict[str, Any] | None = None,
        *,
        expected_status: str,
    ) -> None:
        action = getattr(plan.action, "value", str(plan.action))
        form = dict(plan.form or {})
        if event:
            label = _calendar_event_label(event)
            event_id = str(event.get("event_id") or plan.event_id or "")
            revision = int(event.get("revision", plan.expected_revision or 0) or 0)
        else:
            changes = dict(plan.changes or {})
            label = str(
                form.get("title") or form.get("activity")
                or changes.get("title") or changes.get("activity") or "行程"
            )[:180]
            event_id = str(plan.event_id or "")
            revision = int(plan.expected_revision or 0)
        operation_records.append({
            "action": action,
            "event_id": event_id,
            "revision": revision,
            "source_type": str(plan.source_type or "personal"),
            "other_id": str(plan.other_id or ""),
            "coordination_id": str(plan.coordination_id or ""),
            "expected_status": expected_status,
            "safe_label": label,
        })

    # Preserve the existing all-target stale protection for a pure cancellation
    # batch.  Mixed commands still rely on each domain service's CAS at the
    # exact write point.
    if all(plan.action == "cancel" for plan in plans):
        targets = [
            {
                "event_id": plan.event_id,
                "event_revision": plan.expected_revision,
                "event_source_type": plan.source_type,
            }
            for plan in plans
        ]
        if not cancel_targets_are_current(ctx.user_id, targets):
            return False, "其中一筆行程剛剛有變動，我沒有刪除任何行程。請重新確認。", "stale_revision"

    key = f"calendar-confirmation:{confirmation_id}"
    completed: list[str] = []
    for index, plan in enumerate(plans):
        item_key = f"{key}:{index}"
        try:
            if plan.action == "create":
                event = create_personal_event(ctx.user_id, plan.form, agent_action_key=item_key)
                completed.append(_calendar_event_label(event))
                append_operation(
                    plan, event,
                    expected_status=str((event or {}).get("status") or "confirmed"),
                )
                continue

            event_id = str(plan.event_id or "")
            revision = int(plan.expected_revision or 0)
            if plan.action == "update":
                if plan.source_type == "date":
                    coordination, event = request_reschedule(
                        ctx.user_id, str(plan.other_id or ""), event_id,
                        dict(plan.form), expected_revision=revision, idempotency_key=item_key,
                    )
                    form = coordination.get("form") or {}
                    completed.append(
                        f"改期：{str(form.get('date') or '')[5:].replace('-', '/')} "
                        f"{form.get('start_time', '')}–{form.get('end_time', '')} "
                        f"{form.get('activity') or '共同約會'}"
                    )
                else:
                    event = update_personal_event(
                        ctx.user_id, event_id, dict(plan.changes),
                        expected_revision=revision, agent_action_key=item_key,
                    )
                    completed.append(_calendar_event_label(event))
                append_operation(
                    plan, event,
                    expected_status=str((event or {}).get("status") or "confirmed"),
                )
                continue

            event = None
            if plan.source_type == "date":
                coordination = cancel_coordination_or_event(
                    ctx.user_id, str(plan.other_id or ""), str(plan.coordination_id or ""),
                    expected_revision=revision, idempotency_key=item_key,
                )
                title = str((coordination.get("form") or {}).get("activity") or "共同約會")
                completed.append(f"取消共同約會「{title}」")
            else:
                event = cancel_event(
                    ctx.user_id, event_id, personal_only=True,
                    expected_revision=revision, agent_action_key=item_key,
                )
                completed.append(f"取消「{_calendar_event_label(event)}」")
            append_operation(plan, event, expected_status="cancelled")
        except HTTPException as exc:
            code = "stale_revision" if exc.status_code == 409 else "calendar_write_failed"
            if not completed:
                message = (
                    "這筆行程剛剛有變動，我沒有覆寫它。請重新確認。"
                    if code == "stale_revision" else "這筆行程現在無法變更，請重新確認。"
                )
                remember_outcome("failed")
                return False, message, code
            remember_outcome("partial")
            return True, (
                "已處理：" + "、".join(completed) +
                f"。第 {index + 1} 筆沒有完成，後續變更已停止，請重新查看。"
            ), "partial"
        except Exception as exc:
            if not completed:
                remember_outcome("failed")
                return False, "我這筆行程目前沒有成功變更，先沒有把它當作已完成。請直接告訴我想改成哪一天、幾點。", type(exc).__name__
            remember_outcome("partial")
            return True, (
                "已處理：" + "、".join(completed) +
                f"。第 {index + 1} 筆沒有完成，後續變更已停止，請重新查看。"
            ), "partial"

    remember_outcome("success")
    return True, "已處理：" + "、".join(completed) + "。", None


def _calendar_execute(
    ctx: Any,
    tool_name: str,
    arguments: dict[str, Any],
    payload: dict[str, Any] | None,
    *,
    confirmation_id: str,
) -> tuple[bool, str, str | None]:
    """Execute only the typed ``calendar.submit_commands`` confirmation plan."""
    if tool_name != "calendar.submit_commands":
        return False, "這個行程確認已失效，請重新告訴我你想怎麼安排。", "calendar_legacy_tool_disabled"
    payload = payload or {}
    if payload.get("calendar_plan_version") != 1:
        return False, "這次行程確認資料已失效，請重新告訴我想怎麼安排。", "calendar_plan_invalid"
    plans = payload.get("plans")
    if not isinstance(plans, list) or not plans:
        return False, "這次沒有可執行的行程變更。", "calendar_plan_empty"
    return _execute_calendar_mutation_plans(
        ctx, plans, confirmation_id=confirmation_id,
    )


_WRITE_EXECUTORS = {
    "match.start_search": lambda ctx, turn, run_id, index, args, cid, payload: _start_search(ctx, run_id, index, confirmation_id=cid),
    "match.decide_active_proposal": lambda ctx, turn, run_id, index, args, cid, payload: _decide_active_proposal(ctx, turn, run_id, index, args, payload),
    "profile.start_assessment": lambda ctx, turn, run_id, index, args, cid, payload: _start_assessment(ctx, args, confirmation_id=cid),
}


def prepare_write_confirmation(
    tool_name: str, arguments: dict[str, Any], ctx: Any, turn: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a proposed write and return (pending_payload, preview_reply).

    Returns (None, error_reply) when the write cannot be confirmed (not ready,
    blocked, ambiguous, missing fields).  The payload carries executor-only
    data (event IDs, revisions, targets) that never reach the planner.
    """
    if tool_name == "match.start_search":
        assessment = assess_match_opportunity(ctx.user_profile or {}, ctx.user_id, explicit_search=True)
        if assessment.state == "not_ready":
            return None, "我想先多了解你的方向，才能幫你找得更準。" + missing_basis_question(assessment)
        if assessment.state == "active_match_blocked":
            return None, "你目前還有一段配對正在進行，我先不重複開新搜尋。"
        return {"action": tool_name, "arguments": {}, "data": {}}, (
            "我會依你的近況、偏好和個性挑選，不會隨機配對。要我現在開始找就回覆「確認」；也可以先補充條件。"
        )
    if tool_name == "match.decide_active_proposal":
        proposal = turn.active_proposal or {}
        decision = str(arguments.get("decision") or "")
        if not proposal.get("user_can_decide") or decision not in {"interested", "declined"}:
            return None, "目前沒有一張可由你決定的配對提案。"
        revision = int(proposal.get("proposal_revision", 0) or 0)
        if revision <= 0:
            return None, "這張提案的狀態已經更新，請重新查看後再決定。"
        counterparty = str(proposal.get("counterparty") or "對方")
        action_label = "表示有興趣" if decision == "interested" else "婉拒"
        return {
            "action": tool_name,
            "arguments": {"decision": decision},
            "data": {"proposal_revision": revision},
        }, f"要對 {counterparty} 的提案{action_label}嗎？確認後我才會送出。"
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
    cid = (payload or {}).get("_confirmation_id") or confirmation_id
    return executor(ctx, turn, run_id, index, arguments, cid, payload)
