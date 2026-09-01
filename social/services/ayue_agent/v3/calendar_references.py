"""Short-lived server-owned references for conversational Calendar follow-ups."""

from __future__ import annotations

import threading
import time
import os
from datetime import datetime, timedelta
from typing import Any

from services.calendar_service import as_utc, get_timezone


REFERENCE_TTL_SECONDS = 15 * 60
DRAFT_TARGET_REFERENCE_KEY = "draft_target"
RECENT_MUTATION_REFERENCE_KEY = "recent_mutation"
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
    if event.get("all_day"):
        inclusive_end = end.date() - timedelta(days=1)
        if inclusive_end == start.date():
            return f"{start.month}/{start.day} 全天 {title}"
        return f"{start.month}/{start.day}–{inclusive_end.month}/{inclusive_end.day} 全天 {title}"
    if end.date() != start.date():
        return f"{start.month}/{start.day} {start:%H:%M}–{end.month}/{end.day} {end:%H:%M} {title}"
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


def remember_recent_mutation(
    user_id: str,
    *,
    action: str,
    outcome: str,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Store a bounded, server-only summary of the latest Calendar write.

    IDs/revisions are retained only for authoritative verification.  The
    public projection below deliberately exposes labels and outcome only.
    """
    now = time.time()
    safe_operations: list[dict[str, Any]] = []
    for item in operations[:5]:
        if not isinstance(item, dict):
            continue
        safe_operations.append({
            "action": str(item.get("action") or action)[:24],
            "event_id": str(item.get("event_id") or "")[:160],
            "revision": int(item.get("revision", 0) or 0),
            "source_type": str(item.get("source_type") or "personal")[:16],
            "other_id": str(item.get("other_id") or "")[:160],
            "coordination_id": str(item.get("coordination_id") or "")[:160],
            "expected_status": str(item.get("expected_status") or "confirmed")[:32],
            "safe_label": str(item.get("safe_label") or "行程")[:180],
        })
    record = {
        "user_id": user_id,
        "reference_key": RECENT_MUTATION_REFERENCE_KEY,
        "action": str(action or "calendar")[:24],
        "outcome": str(outcome or "failed")[:24],
        "operations": safe_operations,
        "created_at": now,
        "expires_at": now + REFERENCE_TTL_SECONDS,
    }
    with _LOCK:
        _MEMORY[(user_id, RECENT_MUTATION_REFERENCE_KEY)] = dict(record)
    collection = _collection()
    try:
        if collection is not None:
            collection.update_one(
                {"user_id": user_id, "reference_key": RECENT_MUTATION_REFERENCE_KEY},
                {"$set": record},
                upsert=True,
            )
    except Exception:
        pass
    return record


def get_recent_mutation(user_id: str) -> dict[str, Any] | None:
    return get_reference(user_id, reference_key=RECENT_MUTATION_REFERENCE_KEY)


def recent_mutation_projection(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Project only safe, human-readable mutation state into agent context."""
    if not record:
        return None
    return {
        "action": str(record.get("action") or "calendar"),
        "outcome": str(record.get("outcome") or "failed"),
        "labels": [
            str(item.get("safe_label") or "行程")[:180]
            for item in (record.get("operations") or [])[:5]
            if isinstance(item, dict)
        ],
    }


def verify_recent_mutation(user_id: str) -> dict[str, Any]:
    """Verify the latest mutation against canonical Calendar/domain state."""
    record = get_recent_mutation(user_id)
    if not record:
        return {"status": "not_available", "action": "", "label": "", "outcome": ""}
    outcome = str(record.get("outcome") or "failed")
    operations = [item for item in (record.get("operations") or []) if isinstance(item, dict)]
    label = str((operations[0] if operations else {}).get("safe_label") or "行程")[:180]
    if outcome in {"failed", "error"}:
        return {"status": "failed", "action": str(record.get("action") or "calendar"), "label": label, "outcome": outcome}

    states: list[bool] = []
    try:
        from services.calendar_service import get_owned_event_by_id
        from services.date_coordination_service import find_accepted_match

        for operation in operations:
            expected = str(operation.get("expected_status") or "confirmed")
            event_id = str(operation.get("event_id") or "")
            current = get_owned_event_by_id(
                user_id,
                event_id,
                include_cancelled=True,
                source_type=str(operation.get("source_type") or "") or None,
            ) if event_id else None
            event_ok = bool(current and str(current.get("status") or "") == expected)
            if not event_ok and str(operation.get("source_type") or "") == "date":
                other_id = str(operation.get("other_id") or "")
                coordination_id = str(operation.get("coordination_id") or "")
                if other_id and coordination_id:
                    coordination = (find_accepted_match(user_id, other_id).get("date_coordination") or {})
                    event_ok = (
                        str(coordination.get("coordination_id") or "") == coordination_id
                        and str(coordination.get("status") or "") == expected
                    )
            states.append(event_ok)
    except Exception:
        states = []

    if states and all(states):
        status = "verified_success"
    elif outcome == "partial":
        status = "partial"
    else:
        status = "verification_failed"
    return {
        "status": status,
        "action": str(record.get("action") or "calendar"),
        "label": label,
        "outcome": outcome,
    }


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
