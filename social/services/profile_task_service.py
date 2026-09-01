"""Schedule owner-message profile extraction without coupling it to a router."""

from __future__ import annotations

import re
import time

from bson.objectid import ObjectId

from database import messages_coll, profiles_coll
from services.profile_skills import PROFILE_RUNS, process_profile_message, profile_skills_mode_for_user


PROFILE_PROCESS_TTL_SECONDS = 30


def queue_profile_coverage(
    background_tasks, user_id: str, room_id: str, message_ids: list[str],
) -> dict:
    """Queue only unclaimed owner messages before compaction hides old turns."""
    mode = profile_skills_mode_for_user(user_id)
    result = {
        "version": "profile-coverage-v1", "status": "ok", "requeued_count": 0,
    }
    if mode == "off":
        return {**result, "status": "disabled"}
    expected_room = "_".join(sorted([str(user_id), "ai_assistant"]))
    if str(room_id) != expected_room:
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
            {"content": 1, "metadata.owner_raw_content": 1},
        ))
        claimed = {
            str(row.get("message_id") or "")
            for row in PROFILE_RUNS.find(
                {"message_id": {"$in": [str(value) for value in safe_ids]}, "user_id": user_id},
                {"message_id": 1},
            )
        }
    except Exception:
        return {**result, "status": "storage_unavailable"}
    requeued = 0
    for source in sources:
        message_id = str(source.get("_id") or "")
        if not message_id or message_id in claimed:
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
        if bool((result or {}).get("recent_changed")):
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
    mode = (mode_resolver or profile_skills_mode_for_user)(user_id)
    if mode == "off":
        return mode
    safe_token = _safe_progress_token(progress_token)
    if safe_token:
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
