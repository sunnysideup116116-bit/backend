"""Short-lived typed Calendar drafts used to complete a clarification turn."""

from __future__ import annotations

import threading
import time
import os
import logging
from typing import Any


DRAFT_TTL_SECONDS = 15 * 60
_COLLECTION = None
_LOCK = threading.RLock()
_MEMORY: dict[str, dict[str, Any]] = {}
_LOGGER = logging.getLogger(__name__)
_FALLBACK_WARNING_EMITTED = False


def _warn_memory_fallback(reason: str) -> None:
    """Make non-durable draft state visible without spamming every turn."""
    global _FALLBACK_WARNING_EMITTED
    if _FALLBACK_WARNING_EMITTED:
        return
    _FALLBACK_WARNING_EMITTED = True
    _LOGGER.warning(
        "V3 calendar draft persistence is using process-local memory fallback: %s; "
        "multi-worker clarification state is not durable",
        reason,
    )


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
        except Exception as exc:
            _warn_memory_fallback(f"Mongo collection unavailable ({type(exc).__name__})")
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
    storage_backend = "memory_fallback"
    collection = _collection()
    try:
        if collection is not None:
            collection.update_one({"user_id": user_id}, {"$set": record}, upsert=True)
            storage_backend = "mongo"
    except Exception as exc:
        _warn_memory_fallback(f"Mongo write failed ({type(exc).__name__})")
    record["storage_backend"] = storage_backend
    with _LOCK:
        _MEMORY[user_id] = dict(record)
    return record


def get_draft(user_id: str) -> dict[str, Any] | None:
    now = time.time()
    record: dict[str, Any] | None = None
    loaded_from_mongo = False
    collection = _collection()
    storage_backend = "memory_fallback"
    try:
        record = collection.find_one({"user_id": user_id}) if collection is not None else None
        if record:
            storage_backend = "mongo"
            loaded_from_mongo = True
    except Exception as exc:
        _warn_memory_fallback(f"Mongo read failed ({type(exc).__name__})")
        record = None
    if not record:
        with _LOCK:
            record = dict(_MEMORY.get(user_id) or {}) or None
    if not record:
        return None
    if float(record.get("expires_at", 0) or 0) <= now:
        clear_draft(user_id)
        return None
    result = dict(record)
    result["storage_backend"] = "mongo" if loaded_from_mongo else storage_backend
    return result


def public_projection(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    command = dict(record.get("command") or {})
    return {
        "action": command.get("action"),
        "fields": {
            key: value for key, value in command.items()
            if key in {"title", "date", "start_time", "end_time", "duration_minutes", "timezone", "location", "notes"}
        },
        "missing_fields": list(record.get("missing_fields") or [])[:8],
        "storage_backend": str(record.get("storage_backend") or "unknown"),
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
    prior = dict(record.get("command") or {})
    values = command.model_dump(exclude_none=True)
    missing_fields = {
        str(field).strip() for field in (record.get("missing_fields") or []) if str(field).strip()
    }
    provided_fields = {
        key for key, value in values.items()
        if key not in {"action", "draft_mode"} and value not in (None, "", [])
    }
    # A duration is the semantic equivalent of supplying the missing end_time;
    # the server derives the authoritative clock value during preflight.
    effective_provided_fields = set(provided_fields)
    if "duration_minutes" in effective_provided_fields and "end_time" not in effective_provided_fields:
        effective_provided_fields.add("end_time")
    # draft_mode is model-produced guidance, not authority to discard a clear
    # missing-field continuation.  Preserve the draft when the new command
    # supplies only fields it previously reported missing, or those fields
    # plus an explicit correction to an already-present field.
    clear_continuation = bool(missing_fields and provided_fields) and (
        effective_provided_fields <= missing_fields
        or (
            effective_provided_fields & missing_fields
            and (effective_provided_fields - missing_fields) <= set(prior) | {"duration_minutes"}
        )
    )
    if (
        str(getattr(command, "action", "")) == "create"
        and {"title", "date", "start_time", "end_time"} <= effective_provided_fields
    ):
        # A complete command is a genuinely actionable replacement, even if
        # its fields happen to overlap the old draft.
        clear_continuation = False
    if mode == "replace" and not clear_continuation:
        return command
    effective_mode = "continue" if clear_continuation else mode
    if effective_mode != "continue":
        required = {"title", "date", "start_time", "end_time"}
        if str(getattr(command, "action", "")) == "create" and required <= set(values):
            return command
    if clear_continuation:
        values["draft_mode"] = "continue"
    for key, value in prior.items():
        if key in {"action", "draft_mode"}:
            continue
        if key == "target_hint" and values.get("target_reference"):
            continue
        if key == "target_reference" and values.get("target_hint"):
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
