"""Server-side scheduler for persisted proactive-care messages."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from bson.objectid import ObjectId
from database import messages_coll, profiles_coll
from services.chat_service import find_system_message_by_event, save_system_message_once
from services.message_use_service import is_reusable_for_care
from services.public_ai_room_scope import is_owned_public_ai_room
from services.proactive_followup_service import (
    FOLLOWUP_ACTIVE_CHAT_GRACE_SECONDS,
    FOLLOWUP_BUSY_GRACE_SECONDS,
    PROACTIVE_FOLLOWUPS,
    authorize_pending_delivery_slot,
    cancel_pending_delivery_slot,
    claim_delivery_slot,
    claim_followup_candidate,
    commit_delivery_slot,
    defer_followup_candidate,
    expire_followup_candidate,
    expire_stale_followups,
    followup_delivery_claim_is_current,
    followup_mode_for_user,
    is_proactive_care_enabled,
    list_pending_delivery_slots,
    mark_followup_asked,
    next_quiet_end,
    owner_busy_until,
    release_delivery_slot,
    release_followup_candidate,
    stage_delivery_slot,
    unanswered_followup_until,
)

from .proactive_care import (
    build_proactive_care_context,
    generate_proactive_care_outcome,
    proactive_frequency_seconds,
)

_STOP_EVENT = threading.Event()
_THREAD: threading.Thread | None = None


def _deliver_pending_slot(profile: dict, *, now: float) -> str:
    user_id = str(profile.get("user_id") or "")
    pending = profile.get("proactive_care_pending_delivery")
    if not user_id or not isinstance(pending, dict):
        return "skipped"
    event_key = str(pending.get("event_key") or "")
    if not event_key:
        return "skipped"
    try:
        # The scanner's snapshot is not authority: settings or this pending
        # event can change while another owner is being processed.
        current_profile = profiles_coll.find_one(
            {"user_id": user_id, "proactive_care_pending_delivery.event_key": event_key},
            {"proactive_care_pending_delivery": 1, "proactive_care_enabled": 1,
             "proactive_frequency": 1, "last_user_activity_at": 1,
             "mediator_calendar_access": 1},
        )
        if not current_profile:
            return "skipped"
        if not is_proactive_care_enabled(current_profile):
            cancel_pending_delivery_slot(user_id, event_key=event_key)
            return "skipped"
        pending = current_profile.get("proactive_care_pending_delivery") or {}
        candidate_id = str(pending.get("candidate_id") or "")
        candidate_token = str(pending.get("candidate_token") or "")
        room_id = str(pending.get("origin_room_id") or "")
        intended_message = str(pending.get("message") or "").strip()
        if not candidate_id or not room_id or not intended_message or not is_owned_public_ai_room(user_id, room_id):
            cancel_pending_delivery_slot(user_id, event_key=event_key)
            return "skipped"
        existing = find_system_message_by_event(room_id, event_key=event_key)
        if not existing:
            # Unsent work must still be timely and grounded. Already saved
            # messages only need bookkeeping, even after their candidate closes.
            candidate = PROACTIVE_FOLLOWUPS.find_one({
                "_id": candidate_id, "user_id": user_id, "room_id": room_id,
                "status": "processing", "lease_token": candidate_token,
                "expires_at": {"$gt": now},
            })
            if not candidate:
                cancel_pending_delivery_slot(user_id, event_key=event_key)
                return "skipped"
            if candidate.get("source_message_id"):
                source = messages_coll.find_one({
                    "_id": ObjectId(str(candidate["source_message_id"])),
                    "sender_id": user_id, "room_id": room_id,
                }, {"metadata.message_use": 1})
                if not source or not is_reusable_for_care(source):
                    cancel_pending_delivery_slot(user_id, event_key=event_key)
                    return "skipped"
            last_activity = float(current_profile.get("last_user_activity_at", 0) or 0)
            if next_quiet_end(now) is not None or (
                last_activity and now - last_activity < FOLLOWUP_ACTIVE_CHAT_GRACE_SECONDS
            ):
                return "skipped"
            if bool(current_profile.get("mediator_calendar_access", True)):
                busy_until, calendar_ok = owner_busy_until(user_id, now=now)
                if not calendar_ok or (busy_until and busy_until > now):
                    return "skipped"
            if not authorize_pending_delivery_slot(user_id, event_key, current_profile, now=now):
                return "retried"
            saved = save_system_message_once(
                room_id,
                intended_message,
                metadata={
                    "event_type": "proactive_care", "agent_run_id": candidate_token,
                    "grounding_source": "follow_up", "origin_room_id": room_id,
                },
                event_key=event_key,
            )
            # A concurrent worker or an ambiguous insert may have saved first.
            # Always read the stored timestamp, never the retry's attempted time.
            existing = find_system_message_by_event(room_id, event_key=event_key)
            if not existing and saved.get("created"):
                existing = saved
        if not existing:
            return "retried"
        message_id = str(existing.get("_id") or existing.get("message_id") or "")
        persisted_message = str(existing.get("content") or "").strip()
        sent_at = float(existing.get("timestamp", 0) or 0)
        if not message_id or not persisted_message or sent_at <= 0:
            return "retried"
        if candidate_token:
            mark_followup_asked(
                {"_id": candidate_id}, candidate_token, now=sent_at, message_id=message_id,
            )
        committed = commit_delivery_slot(
            user_id,
            event_key=event_key,
            now=sent_at,
            message_id=message_id,
            message=persisted_message,
            origin_room_id=room_id,
            require_enabled_flag="proactive_care_enabled" in current_profile,
        )
        return "delivered" if committed else "retried"
    except Exception:
        return "retried"


def run_due_proactive_care_once(*, now: float | None = None, limit: int = 40) -> dict[str, int]:
    """Claim, generate and persist one safe candidate per scheduler pass.

    The worker is intentionally independent from public Pi. Profile extraction
    creates candidates; this function only evaluates timing/consent gates and
    writes a grounded assistant message to the candidate's original room.
    """
    current_time = now if now is not None else time.time()
    stats = {"scanned": 0, "delivered": 0, "retried": 0, "skipped": 0, "shadowed": 0}
    mode = followup_mode_for_user("")
    if mode != "on":
        try:
            stats["shadowed"] = int(
                PROACTIVE_FOLLOWUPS.count_documents({"status": "pending"})
            ) if mode == "shadow" else 0
        except Exception:
            pass
        return stats
    for profile in list_pending_delivery_slots(limit=limit):
        stats["scanned"] += 1
        recovery = _deliver_pending_slot(profile, now=current_time)
        stats[recovery] += 1
    expire_stale_followups(now=current_time)
    try:
        candidates = list(PROACTIVE_FOLLOWUPS.find(
            {
                "$or": [
                    {
                        "status": "pending",
                        "expires_at": {"$gt": current_time},
                        "available_at": {"$lte": current_time},
                        "$or": [
                            {"next_attempt_at": {"$lte": current_time}},
                            {"next_attempt_at": {"$exists": False}},
                        ],
                    },
                    {
                        "status": "processing",
                        "expires_at": {"$gt": current_time},
                        "lease_until": {"$lte": current_time},
                    },
                ],
            },
        ).sort([("priority", -1), ("available_at", 1)]).limit(max(1, min(int(limit or 1), 100))))
    except Exception:
        return stats
    for raw_candidate in candidates:
        stats["scanned"] += 1
        candidate = dict(raw_candidate or {})
        user_id = str(candidate.get("user_id") or "")
        room_id = str(candidate.get("room_id") or "")
        if not user_id or not room_id or not is_owned_public_ai_room(user_id, room_id):
            expire_followup_candidate(candidate, now=current_time)
            stats["skipped"] += 1
            continue
        try:
            user_doc = profiles_coll.find_one(
                {"user_id": user_id},
                {
                    "user_id": 1,
                    "proactive_care_enabled": 1,
                    "proactive_frequency": 1,
                    "mediator_calendar_access": 1,
                    "last_user_activity_at": 1,
                    "proactive_care_delivery": 1,
                    "proactive_care_last_sent_at": 1,
                    "proactive_care_delivery_times": 1,
                    "current_context": 1,
                    "recent_context_state": 1,
                    "recent_context_updated_at": 1,
                    "recent_context_draft": 1,
                    "mediator_tone": 1,
                    "profile_memory_preview": 1,
                },
            ) or {}
        except Exception:
            stats["skipped"] += 1
            continue
        if not is_proactive_care_enabled(user_doc):
            expire_followup_candidate(candidate, now=current_time)
            stats["skipped"] += 1
            continue
        try:
            source_id = ObjectId(str(candidate.get("source_message_id") or ""))
            source = messages_coll.find_one(
                {
                    "_id": source_id,
                    "room_id": room_id,
                    "sender_id": user_id,
                },
                {"content": 1, "metadata.owner_raw_content": 1, "metadata.message_use": 1},
            )
            if not source or not is_reusable_for_care(source):
                expire_followup_candidate(candidate, now=current_time)
                stats["skipped"] += 1
                continue
        except Exception:
            stats["skipped"] += 1
            continue

        last_activity = float(user_doc.get("last_user_activity_at", 0) or 0)
        if last_activity and current_time - last_activity < FOLLOWUP_ACTIVE_CHAT_GRACE_SECONDS:
            defer_followup_candidate(
                candidate,
                until=max(
                    current_time + FOLLOWUP_ACTIVE_CHAT_GRACE_SECONDS,
                    last_activity + FOLLOWUP_ACTIVE_CHAT_GRACE_SECONDS,
                ),
                now=current_time,
            )
            stats["skipped"] += 1
            continue
        quiet_end = next_quiet_end(current_time)
        if quiet_end is not None:
            defer_followup_candidate(candidate, until=quiet_end, now=current_time)
            stats["skipped"] += 1
            continue
        delivery_marker = user_doc.get("proactive_care_delivery")
        if isinstance(delivery_marker, dict) and delivery_marker.get("message"):
            defer_followup_candidate(candidate, until=current_time + 15 * 60, now=current_time)
            stats["skipped"] += 1
            continue
        if bool(user_doc.get("mediator_calendar_access", True)):
            busy_until, calendar_ok = owner_busy_until(user_id, now=current_time)
            if not calendar_ok:
                defer_followup_candidate(
                    candidate,
                    until=current_time + FOLLOWUP_BUSY_GRACE_SECONDS,
                    now=current_time,
                )
                stats["skipped"] += 1
                continue
            if busy_until and busy_until > current_time:
                defer_followup_candidate(candidate, until=busy_until, now=current_time)
                stats["skipped"] += 1
                continue
        unanswered_until, unanswered_available = unanswered_followup_until(
            user_id, now=current_time,
        )
        if not unanswered_available:
            defer_followup_candidate(
                candidate,
                until=current_time + FOLLOWUP_BUSY_GRACE_SECONDS,
                now=current_time,
            )
            stats["skipped"] += 1
            continue
        if unanswered_until and unanswered_until > current_time:
            defer_followup_candidate(
                candidate, until=unanswered_until, now=current_time,
            )
            stats["skipped"] += 1
            continue
        delivery_token = claim_delivery_slot(user_id, str(candidate.get("_id") or ""), now=current_time)
        if not delivery_token:
            defer_followup_candidate(candidate, until=current_time + 60 * 60, now=current_time)
            stats["skipped"] += 1
            continue
        claimed = claim_followup_candidate(candidate, now=current_time)
        if not claimed:
            release_delivery_slot(user_id, delivery_token, candidate_id=str(candidate.get("_id") or ""))
            continue
        claim_token, claimed_candidate = claimed
        decision = None
        outcome = "invalid_output"
        try:
            context = build_proactive_care_context(
                user_id,
                user_doc,
                now=datetime.fromtimestamp(current_time, tz=ZoneInfo("Asia/Taipei")),
                room_id=room_id,
                candidate=claimed_candidate,
            )
            decision, outcome = generate_proactive_care_outcome(context)
            if not decision or decision.focus != "follow_up":
                decision = None
                outcome = "invalid_output"
            elif not followup_delivery_claim_is_current(
                user_id,
                candidate.get("_id"),
                claim_token,
                delivery_token,
                now=time.time(),
            ):
                decision = None
                outcome = "stale_claim"
            if decision:
                event_key = (
                    f"proactive-care:{candidate.get('_id')}"
                    f":r{int(claimed_candidate.get('revision', 1) or 1)}"
                )
                pending = stage_delivery_slot(
                    user_id,
                    delivery_token,
                    candidate_id=str(candidate.get("_id") or ""),
                    candidate_token=claim_token,
                    now=current_time,
                    event_key=event_key,
                    message=decision.message,
                    origin_room_id=room_id,
                    require_enabled_flag="proactive_care_enabled" in user_doc,
                )
                if pending:
                    delivery_profile = {
                        "user_id": user_id,
                        "proactive_care_pending_delivery": pending,
                    }
                    if "proactive_care_enabled" in user_doc:
                        delivery_profile["proactive_care_enabled"] = user_doc.get(
                            "proactive_care_enabled"
                        )
                    if "proactive_frequency" in user_doc:
                        delivery_profile["proactive_frequency"] = user_doc.get(
                            "proactive_frequency"
                        )
                    recovery = _deliver_pending_slot(
                        delivery_profile,
                        now=current_time,
                    )
                    stats[recovery] += 1
                    continue
                outcome = "provider_error"
        except Exception:
            outcome = "provider_error"
        release_delivery_slot(
            user_id, delivery_token, candidate_id=str(candidate.get("_id") or ""),
        )
        if outcome in {"provider_error", "invalid_output", "stale_claim"}:
            release_followup_candidate(
                claimed_candidate,
                claim_token,
                now=current_time,
                retry=True,
            )
            stats["retried"] += 1
        else:
            release_followup_candidate(
                claimed_candidate,
                claim_token,
                now=current_time,
                retry=False,
            )
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
            # Date follow-ups run first so a same-minute general candidate
            # cannot consume the shared automatic-prompt spacing window.
            from services.post_date_followup_service import run_post_date_followups_once

            date_stats = run_post_date_followups_once()
            if any(date_stats.get(key, 0) for key in ("delivered", "shadowed", "expired", "skipped")):
                print(
                    "[POST_DATE_FOLLOWUP] "
                    + " ".join(f"{key}={int(value)}" for key, value in date_stats.items())
                )
            run_due_proactive_care_once()
        except Exception as exc:
            print(f"Proactive care scheduler skipped: {type(exc).__name__}")


def start_proactive_care_scheduler(interval_seconds: float = 15.0) -> None:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return
    _STOP_EVENT.clear()
    _THREAD = threading.Thread(target=_loop, args=(max(5.0, interval_seconds),), name="ayue-proactive-care", daemon=True)
    _THREAD.start()


def stop_proactive_care_scheduler() -> None:
    _STOP_EVENT.set()
