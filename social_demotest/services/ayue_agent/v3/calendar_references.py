"""Short-lived server-owned references for conversational Calendar follow-ups."""

from __future__ import annotations

import threading
import time
import os
from datetime import datetime
from typing import Any

from services.calendar_service import as_utc, get_timezone


REFERENCE_TTL_SECONDS = 15 * 60
DRAFT_TARGET_REFERENCE_KEY = "draft_target"
_COLLECTION = None
_LOCK = threading.RLock()
_MEMORY: dict[tuple[str, str], dict[str, Any]] = {}


def clear_runtime_state() -> None:
    """Clear the process-local fallback used by the demo/test runtime."""
    with _LOCK:
        _MEMORY.clear()


def _collection() -> Any:
    """Optional persistence; memory is the safe default for the demo runtime."""
    global _COLLECTION
    if os.getenv("AYUE_TEST_MODE", "off").strip().lower() in {"1", "true", "on"}:
        return None
    if os.getenv("AYUE_CALENDAR_STATE_MONGO", "on").strip().lower() not in {"1", "true", "on"}:
        return None
    if _COLLECTION is None:
        try:
            from database import db
            _COLLECTION = db["v3_calendar_references"]
        except Exception:
            return None
    return _COLLECTION


def ensure_indexes() -> None:
    collection = _collection()
    if collection is None:
        return
    try:
        collection.create_index([("user_id", 1), ("reference_key", 1)], unique=True)
        collection.create_index("expires_at", expireAfterSeconds=0)
    except Exception:
        pass


def _event_label(event: dict[str, Any]) -> str:
    zone = get_timezone(event.get("timezone") or "Asia/Taipei")
    start_value = event.get("start_at")
    end_value = event.get("end_at")
    if isinstance(start_value, str):
        start_value = datetime.fromisoformat(start_value.replace("Z", "+00:00"))
    if isinstance(end_value, str):
        end_value = datetime.fromisoformat(end_value.replace("Z", "+00:00"))
    start = as_utc(start_value).astimezone(zone)
    end = as_utc(end_value).astimezone(zone)
    title = str(event.get("activity") or event.get("title") or "這筆行程").strip()
    return f"{start.month}/{start.day} {start:%H:%M}–{end:%H:%M} {title}"


def _record(user_id: str, reference_key: str, event: dict[str, Any], *, safe_label: str = "") -> dict[str, Any]:
    now = time.time()
    other_id = None
    if event.get("source_type") == "date":
        other_id = next((p for p in (event.get("participants") or []) if p != user_id), None)
    record = {
        "user_id": user_id,
        "reference_key": reference_key,
        "event_id": str(event.get("event_id") or ""),
        "revision": int(event.get("revision", 1) or 1),
        "source_type": str(event.get("source_type") or "personal"),
        "other_id": other_id,
        "coordination_id": event.get("coordination_id"),
        "safe_label": safe_label or _event_label(event),
        "created_at": now,
        "expires_at": now + REFERENCE_TTL_SECONDS,
    }
    if not record["event_id"]:
        return {}
    with _LOCK:
        _MEMORY[(user_id, reference_key)] = dict(record)
    collection = _collection()
    try:
        if collection is not None:
            collection.update_one(
            {"user_id": user_id, "reference_key": reference_key},
            {"$set": record},
            upsert=True,
            )
    except Exception:
        pass
    return record


def remember_event(user_id: str, event: dict[str, Any], *, reference_key: str = "recent_event", safe_label: str = "") -> dict[str, Any]:
    return _record(user_id, reference_key, event, safe_label=safe_label)


def remember_resolved_target(user_id: str, target: Any) -> dict[str, Any]:
    """Persist a preflight-resolved target for a clarification continuation.

    The actual event id/revision stay in this server-owned reference store;
    only the bounded label is projected into the Calendar draft context.
    """
    if hasattr(target, "model_dump"):
        target = target.model_dump()
    target = dict(target or {})
    event = {
        "event_id": target.get("event_id"),
        "revision": target.get("expected_revision", target.get("revision", 1)),
        "source_type": target.get("source_type") or "personal",
        "coordination_id": target.get("coordination_id"),
        "participants": [user_id] + ([target.get("other_id")] if target.get("other_id") else []),
    }
    return _record(
        user_id,
        DRAFT_TARGET_REFERENCE_KEY,
        event,
        safe_label=str(target.get("safe_label") or "這筆行程"),
    )


def remember_candidates(user_id: str, events: list[dict[str, Any]], *, limit: int = 3) -> list[dict[str, Any]]:
    """Persist bounded candidate references without exposing authority fields."""
    records: list[dict[str, Any]] = []
    for index, event in enumerate(events[:limit], start=1):
        record = _record(user_id, f"candidate_{index}", event)
        if record:
            records.append(record)
    for index in range(len(records) + 1, limit + 1):
        clear_reference(user_id, reference_key=f"candidate_{index}")
    return records


def get_reference(user_id: str, *, reference_key: str = "recent_event") -> dict[str, Any] | None:
    now = time.time()
    key = (user_id, reference_key)
    record: dict[str, Any] | None = None
    collection = _collection()
    try:
        record = collection.find_one({"user_id": user_id, "reference_key": reference_key}) if collection is not None else None
    except Exception:
        record = None
    if not record:
        with _LOCK:
            record = dict(_MEMORY.get(key) or {}) or None
    if not record:
        return None
    if float(record.get("expires_at", 0) or 0) <= now:
        clear_reference(user_id, reference_key=reference_key)
        return None
    return dict(record)


def public_projection(record: dict[str, Any] | None) -> dict[str, str] | None:
    """Return only the information safe to place in an agent context."""
    if not record:
        return None
    return {
        "reference": str(record.get("reference_key") or "recent_event"),
        "label": str(record.get("safe_label") or "這筆行程")[:180],
    }


def clear_reference(user_id: str, *, reference_key: str = "recent_event") -> None:
    with _LOCK:
        _MEMORY.pop((user_id, reference_key), None)
    collection = _collection()
    try:
        if collection is not None:
            collection.delete_one({"user_id": user_id, "reference_key": reference_key})
    except Exception:
        pass
