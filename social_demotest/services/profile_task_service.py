"""Schedule owner-message profile extraction without coupling it to a router."""

from __future__ import annotations

import re
import time

from database import profiles_coll
from services.profile_skills import process_profile_message, profile_skills_mode_for_user


PROFILE_PROCESS_TTL_SECONDS = 30


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
