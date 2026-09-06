"""Schedule owner-message profile extraction without coupling it to a router."""

from __future__ import annotations

import os
import re
import threading
import time

from bson.objectid import ObjectId

from database import messages_coll, profiles_coll
from services.profile_skills import PROFILE_RUNS, process_profile_message, profile_skills_mode_for_user
from services.message_use_service import is_reusable_for_profile
from services.public_ai_room_scope import is_owned_public_ai_room
from services.proactive_followup_service import followup_mode_for_user


PROFILE_PROCESS_TTL_SECONDS = 30
PROFILE_RETRY_POLL_SECONDS = 30.0
_PROFILE_RETRY_STOP = threading.Event()
_PROFILE_RETRY_THREAD: threading.Thread | None = None
_PROFILE_RETRY_LOCK = threading.Lock()


def queue_profile_coverage(
    background_tasks, user_id: str, room_id: str, message_ids: list[str],
) -> dict:
    """Queue only unclaimed owner messages before compaction hides old turns."""
    profile_mode = profile_skills_mode_for_user(user_id)
    followup_mode = followup_mode_for_user(user_id)
    mode = profile_mode if profile_mode != "off" else followup_mode
    result = {
        "version": "profile-coverage-v1", "status": "ok", "requeued_count": 0,
    }
    if mode == "off":
        return {**result, "status": "disabled"}
    expected_room = str(room_id)
    if not is_owned_public_ai_room(user_id, expected_room):
        return {**result, "status": "invalid_scope"}
    safe_ids: list[ObjectId] = []
    for value in list(message_ids or [])[:20]:
        try:
            safe_ids.append(ObjectId(str(value)))
        except Exception:
            continue
    if not safe_ids:
        return result
    try:
        sources = list(messages_coll.find(
            {"_id": {"$in": safe_ids}, "room_id": expected_room, "sender_id": user_id},
            {
                "content": 1,
                "metadata.owner_raw_content": 1,
                "metadata.message_use": 1,
            },
        ))
        now = time.time()
        claimed: set[str] = set()
        for row in PROFILE_RUNS.find(
            {"message_id": {"$in": [str(value) for value in safe_ids]}, "user_id": user_id},
            {
                "message_id": 1,
                "status": 1,
                "attempt_count": 1,
                "next_attempt_at": 1,
                "lease_until": 1,
            },
        ):
            message_key = str(row.get("message_id") or "")
            status = str(row.get("status") or "")
            attempts = int(row.get("attempt_count", 0) or 0)
            if status in {"completed", "excluded"}:
                claimed.add(message_key)
            elif status == "processing" and float(row.get("lease_until", 0) or 0) > now:
                claimed.add(message_key)
            elif status == "failed" and (
                attempts >= 3
                or float(row.get("next_attempt_at", 0) or 0) > now
            ):
                claimed.add(message_key)
    except Exception:
        return {**result, "status": "storage_unavailable"}
    requeued = 0
    for source in sources:
        message_id = str(source.get("_id") or "")
        if not message_id or message_id in claimed:
            continue
        if not is_reusable_for_profile(source):
            continue
        owner_message = ((source.get("metadata") or {}).get("owner_raw_content"))
        owner_message = owner_message if isinstance(owner_message, str) else source.get("content")
        if not isinstance(owner_message, str):
            continue
        background_tasks.add_task(
            process_profile_message, user_id, owner_message, message_id, "global", None,
        )
        requeued += 1
    return {**result, "requeued_count": requeued}


def run_due_profile_retries_once(*, now: float | None = None, limit: int = 3) -> dict[str, int]:
    """Retry provider-failed profile extraction from the saved owner source."""
    current = time.time() if now is None else float(now)
    stats = {"scanned": 0, "attempted": 0, "rescheduled": 0, "completed": 0, "excluded": 0}
    try:
        records = PROFILE_RUNS.find(
            {
                "$or": [
                    {
                        "status": "failed",
                        "attempt_count": {"$lt": 3},
                        "next_attempt_at": {"$lte": current},
                    },
                    {
                        "status": "processing",
                        "attempt_count": {"$lt": 3},
                        "lease_until": {"$lte": current},
                    },
                ],
            },
            {
                "message_id": 1,
                "user_id": 1,
                "surface": 1,
                "match_id": 1,
            },
        ).sort("next_attempt_at", 1).limit(max(1, min(int(limit or 1), 10)))
    except Exception:
        return stats
    for record in records:
        stats["scanned"] += 1
        user_id = str(record.get("user_id") or "")
        message_id = str(record.get("message_id") or "")
        try:
            source_id = ObjectId(message_id)
        except Exception:
            continue
        try:
            source = messages_coll.find_one(
                {"_id": source_id, "sender_id": user_id},
                {
                    "content": 1,
                    "metadata.owner_raw_content": 1,
                    "metadata.message_use": 1,
                },
            )
        except Exception:
            # A storage outage must leave the failed record retryable.
            continue
        if not source or not is_reusable_for_profile(source):
            try:
                PROFILE_RUNS.update_one(
                    {
                        "message_id": message_id,
                        "status": {"$in": ["failed", "processing"]},
                    },
                    {
                        "$set": {
                            "status": "excluded",
                            "updated_at": current,
                            "failure_code": "source_unavailable_or_excluded",
                        },
                        "$unset": {"next_attempt_at": "", "lease_until": "", "lease_token": ""},
                    },
                )
            except Exception:
                pass
            stats["excluded"] += 1
            continue
        owner_message = ((source.get("metadata") or {}).get("owner_raw_content"))
        owner_message = owner_message if isinstance(owner_message, str) else source.get("content")
        if not isinstance(owner_message, str) or not owner_message.strip():
            continue
        stats["attempted"] += 1
        result = process_profile_message(
            user_id,
            owner_message,
            message_id,
            str(record.get("surface") or "global")[:40],
            str(record.get("match_id")) if record.get("match_id") else None,
        )
        if (result or {}).get("retry_scheduled"):
            stats["rescheduled"] += 1
        else:
            stats["completed"] += 1
    return stats


def _profile_retry_poll_seconds() -> float:
    try:
        configured = float(os.getenv("AYUE_PROFILE_RETRY_POLL_SECONDS", str(PROFILE_RETRY_POLL_SECONDS)))
    except (TypeError, ValueError):
        configured = PROFILE_RETRY_POLL_SECONDS
    return max(10.0, min(configured, 300.0))


def _profile_retry_worker_loop() -> None:
    while not _PROFILE_RETRY_STOP.wait(_profile_retry_poll_seconds()):
        try:
            run_due_profile_retries_once()
        except Exception as exc:
            print(f"Profile retry worker skipped: {type(exc).__name__}")


def start_profile_retry_worker() -> None:
    global _PROFILE_RETRY_THREAD
    if os.getenv("AYUE_PROFILE_RETRY_WORKER_ENABLED", "on").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return
    with _PROFILE_RETRY_LOCK:
        if _PROFILE_RETRY_THREAD and _PROFILE_RETRY_THREAD.is_alive():
            return
        _PROFILE_RETRY_STOP.clear()
        _PROFILE_RETRY_THREAD = threading.Thread(
            target=_profile_retry_worker_loop,
            name="profile-retry-worker",
            daemon=True,
        )
        _PROFILE_RETRY_THREAD.start()


def stop_profile_retry_worker() -> None:
    global _PROFILE_RETRY_THREAD
    _PROFILE_RETRY_STOP.set()
    thread = _PROFILE_RETRY_THREAD
    if thread and thread.is_alive():
        thread.join(timeout=2.0)
    _PROFILE_RETRY_THREAD = None


def _safe_progress_token(value: str | None) -> str | None:
    token = str(value or "").strip().lower()
    return token if re.fullmatch(r"[0-9a-f]{32}", token) else None


def _run_profile_process(
    user_id: str, message: str, message_id: str | None, surface: str,
    match_id: str | None, progress_token: str,
) -> None:
    """Run extraction and publish only a privacy-safe process outcome."""
    try:
        profiles_coll.update_one(
            {
                "user_id": user_id,
                "agentic_profile_process.run_key": progress_token,
                "agentic_profile_process.state": "queued",
            },
            {"$set": {
                "agentic_profile_process.state": "processing",
                "agentic_profile_process.updated_at": time.time(),
            }},
        )
    except Exception:
        # Progress publication is optional and must never become the owner-data
        # extraction gate.
        pass
    # The process projection is only UI state. A newer message may replace it
    # before this task starts, but that must never suppress extraction of this
    # already-saved owner message. process_profile_message owns message-id
    # idempotency; the token only decides whether this run may publish progress.
    outcome = "no_update"
    try:
        result = process_profile_message(user_id, message, message_id, surface, match_id)
        if bool((result or {}).get("retry_scheduled")):
            outcome = "retrying"
        elif bool((result or {}).get("recent_changed")):
            outcome = "updated"
    except Exception:
        outcome = "error"
    try:
        profiles_coll.update_one(
            {
                "user_id": user_id,
                "agentic_profile_process.run_key": progress_token,
                "agentic_profile_process.state": "processing",
            },
            {"$set": {
                "agentic_profile_process.state": "completed",
                "agentic_profile_process.outcome": outcome,
                "agentic_profile_process.updated_at": time.time(),
            }},
        )
    except Exception:
        pass


def queue_profile_skills(
    background_tasks, user_id: str, message: str, message_id: str | None,
    surface: str, match_id: str | None = None, *, mode_resolver=None,
    progress_token: str | None = None,
) -> str:
    """Run profile writes from the saved owner message exactly once."""
    profile_mode = (mode_resolver or profile_skills_mode_for_user)(user_id)
    followup_mode = followup_mode_for_user(user_id)
    mode = profile_mode if profile_mode != "off" else followup_mode
    if mode == "off":
        return mode
    safe_token = _safe_progress_token(progress_token)
    # Follow-up-only extraction is a background data task and must not expose a
    # Profile UI progress card. The shared processor still performs the same
    # saved-message and lease checks.
    if safe_token and profile_mode != "off":
        now = time.time()
        try:
            profiles_coll.update_one(
                {"user_id": user_id},
                {"$set": {"agentic_profile_process": {
                    "version": "v1", "kind": "recent_context", "run_key": safe_token,
                    "state": "queued", "outcome": None,
                    "created_at": now, "updated_at": now,
                    "expires_at": now + PROFILE_PROCESS_TTL_SECONDS,
                }}},
                upsert=True,
            )
            background_tasks.add_task(
                _run_profile_process, user_id, message, message_id, surface, match_id, safe_token,
            )
            return mode
        except Exception:
            # The UI status is optional. Extraction remains owner-only and can
            # proceed without exposing a half-created process state.
            pass
    background_tasks.add_task(process_profile_message, user_id, message, message_id, surface, match_id)
    return mode
