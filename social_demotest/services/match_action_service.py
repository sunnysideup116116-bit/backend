"""Shared write boundary for public match actions.

The HTTP router owns transport and the candidate pipeline remains an injected
infrastructure executor. Public-Ayue and ``/api/match/decision`` both enter
this module before a match state transition, so neither path can maintain a
separate write rule.
"""

from __future__ import annotations

from typing import Any, Callable

import requests

from database import profiles_coll
from services.ai_service import generate_peer_first_message
from services.chat_service import generate_room_id, save_message
from services.match_decision_service import apply_match_decision
from services.match_state_service import derive_match_stage, reconcile_live_match
from services.mediator_event_service import queue_mediator_event


SearchExecutor = Callable[[str, str, bool], dict[str, Any]]
TaskScheduler = Callable[..., Any]

_search_executor: SearchExecutor | None = None


def register_match_search_executor(executor: SearchExecutor) -> None:
    """Register the application-owned candidate-search implementation."""
    global _search_executor
    _search_executor = executor


def start_match_search(user_id: str, *, source: str, force_new: bool = False) -> dict[str, Any]:
    """Start a search through the registered domain executor, or fail closed."""
    executor = _search_executor
    if executor is None:
        return {"status": "failed", "detail": "match_search_executor_unavailable"}
    return executor(user_id, source, force_new)


def _schedule_or_run(scheduler: TaskScheduler | None, task: Callable[[], None]) -> None:
    if scheduler is None:
        task()
    else:
        scheduler(task)


def apply_transition_effects(
    match_doc: dict, action: str, previous_status: str, explicit_reasons: list[str],
    *, schedule_task: TaskScheduler | None = None,
) -> None:
    """Run the user-visible effects only after the CAS transition succeeds."""
    from_id, to_id = match_doc["from_user"], match_doc["to_user"]
    match_id = str(match_doc["_id"])
    status = match_doc["status"]
    if status == "pending":
        receiver_reason = match_doc.get("receiver_reason") or match_doc.get("reason", "")
        from_doc = profiles_coll.find_one(
            {"user_id": from_id}, {"_id": 0, "current_context": 1},
        ) or {}
        to_doc = profiles_coll.find_one(
            {"user_id": to_id}, {"_id": 0, "current_context": 1},
        ) or {}
        queue_mediator_event(
            to_id, f"欸，@{from_id} 想認識你，我先來問你本人。", "incoming_match_interest",
            event_key=f"match:{match_id}:pending:{to_id}", match_id=match_id, other_id=from_id,
            proposal_role="receiver", matches=[{
                "match_id": match_id, "matched_user_id": from_id,
                "contrast_label": match_doc.get("contrast_label", ""),
                "distinctive_tags": match_doc.get("distinctive_tags", []),
                "recommendation_reason": receiver_reason, "receiver_reason": receiver_reason,
                "score_breakdown": match_doc.get("receiver_score_breakdown", {}),
                "reason_items": match_doc.get("receiver_reason_items", []),
                "top_reasons": [
                    item.get("text") for item in match_doc.get("receiver_reason_items", [])
                    if item.get("kind") != "context_pair" and item.get("text")
                ][:2],
                "current_context": from_doc.get("current_context", ""),
                "target_context": to_doc.get("current_context", ""),
            }],
        )
        return
    if status == "accepted":
        initiator_doc = profiles_coll.find_one({"user_id": from_id}) or {}
        target_doc = profiles_coll.find_one({"user_id": to_id}) or {}
        reason = match_doc.get("reason", "")

        def send_first_message() -> None:
            try:
                save_message(
                    generate_room_id(from_id, to_id), from_id,
                    generate_peer_first_message(initiator_doc, target_doc, reason),
                )
            except Exception as exc:
                print(f"[match] first-message generation failed: {type(exc).__name__}")

        for user_id, other_id in ((from_id, to_id), (to_id, from_id)):
            queue_mediator_event(
                user_id,
                f"好，{other_id} 也點頭了！聊天室已經替你們開好。先自然打聲招呼，別一上來就像面試官，我會在旁邊幫你顧氣氛。",
                "match_connected", event_key=f"match:{match_id}:accepted:{user_id}",
                match_id=match_id, other_id=other_id,
            )
        _schedule_or_run(schedule_task, send_first_message)
        return
    if status != "declined":
        return
    actor = from_id if previous_status == "draft" or action == "cancel" else to_id
    other_id = to_id if actor == from_id else from_id
    target_doc = profiles_coll.find_one({"user_id": other_id}) or {}

    def notify_decline_feedback() -> None:
        try:
            requests.post("http://127.0.0.1:9001/api/feedback", json={
                "user_id": actor, "target_id": other_id, "action": "decline",
                "target_traits": target_doc.get("big_five", {}),
                "explicit_reasons": explicit_reasons,
            }, timeout=15).raise_for_status()
        except Exception as exc:
            print(f"[match] feedback forwarding failed: {exc}")

    # Feedback is optional enrichment. Never block a synchronous agent turn on
    # the external feedback service when no background scheduler is available.
    if action == "decline" and schedule_task is not None:
        schedule_task(notify_decline_feedback)
    if previous_status == "pending":
        event_type = "match_cancelled" if action == "cancel" else "match_declined"
        message = (
            f"@{actor} 取消了這次邀請，這張提案已經收起來。"
            if action == "cancel" else f"@{actor} 這次先婉拒了邀請，這張提案已經收起來。"
        )
        queue_mediator_event(
            other_id, message, event_type,
            event_key=f"match:{match_id}:{event_type}:{other_id}",
            match_id=match_id, other_id=actor,
        )


def decide_match(
    *,
    user_id: str,
    match_id: str,
    action: str,
    expected_status: str,
    expected_revision: int | None,
    explicit_reasons: list[str] | None = None,
    idempotency_key: str | None = None,
    schedule_task: TaskScheduler | None = None,
) -> dict[str, Any]:
    """Apply the canonical compare-and-set transition for every public caller."""
    def after_transition(match_doc: dict, transition_action: str, previous: str, reasons: list[str]) -> None:
        try:
            apply_transition_effects(
                match_doc, transition_action, previous, reasons, schedule_task=schedule_task,
            )
        except Exception as exc:
            # The database transition is already committed. A notification
            # failure must not make callers retry or report that it did not happen.
            print(f"[match] transition effects failed: {type(exc).__name__}")

    return apply_match_decision(
        user_id=user_id,
        match_id=match_id,
        action=action,
        expected_status=expected_status,
        expected_revision=expected_revision,
        explicit_reasons=explicit_reasons,
        idempotency_key=idempotency_key,
        after_transition=after_transition,
    )


def decide_active_proposal(
    *, user_id: str, decision: str, expected_revision: int, idempotency_key: str,
    schedule_task: TaskScheduler | None = None,
) -> dict[str, Any]:
    """Decide only the single current proposal; IDs and status stay server-side."""
    if decision not in {"interested", "declined"}:
        return {"status": "invalid", "invalid": True}
    active = reconcile_live_match(user_id)
    if not active or user_id not in {active.get("from_user"), active.get("to_user")}:
        return {"status": "stale", "stale": True}
    stage = derive_match_stage(active, user_id)
    if stage not in {"waiting_user", "incoming_decision"}:
        return {"status": "stale", "stale": True, "current_status": active.get("status")}
    return decide_match(
        user_id=user_id,
        match_id=str(active["_id"]),
        action="accept" if decision == "interested" else "decline",
        expected_status=str(active.get("status") or ""),
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        schedule_task=schedule_task,
    )
