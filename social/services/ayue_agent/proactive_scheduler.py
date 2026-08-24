"""Server-side scheduler for persisted proactive-care messages."""

from __future__ import annotations

import threading
import time
from typing import Any

from database import profiles_coll
from services.chat_service import generate_room_id, save_message
from services.ai_room_service import most_recent_ai_room

from .proactive_care import (
    build_proactive_care_context,
    claim_proactive_care,
    finalize_proactive_care_claim,
    generate_proactive_care_outcome,
    proactive_care_claim_is_current,
    proactive_frequency_seconds,
    reschedule_proactive_care_claim,
)

_STOP_EVENT = threading.Event()
_THREAD: threading.Thread | None = None
_RETRY_DELAYS = (60, 300, 900)


def run_due_proactive_care_once(*, now: float | None = None, limit: int = 40) -> dict[str, int]:
    """Claim, generate and persist due care. Safe to call from many workers."""
    current_time = now if now is not None else time.time()
    stats = {"scanned": 0, "delivered": 0, "retried": 0, "skipped": 0}
    query = {
        "next_proactive_care_at": {"$lte": current_time},
        "proactive_frequency": {"$nin": [None, "none"]},
    }
    for user_doc in profiles_coll.find(query).sort("next_proactive_care_at", 1).limit(limit):
        stats["scanned"] += 1
        user_id = str(user_doc.get("user_id") or "")
        last_activity = float(user_doc.get("last_user_activity_at", 0) or 0)
        frequency = proactive_frequency_seconds(user_doc.get("proactive_frequency"))
        if not user_id or not last_activity or frequency is None:
            continue
        # Re-check the due time from the document to keep an old scheduler
        # scan from racing a just-updated frequency or newer user message.
        if float(user_doc.get("next_proactive_care_at", 0) or 0) > current_time:
            continue
        claim_id = claim_proactive_care(user_id, last_activity, now=current_time, due_before=current_time)
        if not claim_id:
            continue
        decision = None
        outcome = "invalid_output"
        try:
            decision, outcome = generate_proactive_care_outcome(build_proactive_care_context(user_id, user_doc))
            if decision and proactive_care_claim_is_current(user_id, claim_id, last_activity):
                message = save_message(
                    most_recent_ai_room(user_id), "ai_assistant", decision.message,
                    metadata={
                        "event_type": "proactive_care", "agent_run_id": claim_id,
                        "grounding_source": decision.focus,
                    },
                )
                marker = {"message": decision.message, "message_id": message.get("message_id"), "created_at": current_time}
                finalize_proactive_care_claim(
                    user_id, claim_id, last_activity, delivered=True, now=current_time, delivery_marker=marker,
                )
                stats["delivered"] += 1
                continue
        except Exception:
            outcome = "provider_error"

        retries = int(user_doc.get("proactive_care_retry_count", 0) or 0)
        if outcome in {"provider_error", "invalid_output"} and retries < len(_RETRY_DELAYS):
            reschedule_proactive_care_claim(
                user_id, claim_id, last_activity,
                retry_after=current_time + _RETRY_DELAYS[retries], retry_count=retries + 1,
            )
            stats["retried"] += 1
        else:
            # No grounded topic or exhausted retries: consume this activity
            # without fabricating a generic message.
            finalize_proactive_care_claim(user_id, claim_id, last_activity, delivered=False, now=current_time)
            stats["skipped"] += 1
    return stats


def backfill_missing_proactive_due_times(*, now: float | None = None, limit: int = 200) -> int:
    """Make pre-scheduler frequency settings effective after the first deploy."""
    current_time = now if now is not None else time.time()
    scheduled = 0
    for profile in profiles_coll.find(
        {"proactive_frequency": {"$nin": [None, "none"]}, "next_proactive_care_at": {"$exists": False}},
        {"user_id": 1, "proactive_frequency": 1, "last_user_activity_at": 1, "last_followup_activity_at": 1},
    ).limit(limit):
        user_id = str(profile.get("user_id") or "")
        activity = float(profile.get("last_user_activity_at", 0) or 0)
        handled = float(profile.get("last_followup_activity_at", 0) or 0)
        if not user_id or activity <= handled or activity <= 0:
            continue
        # Compare the activity timestamp in the write so a newer owner turn
        # cannot be replaced by this one-time backfill.
        frequency = profile.get("proactive_frequency")
        seconds = proactive_frequency_seconds(frequency)
        if seconds is None:
            continue
        result = profiles_coll.update_one(
            {"user_id": user_id, "last_user_activity_at": activity, "next_proactive_care_at": {"$exists": False}},
            {"$set": {"next_proactive_care_at": max(current_time, activity + seconds)}},
        )
        scheduled += int(bool(getattr(result, "modified_count", 0)))
    return scheduled


def _loop(interval_seconds: float) -> None:
    while not _STOP_EVENT.wait(interval_seconds):
        try:
            run_due_proactive_care_once()
        except Exception as exc:
            print(f"Proactive care scheduler skipped: {type(exc).__name__}")


def start_proactive_care_scheduler(interval_seconds: float = 15.0) -> None:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return
    _STOP_EVENT.clear()
    try:
        backfill_missing_proactive_due_times()
    except Exception as exc:
        print(f"Proactive care due-time backfill skipped: {type(exc).__name__}")
    _THREAD = threading.Thread(target=_loop, args=(max(5.0, interval_seconds),), name="ayue-proactive-care", daemon=True)
    _THREAD.start()


def stop_proactive_care_scheduler() -> None:
    _STOP_EVENT.set()
