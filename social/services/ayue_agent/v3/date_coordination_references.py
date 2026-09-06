"""Short-lived, room-scoped references for the latest date card.

The public Planner may receive a small projection of this reference so a
follow-up such as ``可以取消嗎`` has a stable referent.  Match and
coordination identifiers stay in this module and are returned only through
the executor-side authority projection.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from database import calendar_events_coll, matches_coll
from services.match_state_service import verified_accepted_match_query
from services.ayue_agent.public_relationship_projection import display_name


REFERENCE_TTL_SECONDS = 30 * 60
REFERENCE_KEY = "recent_action_reference"
CANCELLABLE_STATUSES = frozenset({"pending_partner", "active", "completed"})
MAX_PUBLIC_SUMMARY_CARDS = 3

_COLLECTION = None
_LOCK = threading.RLock()
_MEMORY: dict[tuple[str, str, str], dict[str, Any]] = {}


def _safe_display_name(user_id: str) -> str:
    try:
        return str(display_name(user_id) or "對方")[:30] or "對方"
    except Exception:
        return "對方"


def clear_runtime_state() -> None:
    """Clear the process-local fallback used by tests and demo mode."""
    with _LOCK:
        _MEMORY.clear()


def _collection() -> Any:
    global _COLLECTION
    if os.getenv("AYUE_TEST_MODE", "off").strip().lower() in {"1", "true", "on"}:
        return None
    if os.getenv("AYUE_DATE_REFERENCE_MONGO", "on").strip().lower() not in {"1", "true", "on"}:
        return None
    if _COLLECTION is None:
        try:
            from database import db
            _COLLECTION = db["v3_recent_action_references"]
        except Exception:
            return None
    return _COLLECTION


def ensure_indexes() -> None:
    collection = _collection()
    if collection is None:
        return
    try:
        collection.create_index(
            [("user_id", 1), ("room_id", 1), ("reference_key", 1)],
            unique=True,
        )
        collection.create_index("expires_at", expireAfterSeconds=0)
    except Exception:
        pass


def _memory_key(user_id: str, room_id: str) -> tuple[str, str, str]:
    return user_id, room_id, REFERENCE_KEY


def remember_date_coordination(
    user_id: str,
    room_id: str,
    match: dict[str, Any] | None,
    coordination: dict[str, Any] | None,
    *,
    other_id: str | None = None,
    safe_label: str | None = None,
) -> dict[str, Any]:
    """Remember a successfully created date card for one owner and room."""
    if not user_id or not room_id or not isinstance(match, dict) or not isinstance(coordination, dict):
        return {}
    coordination_id = str(coordination.get("coordination_id") or "").strip()
    resolved_other = str(other_id or (match.get("to_user") if match.get("from_user") == user_id else match.get("from_user")) or "").strip()
    if not coordination_id or not resolved_other or user_id not in {match.get("from_user"), match.get("to_user")}:
        return {}
    now = time.time()
    label = str(safe_label or _safe_display_name(resolved_other) or "對方").strip()[:30] or "對方"
    record = {
        "user_id": user_id,
        "room_id": room_id,
        "reference_key": REFERENCE_KEY,
        "kind": "date_coordination",
        "match_id": str(match.get("_id") or ""),
        "coordination_id": coordination_id,
        "other_id": resolved_other,
        "safe_label": label,
        "status": str(coordination.get("status") or ""),
        "revision": int(coordination.get("revision", 1) or 1),
        "calendar_event_id": str(coordination.get("calendar_event_id") or ""),
        "created_at": now,
        "expires_at": now + REFERENCE_TTL_SECONDS,
    }
    with _LOCK:
        _MEMORY[_memory_key(user_id, room_id)] = dict(record)
    collection = _collection()
    try:
        if collection is not None:
            collection.update_one(
                {"user_id": user_id, "room_id": room_id, "reference_key": REFERENCE_KEY},
                {"$set": record},
                upsert=True,
            )
    except Exception:
        pass
    return record


def clear_reference(user_id: str, room_id: str) -> None:
    if not user_id or not room_id:
        return
    with _LOCK:
        _MEMORY.pop(_memory_key(user_id, room_id), None)
    collection = _collection()
    try:
        if collection is not None:
            collection.delete_one({
                "user_id": user_id,
                "room_id": room_id,
                "reference_key": REFERENCE_KEY,
            })
    except Exception:
        pass


def _load_record(user_id: str, room_id: str) -> dict[str, Any] | None:
    record: dict[str, Any] | None = None
    collection = _collection()
    try:
        if collection is not None:
            record = collection.find_one({
                "user_id": user_id,
                "room_id": room_id,
                "reference_key": REFERENCE_KEY,
            })
    except Exception:
        record = None
    if not record:
        with _LOCK:
            record = dict(_MEMORY.get(_memory_key(user_id, room_id)) or {}) or None
    return record


def get_reference(user_id: str, room_id: str) -> dict[str, Any] | None:
    """Return a fresh canonical reference, or clear an expired/stale one."""
    if not user_id or not room_id:
        return None
    record = _load_record(user_id, room_id)
    if not record:
        return None
    if float(record.get("expires_at", 0) or 0) <= time.time():
        clear_reference(user_id, room_id)
        return None
    other_id = str(record.get("other_id") or "")
    coordination_id = str(record.get("coordination_id") or "")
    if not other_id or not coordination_id:
        clear_reference(user_id, room_id)
        return None
    try:
        match = matches_coll.find_one(verified_accepted_match_query(user_id, other_id))
    except Exception:
        match = None
    coordination = (match or {}).get("date_coordination") or {}
    status = str(coordination.get("status") or "")
    if (
        not match
        or str(match.get("_id") or "") != str(record.get("match_id") or str(match.get("_id") or ""))
        or str(coordination.get("coordination_id") or "") != coordination_id
        or status not in CANCELLABLE_STATUSES
    ):
        clear_reference(user_id, room_id)
        return None
    event_revision = None
    event_id = str(coordination.get("calendar_event_id") or "")
    if event_id:
        try:
            event = calendar_events_coll.find_one({
                "event_id": event_id,
                "source_type": "date",
                "participants": {"$all": [user_id, other_id]},
            }) or {}
            if event:
                event_revision = int(event.get("revision", 1) or 1)
        except Exception:
            event_revision = None
    fresh = {
        **record,
        "match_id": str(match.get("_id") or record.get("match_id") or ""),
        "other_id": other_id,
        "safe_label": str(record.get("safe_label") or _safe_display_name(other_id) or "對方")[:30],
        "status": status,
        "revision": int(coordination.get("revision", 1) or 1),
        "calendar_event_id": event_id,
        "event_revision": event_revision,
    }
    with _LOCK:
        _MEMORY[_memory_key(user_id, room_id)] = dict(fresh)
    return fresh


def public_projection(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    remaining = max(
        0,
        min(REFERENCE_TTL_SECONDS, int(float(record.get("expires_at", 0) or 0) - time.time())),
    )
    status = str(record.get("status") or "")
    return {
        "kind": "date_coordination",
        "display_name": str(record.get("safe_label") or "對方")[:30],
        "status": status,
        "allowed_actions": ["cancel"] if status in CANCELLABLE_STATUSES else [],
        "created_at": record.get("created_at"),
        "expires_in_seconds": remaining,
    }


def authority_projection(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return executor-only target and CAS fields."""
    if not record:
        return None
    return {
        "kind": "date_coordination",
        "match_id": str(record.get("match_id") or ""),
        "coordination_id": str(record.get("coordination_id") or ""),
        "other_id": str(record.get("other_id") or ""),
        "expected_status": str(record.get("status") or ""),
        "expected_revision": int(record.get("revision", 0) or 0),
        "expected_coordination_revision": int(record.get("revision", 0) or 0),
        "expected_event_revision": record.get("event_revision"),
        "calendar_event_id": str(record.get("calendar_event_id") or ""),
        "safe_label": str(record.get("safe_label") or "對方")[:30],
    }


def _other_participant(match: dict[str, Any], user_id: str) -> str:
    return str(
        match.get("to_user")
        if match.get("from_user") == user_id
        else match.get("from_user") or ""
    )


def _sort_timestamp(match: dict[str, Any]) -> float:
    value = match.get("updated_at") or match.get("created_at") or 0
    if hasattr(value, "timestamp"):
        try:
            return float(value.timestamp())
        except (TypeError, ValueError, OSError):
            return 0.0
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def date_coordination_summary(
    user_id: str, *, max_cards: int = MAX_PUBLIC_SUMMARY_CARDS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return a bounded public date-card summary and private authorities.

    Only cards that are still valid for the public cancellation workflow are
    counted. The public side contains display labels, statuses and action
    names; IDs, revisions and participant identifiers remain in the second
    return value attached to the executor-only turn state.
    """
    owner = str(user_id or "").strip()
    safe_limit = max(1, min(int(max_cards or MAX_PUBLIC_SUMMARY_CARDS), 3))
    if not owner:
        return {"count": 0, "total_count": 0, "cards": [], "truncated": False}, []
    try:
        rows = list(matches_coll.find(verified_accepted_match_query(owner)))
    except Exception:
        rows = []
    rows = [
        row for row in rows
        if isinstance(row, dict)
        and str((row.get("date_coordination") or {}).get("status") or "") in CANCELLABLE_STATUSES
    ]
    rows.sort(key=_sort_timestamp, reverse=True)
    authorities: list[dict[str, Any]] = []
    cards: list[dict[str, Any]] = []
    for row in rows[:safe_limit]:
        coordination = row.get("date_coordination") or {}
        other = _other_participant(row, owner)
        status = str(coordination.get("status") or "")
        event_id = str(coordination.get("calendar_event_id") or "")
        event_revision = None
        if event_id and other:
            try:
                event = calendar_events_coll.find_one({
                    "event_id": event_id,
                    "source_type": "date",
                    "participants": {"$all": [owner, other]},
                }) or {}
                if event:
                    event_revision = int(event.get("revision", 1) or 1)
            except Exception:
                event_revision = None
        label = _safe_display_name(other)
        cards.append({
            "display_name": label,
            "status": status,
            "allowed_actions": ["cancel"],
        })
        authorities.append({
            "kind": "date_coordination",
            "match_id": str(row.get("_id") or ""),
            "coordination_id": str(coordination.get("coordination_id") or ""),
            "other_id": other,
            "expected_status": status,
            "expected_revision": int(coordination.get("revision", 1) or 1),
            "expected_coordination_revision": int(coordination.get("revision", 1) or 1),
            "expected_event_revision": event_revision,
            "calendar_event_id": event_id,
            "safe_label": label,
        })
    total = len(rows)
    return {
        "count": total,
        "total_count": total,
        "cards": cards,
        "truncated": total > len(cards),
    }, authorities


# Short aliases keep the storage contract easy to discover for callers/tests.
remember_reference = remember_date_coordination
