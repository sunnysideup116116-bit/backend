"""Match intent is semantic; state transitions and confirmation are server-owned."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from services.match_state_service import get_match_status_snapshot, load_match_state
from .confirmation import INTERACTION_BUBBLE, SURFACE_PUBLIC, public_choice_projection
from .contracts import GuardResultCode, SubTaskResult, SubTaskStatus, ToolProposal
from .guard import guard_proposal
from .runtime_registry import TaskRunnerResult
from .sub_agents.base import SubAgentMetrics
from .write_executors import prepare_write_confirmation


INTENT_TOOLS = {
    "status": "match.get_status", "counterparty": "match.get_counterparty_summary",
    "start_search": "match.start_search", "cancel_search": "match.cancel_search",
    "accept_proposal": "match.decide_active_proposal", "dismiss_proposal": "match.decide_active_proposal",
}


def status_reply(snapshot: dict[str, Any]) -> str:
    if snapshot.get("reason_code") == "ambiguous_live_match":
        return "目前有多張有效配對提案，狀態需要處理；我沒有挑選對象或執行變更。"
    return {
        "idle": "目前沒有進行中的配對提案或搜尋。",
        "searching": "目前已有配對搜尋正在執行，這次沒有重複開始。",
        "waiting_user": "目前有一張既有提案等你接受或放棄，並不是這次新搜尋的結果。",
        "waiting_other": "目前的既有提案正在等對方回覆。",
        "incoming_decision": "目前有一張對方送來的提案等你接受或婉拒。",
        "accepted": "最近的配對已互相接受；解除已建立的關係不屬於撤回提案。",
        "declined": "最近一張提案已結束，目前沒有待決提案。",
        "expired": "最近一張提案已失效，目前沒有待決提案。",
        "no_candidates": "上一次搜尋沒有合適人選；這次尚未開始新的搜尋。",
        "failed": "上一次搜尋未完成；這次尚未開始新的搜尋。",
        "cancelled": "上一次搜尋已取消；這次尚未開始新的搜尋。",
    }.get(str(snapshot.get("state") or ""), "我暫時無法確認最新配對狀態，沒有執行任何變更。")


def safe_status_reply(user_id: str) -> str:
    try:
        return status_reply(get_match_status_snapshot(user_id))
    except Exception:
        return "我暫時無法讀取最新配對狀態，沒有執行任何變更。"


def proposal_provenance(proposal: dict[str, Any]) -> str:
    created = proposal.get("created_at")
    if isinstance(created, (int, float)) and created > 0:
        date = datetime.fromtimestamp(created, ZoneInfo("Asia/Taipei")).strftime("%Y/%m/%d")
        return f"這是 {date} 建立的既有提案，不是本次新搜尋結果。"
    return "這是既有提案，不是本次新搜尋結果。"


def proposal_allowed(intent: str | None, proposal: ToolProposal, *, allow_event: bool = False) -> bool:
    """Defense for legacy/injected runners: permission is not user intent."""
    if intent not in INTENT_TOOLS:
        return False
    if allow_event and proposal.tool_name == "match.decide_active_event_invitation":
        return (intent == "accept_proposal" and proposal.arguments == {"decision": "interested"}
                or intent == "dismiss_proposal" and proposal.arguments == {"decision": "declined"})
    if proposal.tool_name != INTENT_TOOLS[intent]:
        return False
    if intent == "accept_proposal":
        return proposal.arguments == {"decision": "interested"}
    if intent == "dismiss_proposal":
        return proposal.arguments in ({"decision": "declined"}, {"decision": "cancelled"})
    return not proposal.arguments


def _result(task_id: str, reply: str, code: str, *, tool: str | None = None, failed: bool = False) -> TaskRunnerResult:
    return TaskRunnerResult.from_completed([SubTaskResult(
        task_id=task_id, status=SubTaskStatus.FAILED if failed else SubTaskStatus.OK,
        tool_name=tool, error_code=code if failed else None,
        observation={"match_runtime": {"code": code, "reply": reply}},
    )])


def run(context_slice: Any, *, task: Any, services: Any) -> tuple[TaskRunnerResult, SubAgentMetrics]:
    intent = task.match_intent
    turn = services.turn_ctx
    metrics = SubAgentMetrics()
    audit = {"intent": intent or "missing", "action": "none", "outcome": "not_executed"}
    services.trace.setdefault("match_actions", []).append(audit)
    if intent not in INTENT_TOOLS or intent == "clarify":
        audit["outcome"] = "intent_unavailable"
        return _result(task.id, "這次沒有執行配對操作。" + safe_status_reply(turn.user_id), "intent_unavailable"), metrics
    if intent in {"status", "counterparty"}:
        tool = INTENT_TOOLS[intent]
        outcome = services.execute(
            ToolProposal(tool_name=tool, arguments={}), allowed_tools=frozenset({tool}),
            step_count=0, max_reads=1, prior_observations=[], call_index=0,
        )
        result = outcome.result
        audit.update(action=tool, outcome=result.status.value)
        if result.status is not SubTaskStatus.OK:
            return _result(task.id, "我暫時無法讀取最新配對狀態，沒有執行任何變更。", "state_unavailable", tool=tool, failed=True), metrics
        data = result.observation or {}
        if intent == "status":
            reply = status_reply(data)
            if turn.active_proposal:
                reply += proposal_provenance(turn.active_proposal)
        else:
            reply = (f"目前這位對象是{data.get('display_name') or '對方'}。{data.get('safe_summary') or ''}"
                     if data.get("found") else "目前沒有可提供公開摘要的配對對象。")
        # Reads carry verified facts, not an immutable transaction sentence.
        # Only previews and mutation outcomes must bypass normal composition.
        return TaskRunnerResult.from_completed([SubTaskResult(
            task_id=task.id, status=SubTaskStatus.OK, tool_name=tool,
            observation={**data, "verified_match_read": True,
                         "match_runtime": {"code": "verified_status", "reply": reply}},
        )]), metrics

    if turn.active_event_invitation and intent in {"accept_proposal", "dismiss_proposal"}:
        # Preserve the separate event-card workflow. The legacy semantic agent
        # may resolve which invitation was referenced, never change the action.
        from .sub_agents.match_agent import run as resolve_event
        proposals, metrics = resolve_event(context_slice, task_brief=task.task_brief)
        events = [p for p in proposals if p.tool_name == "match.decide_active_event_invitation"]
        if events:
            if len(events) == 1 and proposal_allowed(intent, events[0], allow_event=True):
                return TaskRunnerResult.from_proposals(events), metrics
            return _result(task.id, "這次活動邀請操作與你的要求不一致，沒有執行變更。", "intent_mismatch", failed=True), metrics
        if not turn.active_proposal:
            return _result(task.id, "我還無法確定你要處理哪張活動邀請，沒有執行變更。", "event_target_unavailable"), metrics

    try:
        state = load_match_state(turn.user_id)
    except Exception:
        return _result(task.id, "我暫時無法讀取最新配對狀態，沒有執行任何變更。", "state_unavailable", failed=True), metrics
    if state["ambiguous"]:
        return _result(task.id, "目前有多張有效提案，不能安全地替你決定；沒有執行變更。", "ambiguous_live_match", failed=True), metrics
    active = state["active_proposal"]
    prior_authority = getattr(turn, "_active_proposal_authority", None) or {}
    if active and str(prior_authority.get("match_id") or "") != str(active.get("_id") or ""):
        return _result(task.id, "目前提案剛有更新，這次沒有替新對象建立確認。請重新查看提案。", "proposal_changed", failed=True), metrics
    # Rebind only server-owned context, never a model-provided target.
    turn = turn.model_copy(update={"active_proposal": None, "match_search": state["search"]})
    if active:
        turn.active_proposal = {
            "status": active["status"], "stage": state["stage"],
            "allowed_actions": state["allowed_actions"],
            "proposal_revision": int(active.get("proposal_revision", 0)),
            "counterparty": (services.turn_ctx.active_proposal or {}).get("counterparty") or "目前對象",
            "created_at": active.get("created_at"),
        }
        turn._active_proposal_authority = {
            "match_id": str(active["_id"]), "expected_status": active["status"],
            "proposal_namespace": "relationship_match",
        }
    else:
        turn._active_proposal_authority = None

    restart = intent == "start_search" and bool(active)
    tool = INTENT_TOOLS[intent]
    args: dict[str, Any] = {}
    if intent == "start_search" and state["search"]["status"] in {"queued", "running", "searching"}:
        return _result(task.id, "目前已有搜尋正在執行，這次沒有重複開始。", "search_already_active"), metrics
    if restart or intent == "dismiss_proposal":
        tool = "match.decide_active_proposal"
        allowed = state["allowed_actions"]
        decision = "cancelled" if "cancelled" in allowed else "declined" if "declined" in allowed else None
        if decision is None:
            return _result(task.id, "目前沒有可放棄或撤回的提案。" + safe_status_reply(turn.user_id), "proposal_not_actionable"), metrics
        args = {"decision": decision}
    elif intent == "accept_proposal":
        if "interested" not in state["allowed_actions"]:
            return _result(task.id, "目前沒有可接受的提案。" + safe_status_reply(turn.user_id), "proposal_not_actionable"), metrics
        args = {"decision": "interested"}

    decision = guard_proposal(ToolProposal(tool_name=tool, arguments=args), agent_name="match", seen_keys=set(), step_count=0, max_reads=1)
    services.trace["guard_results"].append(decision.code.value)
    if decision.code is not GuardResultCode.WRITE_REQUIRES_CONFIRMATION:
        return _result(task.id, "這次操作沒有通過安全檢查，沒有執行變更。", "guard_rejected", failed=True), metrics
    payload, preview = prepare_write_confirmation(tool, args, turn._raw_ctx, turn)
    if payload is None:
        audit.update(action=tool, outcome="preflight_rejected")
        return _result(task.id, preview or "這次沒有執行配對操作。", "preflight_rejected", failed=True), metrics
    if active:
        preview = proposal_provenance(active) + str(preview or "")
    if restart:
        payload["data"]["continuation"] = "offer_start_search"
        preview += "這一步只結束目前提案；成功後會再問你是否開始新搜尋。"
    try:
        services.create_confirmation(
            user_id=turn.user_id, agent_name="match", tool_name=tool,
            arguments=payload["arguments"], payload=payload["data"],
            origin_run_id=services.run_id, preview=preview,
            interaction_mode=INTERACTION_BUBBLE,
        )
    except Exception:
        return _result(task.id, "這次未能建立確認，沒有執行配對變更。", "confirmation_unavailable", failed=True), metrics
    audit.update(action=tool, outcome="confirmation_prepared")
    return TaskRunnerResult.from_completed([SubTaskResult(
        task_id=task.id, status=SubTaskStatus.OK, tool_name=tool,
        observation={"pending_confirmation": True, "tool_name": tool, "preview": preview},
    )]), metrics


def offer_restart_continuation(manager: Any, ctx: Any, turn: Any, parent: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    """Prepare a second confirmation only after a proven applied first step."""
    if (parent.get("status") != "completed" or parent.get("error_code")
            or parent.get("tool_name") != "match.decide_active_proposal"
            or parent.get("arguments", {}).get("decision") not in {"declined", "cancelled"}
            or parent.get("payload", {}).get("continuation") != "offer_start_search"):
        return None
    if parent.get("user_id") != ctx.user_id or parent.get("room_id") != ctx.room_id or parent.get("surface") != SURFACE_PUBLIC:
        return None
    import time
    if time.time() > float(parent.get("resolved_at") or 0) + 900:
        return None
    import uuid
    key = f"match-restart:{parent['_id']}"
    cid = uuid.uuid5(uuid.NAMESPACE_URL, f"ayue-choice:{ctx.user_id}:{ctx.room_id}:{SURFACE_PUBLIC}:{key}").hex
    existing = manager.record_for_choice(user_id=ctx.user_id, room_id=ctx.room_id, surface=SURFACE_PUBLIC, choice_id=cid, require_pending=False)
    if existing:
        if existing.get("status") not in {"prepared", "pending"}:
            return None
        if float(existing.get("expires_at", 0)) <= time.time():
            return None
        return {"reply": existing["preview_text"], "choice": public_choice_projection(existing), "origin_run_id": existing["origin_run_id"]}
    try:
        state = load_match_state(ctx.user_id)
        if state["search_blocked"]:
            return {"reply": "目前提案已結束，但狀態剛有更新；這次沒有開始新搜尋。" + safe_status_reply(ctx.user_id)}
        payload, preview = prepare_write_confirmation("match.start_search", {}, ctx, turn)
        if payload is None:
            return {"reply": "目前提案已結束。" + str(preview or "尚不能開始新搜尋。")}
        reply = "目前提案已結束。要開始新的配對搜尋嗎？再次確認後才會開始。"
        cid = manager.create_confirmation(
            user_id=ctx.user_id, room_id=ctx.room_id, surface=SURFACE_PUBLIC,
            agent_name="match", tool_name="match.start_search", arguments={},
            payload=payload["data"], origin_run_id=run_id, preview=reply,
            interaction_mode=INTERACTION_BUBBLE, idempotency_key=key,
        )
        record = manager.record_for_choice(user_id=ctx.user_id, room_id=ctx.room_id, surface=SURFACE_PUBLIC, choice_id=cid, require_pending=False)
        return {"reply": reply, "choice": public_choice_projection(record), "origin_run_id": record["origin_run_id"]}
    except Exception:
        return {"reply": "目前提案已結束，但暫時無法建立新搜尋確認；沒有開始搜尋。"}
