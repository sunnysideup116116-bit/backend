"""Short-lived typed Calendar drafts used to complete a clarification turn."""

from __future__ import annotations

import threading
import time
import os
from typing import Any


DRAFT_TTL_SECONDS = 15 * 60
_COLLECTION = None
_LOCK = threading.RLock()
_MEMORY: dict[str, dict[str, Any]] = {}


def clear_runtime_state() -> None:
    """Clear the process-local fallback used by the demo/test runtime."""
    with _LOCK:
        _MEMORY.clear()


def _collection() -> Any:
    global _COLLECTION
    if os.getenv("AYUE_TEST_MODE", "off").strip().lower() in {"1", "true", "on"}:
        return None
    if os.getenv("AYUE_CALENDAR_STATE_MONGO", "on").strip().lower() not in {"1", "true", "on"}:
        return None
    if _COLLECTION is None:
        try:
            from database import db
            _COLLECTION = db["v3_calendar_drafts"]
        except Exception:
            return None
    return _COLLECTION


def ensure_indexes() -> None:
    collection = _collection()
    if collection is None:
        return
    try:
        collection.create_index("user_id", unique=True)
        collection.create_index("expires_at", expireAfterSeconds=0)
    except Exception:
        pass


def save_draft(
    user_id: str,
    command: Any,
    *,
    missing_fields: list[str] | None = None,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    values = command.model_dump(exclude_none=True) if hasattr(command, "model_dump") else dict(command or {})
    # A draft is deliberately authority-free.  Never persist an event identity
    # even if a future caller accidentally passes an untrusted dict.
    for field in ("event_id", "revision", "expected_revision", "user_id", "coordination_id", "other_id"):
        values.pop(field, None)
    now = time.time()
    record = {
        "user_id": user_id,
        "command": values,
        "missing_fields": list(missing_fields or []),
        "candidates": [
            {
                "reference": str(item.get("reference") or "")[:32],
                "label": str(item.get("label") or "")[:180],
            }
            for item in (candidates or [])[:3]
            if isinstance(item, dict) and item.get("reference") and item.get("label")
        ],
        "created_at": now,
        "expires_at": now + DRAFT_TTL_SECONDS,
    }
    with _LOCK:
        _MEMORY[user_id] = dict(record)
    collection = _collection()
    try:
        if collection is not None:
            collection.update_one({"user_id": user_id}, {"$set": record}, upsert=True)
    except Exception:
        pass
    return record


def get_draft(user_id: str) -> dict[str, Any] | None:
    now = time.time()
    record: dict[str, Any] | None = None
    collection = _collection()
    try:
        record = collection.find_one({"user_id": user_id}) if collection is not None else None
    except Exception:
        record = None
    if not record:
        with _LOCK:
            record = dict(_MEMORY.get(user_id) or {}) or None
    if not record:
        return None
    if float(record.get("expires_at", 0) or 0) <= now:
        clear_draft(user_id)
        return None
    return dict(record)


def public_projection(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    command = dict(record.get("command") or {})
    return {
        "action": command.get("action"),
        "fields": {
            key: value for key, value in command.items()
            if key in {"title", "date", "start_time", "end_time", "timezone", "location", "notes"}
        },
        "missing_fields": list(record.get("missing_fields") or [])[:8],
        "candidates": [
            {
                "reference": str(item.get("reference") or "")[:32],
                "label": str(item.get("label") or "")[:180],
            }
            for item in (record.get("candidates") or [])[:3]
            if isinstance(item, dict)
        ],
    }


def candidate_reference_allowed(record: dict[str, Any] | None, reference_key: str) -> bool:
    """Check that an opaque candidate token came from the active draft."""
    key = str(reference_key or "").strip()
    if not key.startswith("candidate_") or not record:
        return False
    return any(
        isinstance(item, dict) and str(item.get("reference") or "").strip() == key
        for item in (record.get("candidates") or [])
    )


def merge_command(command: Any, record: dict[str, Any] | None) -> Any:
    """Merge one same-domain continuation into the prior typed command."""
    if not record or str(getattr(command, "action", "")) != str((record.get("command") or {}).get("action")):
        return command
    mode = str(getattr(command, "draft_mode", "none") or "none")
    if mode == "replace":
        return command
    prior = dict(record.get("command") or {})
    values = command.model_dump(exclude_none=True)
    if mode != "continue":
        required = {"title", "date", "start_time", "end_time"}
        if str(getattr(command, "action", "")) == "create" and required <= set(values):
            return command
    for key, value in prior.items():
        if key in {"action", "draft_mode"}:
            continue
        if key not in values or values.get(key) in (None, "", []):
            values[key] = value
    # Target selectors are one logical field.  A continuation may replace a
    # recent reference with a natural-language hint (or vice versa), but the
    # two must never be merged into an invalid command.
    if values.get("target_hint"):
        values.pop("target_reference", None)
    elif values.get("target_reference"):
        values.pop("target_hint", None)
    # Import lazily so this module stays usable by context construction without
    # introducing a module cycle during application startup.
    from .calendar_commands import CalendarCommand
    return CalendarCommand.model_validate(values)


def clear_draft(user_id: str) -> None:
    with _LOCK:
        _MEMORY.pop(user_id, None)
    collection = _collection()
    try:
        if collection is not None:
            collection.delete_one({"user_id": user_id})
    except Exception:
        pass
