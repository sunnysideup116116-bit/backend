"""Shared write boundary for public match actions.

The HTTP router owns transport and the candidate pipeline remains an injected
infrastructure executor. Public-Ayue and ``/api/match/decision`` both enter
this module before a match state transition, so neither path can maintain a
separate write rule.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

import requests

from database import matches_coll, profiles_coll
from services.giphy_service import schedule_match_celebration_gifs
from services.match_decision_service import apply_match_decision
from services.match_reason_service import accepted_opening_for_viewer
from services.match_state_service import derive_match_stage, reconcile_live_match
from services.mediator_event_service import queue_mediator_event
from services.preference_store import upsert_preference_facts
from services.proposal_namespace import (
    EVENT_INVITATION_NAMESPACE,
    RELATIONSHIP_MATCH_NAMESPACE,
    live_proposal_query,
    namespace_for_document,
)


TaskScheduler = Callable[..., Any]


def start_match_search(
    user_id: str, *, source: str, force_new: bool = False, idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Queue a canonical persistent search job; never rank candidates inline."""
    from services.match_search_job_service import enqueue_match_search
    return enqueue_match_search(
        user_id, source=source, force_new=force_new,
        idempotency_key=idempotency_key or f"legacy-match-search:{uuid.uuid4().hex}",
    )


def _schedule_or_run(scheduler: TaskScheduler | None, task: Callable[[], None]) -> None:
    if scheduler is None:
        task()
    else:
        scheduler(task)


def _safe_profile_label(profile: dict, user_id: str) -> str:
    value = str(
        profile.get("display_name") or profile.get("nickname") or profile.get("name") or ""
    ).strip()
    if not value or value == user_id or value.startswith(("seed_user_", "demo_user", "user_")):
        return "對方"
    return value[:30]


def _receiver_intro(profile: dict, user_id: str) -> str:
    label = _safe_profile_label(profile, user_id)
    greeting = f"{label}{label}～" if label != "對方" else "欸欸～"
    tone = str(profile.get("mediator_tone") or "friend")
    templates = {
        "gentle": f"{label if label != '對方' else '你'}，我留意到一位可能和你聊得來的人。先看看我整理的提案，不急著答應。",
        "enthusiastic": f"{greeting}我好像發現一條很有趣的線了 ✦ 先來看看這位人選，不急著決定～",
    }
    return templates.get(
        tone,
        f"{greeting}我幫你留意到一位可能適合認識的人 👀 先看看我整理的提案，不急著答應～",
    )


def apply_transition_effects(
    match_doc: dict, action: str, previous_status: str, explicit_reasons: list[str],
    *, schedule_task: TaskScheduler | None = None,
) -> None:
    """Run the user-visible effects only after the CAS transition succeeds."""
    from_id, to_id = match_doc["from_user"], match_doc["to_user"]
    match_id = str(match_doc["_id"])
    status = match_doc["status"]
    namespace = namespace_for_document(match_doc)
    if status == "pending":
        receiver_doc = profiles_coll.find_one({"user_id": to_id}) or {}
        try:
            queue_mediator_event(
                to_id, _receiver_intro(receiver_doc, to_id), "incoming_match_intro",
                event_key=f"match:{match_id}:pending-intro:{to_id}", match_id=match_id,
            )
        except Exception as exc:
            # The friendly preface is optional; the actionable proposal is not.
            print(f"[match] receiver intro delivery failed: {type(exc).__name__}")
        queue_mediator_event(
            to_id, "欸，有位人選想認識你，我先來問你本人。", "incoming_match_interest",
            event_key=f"match:{match_id}:pending-proposal:{to_id}", match_id=match_id,
            proposal_role="receiver",
            proposal_namespace=namespace,
        )
        return
    if status == "accepted":
        if (
            namespace == EVENT_INVITATION_NAMESPACE
            and match_doc.get("relationship_establishing") is False
        ):
            event_title = str(
                (match_doc.get("event_snapshot") or {}).get("title") or "這個活動"
            )[:80]
            for user_id in (from_id, to_id):
                queue_mediator_event(
                    user_id,
                    f"你們都對「{event_title}」有興趣；原本的聊天室可以直接接著聊。",
                    "event_invitation_accepted",
                    event_key=f"event-invitation:{match_id}:accepted:{user_id}",
                    match_id=match_id,
                    proposal_namespace=namespace,
                )
            return
        initiator_doc = profiles_coll.find_one({"user_id": from_id}) or {}
        target_doc = profiles_coll.find_one({"user_id": to_id}) or {}
        for user_id, other_id in ((from_id, to_id), (to_id, from_id)):
            other_doc = target_doc if other_id == to_id else initiator_doc
            other_label = _safe_profile_label(other_doc, other_id)
            opening = accepted_opening_for_viewer(match_doc, user_id, other_id, other_label)
            try:
                queue_mediator_event(
                    user_id,
                    opening,
                    "match_connected", event_key=f"match:{match_id}:accepted:{user_id}",
                    match_id=match_id, other_id=other_id,
                )
            except Exception as exc:
                print(f"[match] connected notification failed: {type(exc).__name__}")
        try:
            participants = (
                (from_id, to_id),
                (to_id, from_id),
            )
            schedule_match_celebration_gifs(
                participants, match_id, schedule_task=schedule_task,
            )
        except Exception as exc:
            print(f"[match] celebration GIF scheduling failed: {type(exc).__name__}")
        return
    if status != "declined":
        return
    actor = from_id if previous_status == "draft" or action == "cancel" else to_id
    other_id = to_id if actor == from_id else from_id
    target_doc = profiles_coll.find_one({"user_id": other_id}) or {}

    def notify_decline_feedback() -> None:
        try:
            response = requests.post("http://127.0.0.1:9001/api/feedback", json={
                "user_id": actor, "target_id": other_id, "action": "decline",
                "target_traits": target_doc.get("big_five", {}),
                "explicit_reasons": explicit_reasons,
            }, timeout=15)
            response.raise_for_status()
            memories = response.json().get("memories", [])
            if memories:
                upsert_preference_facts(
                    actor,
                    memories,
                    source="match_feedback",
                    message_id=f"feedback:{match_id}:{actor}",
                    match_id=match_id,
                )
        except Exception as exc:
            print(f"[match] feedback forwarding failed: {exc}")

    # Feedback is optional enrichment. Never block a synchronous agent turn on
    # the external feedback service when no background scheduler is available.
    if action == "decline" and schedule_task is not None:
        schedule_task(notify_decline_feedback)
    if previous_status == "pending":
        event_type = "match_cancelled" if action == "cancel" else "match_declined"
        message = (
            "對方取消了這次邀請，這張提案已經收起來。"
            if action == "cancel" else "對方這次先婉拒了邀請，這張提案已經收起來。"
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
    expected_namespace: str | None = None,
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
        expected_namespace=expected_namespace,
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
        expected_namespace=RELATIONSHIP_MATCH_NAMESPACE,
        idempotency_key=idempotency_key,
        schedule_task=schedule_task,
    )


def decide_active_event_invitation(
    *, user_id: str, decision: str, expected_revision: int, idempotency_key: str,
    schedule_task: TaskScheduler | None = None,
) -> dict[str, Any]:
    """Decide the sole Event invitation without touching the match namespace."""
    if decision not in {"interested", "declined"}:
        return {"status": "invalid", "invalid": True}
    live = list(matches_coll.find(
        live_proposal_query(user_id, EVENT_INVITATION_NAMESPACE),
        {
            "_id": 1, "from_user": 1, "to_user": 1, "status": 1,
            "proposal_revision": 1, "proposal_namespace": 1,
            "proposal_source": 1,
        },
    ).sort([("created_at", -1)]).limit(2))
    if len(live) != 1:
        return {"status": "stale", "stale": True}
    active = live[0]
    status = str(active.get("status") or "")
    actor = active.get("from_user") if status == "draft" else active.get("to_user")
    if status not in {"draft", "pending"} or actor != user_id:
        return {"status": "stale", "stale": True, "current_status": status}
    return decide_match(
        user_id=user_id,
        match_id=str(active["_id"]),
        action="accept" if decision == "interested" else "decline",
        expected_status=status,
        expected_revision=expected_revision,
        expected_namespace=EVENT_INVITATION_NAMESPACE,
        idempotency_key=idempotency_key,
        schedule_task=schedule_task,
    )
