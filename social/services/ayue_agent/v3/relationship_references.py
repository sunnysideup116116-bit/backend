"""Short-lived, server-owned accepted-contact references for Public V3."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from database import matches_coll
from services.match_state_service import verified_accepted_match_query


REFERENCE_TTL_SECONDS = 15 * 60
REFERENCE_KEY = "recent_contact"
_COLLECTION = None
_LOCK = threading.RLock()
_MEMORY: dict[tuple[str, str], dict[str, Any]] = {}


def clear_runtime_state() -> None:
    """Clear the process-local fallback used by tests and demo mode."""
    with _LOCK:
        _MEMORY.clear()


def _collection() -> Any:
    global _COLLECTION
    if os.getenv("AYUE_TEST_MODE", "off").strip().lower() in {"1", "true", "on"}:
        return None
    if os.getenv("AYUE_RELATIONSHIP_REFERENCE_MONGO", "on").strip().lower() not in {"1", "true", "on"}:
        return None
    if _COLLECTION is None:
        try:
            from database import db
            _COLLECTION = db["v3_relationship_references"]
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


def remember_contact(user_id: str, other_id: str, safe_label: str) -> dict[str, Any]:
    """Remember only a validated accepted contact; IDs never leave this store."""
    if not user_id or not other_id or not safe_label:
        return {}
    now = time.time()
    record = {
        "user_id": user_id,
        "reference_key": REFERENCE_KEY,
        "other_id": other_id,
        "safe_label": str(safe_label)[:30],
        "created_at": now,
        "expires_at": now + REFERENCE_TTL_SECONDS,
    }
    with _LOCK:
        _MEMORY[(user_id, REFERENCE_KEY)] = dict(record)
    collection = _collection()
    try:
        if collection is not None:
            collection.update_one(
                {"user_id": user_id, "reference_key": REFERENCE_KEY},
                {"$set": record},
                upsert=True,
            )
    except Exception:
        pass
    return record


def clear_reference(user_id: str) -> None:
    with _LOCK:
        _MEMORY.pop((user_id, REFERENCE_KEY), None)
    collection = _collection()
    try:
        if collection is not None:
            collection.delete_one({"user_id": user_id, "reference_key": REFERENCE_KEY})
    except Exception:
        pass


def get_reference(user_id: str) -> dict[str, Any] | None:
    now = time.time()
    record: dict[str, Any] | None = None
    collection = _collection()
    try:
        if collection is not None:
            record = collection.find_one({"user_id": user_id, "reference_key": REFERENCE_KEY})
    except Exception:
        record = None
    if not record:
        with _LOCK:
            record = dict(_MEMORY.get((user_id, REFERENCE_KEY)) or {}) or None
    if not record:
        return None
    if float(record.get("expires_at", 0) or 0) <= now:
        clear_reference(user_id)
        return None
    other_id = str(record.get("other_id") or "")
    try:
        accepted = matches_coll.find_one(
            verified_accepted_match_query(user_id, other_id),
            {"_id": 1},
        )
    except Exception:
        accepted = None
    if not accepted:
        clear_reference(user_id)
        return None
    return dict(record)


def public_projection(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    remaining = max(0, min(REFERENCE_TTL_SECONDS, int(float(record.get("expires_at", 0) or 0) - time.time())))
    return {
        "display_name": str(record.get("safe_label") or "對方")[:30],
        "expires_in_seconds": remaining,
    }

