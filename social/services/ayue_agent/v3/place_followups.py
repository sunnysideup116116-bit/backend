"""Room-scoped state for incomplete place-to-calendar follow-ups.

Place presentations identify a venue; this store keeps the user's typed
calendar fields while that venue is being clarified.  It is deliberately
separate from the older user-scoped Calendar draft store so two AI rooms owned
by the same user cannot share an unfinished place request.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any


PLACE_FOLLOWUP_VERSION = "v3-place-followup-v1"
_PLACE_REFERENCE_RE = re.compile(r"place_ref_[a-f0-9]{24}")
_ABANDONMENT_RE = re.compile(
    r"(?:算了不用|算了不要|先算了|不用了|先不要了)|"
    r"(?:不用|不要|先別|不必)(?:再)?(?:幫我)?(?:排(?!隊)|安排|加到|加入|記進)(?:行程|日曆|行事曆)?|"
    r"(?:取消|放棄)(?:這個|這次|剛剛)?(?:安排|行程|加入行事曆)"
)
_COLLECTION = None
_LOCK = threading.RLock()
_MEMORY: dict[tuple[str, str], dict[str, Any]] = {}
_LOGGER = logging.getLogger(__name__)


class PlaceFollowupPersistenceError(RuntimeError):
    """Raised when a room-scoped place follow-up cannot be persisted."""


def _test_mode_enabled() -> bool:
    return os.getenv("AYUE_TEST_MODE", "off").strip().lower() in {
        "1", "true", "on",
    }


def _collection() -> Any:
    global _COLLECTION
    if _test_mode_enabled():
        return None
    if os.getenv("AYUE_PLACE_FOLLOWUP_MONGO", "on").strip().lower() not in {
        "1", "true", "on",
    }:
        return None
    if _COLLECTION is None:
        try:
            from database import db
            _COLLECTION = db["v3_place_followups"]
        except Exception as exc:
            raise PlaceFollowupPersistenceError("place_followup_store_unavailable") from exc
    return _COLLECTION


def ensure_indexes() -> None:
    try:
        collection = _collection()
    except PlaceFollowupPersistenceError as exc:
        _LOGGER.warning("V3 place follow-up indexes skipped: %s", exc)
        return
    if collection is None:
        return
    try:
        collection.create_index(
            [("user_id", 1), ("room_id", 1)],
            unique=True,
            name="v3_place_followups_owner_room",
        )
    except Exception as exc:
        _LOGGER.warning("Unable to create V3 place follow-up indexes: %s", type(exc).__name__)


def clear_runtime_state() -> None:
    """Clear only the process-local test store."""
    with _LOCK:
        _MEMORY.clear()


def _safe_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def abandonment_requested(message: Any) -> bool:
    """Recognize an explicit abandonment only while a typed follow-up exists."""
    compact = re.sub(r"[\s，,。.!！?？；;：:]", "", str(message or ""))
    return bool(_ABANDONMENT_RE.search(compact))


def _safe_command_values(command: Any) -> dict[str, Any]:
    values = command.model_dump(exclude_none=True) if hasattr(command, "model_dump") else dict(command or {})
    allowed = {
        "action", "title", "date", "end_date", "all_day", "start_time", "end_time",
        "duration_minutes", "timezone", "location", "notes", "draft_mode",
    }
    return {key: value for key, value in values.items() if key in allowed}


def _safe_resolution(resolution: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(resolution, dict) or str(resolution.get("status") or "") != "resolved":
        return None
    reference = _safe_text(resolution.get("reference"), 40)
    label = _safe_text(resolution.get("label"), 80)
    if not _PLACE_REFERENCE_RE.fullmatch(reference) or not label:
        return None
    result: dict[str, Any] = {"reference": reference, "label": label}
    try:
        ordinal = int(resolution.get("ordinal"))
    except (TypeError, ValueError):
        ordinal = 0
    if 1 <= ordinal <= 8:
        result["ordinal"] = ordinal
    origin = _safe_text(resolution.get("origin_run_id"), 160)
    if origin:
        result["origin_run_id"] = origin
    return result


def _trusted_resolution(
    user_id: str,
    room_id: str,
    resolution: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Sanitize a resolution and recover its private snapshot origin.

    The normal public projection intentionally omits the origin before it is
    placed in model context.  The server can still recover that private value
    from the opaque reference when persisting a room-scoped draft.
    """
    safe = _safe_resolution(resolution)
    if safe is None or safe.get("origin_run_id"):
        return safe
    try:
        from .place_references import get_candidate_origin

        origin = get_candidate_origin(user_id, room_id, str(safe.get("reference") or ""))
    except Exception:
        origin = None
    if origin:
        safe["origin_run_id"] = _safe_text(origin, 160)
    return safe


def save_followup(
    user_id: str,
    room_id: str,
    command: Any,
    *,
    missing_fields: list[str] | None = None,
    candidate_options: list[dict[str, Any]] | None = None,
    resolution: dict[str, Any] | None = None,
    source_message_id: str | None = None,
) -> dict[str, Any]:
    """Persist one incomplete create request for exactly one user and room."""
    now = time.time()
    key = (str(user_id), str(room_id))
    resolved_place = _trusted_resolution(user_id, room_id, resolution)
    record: dict[str, Any] = {
        "version": PLACE_FOLLOWUP_VERSION,
        "user_id": str(user_id),
        "room_id": str(room_id),
        "command": _safe_command_values(command),
        "missing_fields": [str(item)[:40] for item in (missing_fields or [])[:8] if str(item).strip()],
        "candidate_options": [
            {
                "label": _safe_text(item.get("label"), 80),
                "address_summary": _safe_text(item.get("address_summary"), 120),
            }
            for item in (candidate_options or [])[:8]
            if isinstance(item, dict) and _safe_text(item.get("label"), 80)
        ],
        "resolved_place": resolved_place,
        "source_message_id": _safe_text(source_message_id, 160) or None,
        "created_at": now,
    }
    if _test_mode_enabled():
        with _LOCK:
            previous = _MEMORY.get(key)
            if resolution is None and isinstance(previous, dict):
                prior_resolved = previous.get("resolved_place")
                if isinstance(prior_resolved, dict):
                    record["resolved_place"] = dict(prior_resolved)
            _MEMORY[key] = dict(record)
        return dict(record)
    collection = _collection()
    if collection is None:
        raise PlaceFollowupPersistenceError("place_followup_store_unavailable")
    query = {"user_id": key[0], "room_id": key[1]}
    if resolution is None:
        try:
            previous = collection.find_one(query)
        except Exception as exc:
            raise PlaceFollowupPersistenceError("place_followup_store_unavailable") from exc
        if isinstance(previous, dict):
            prior_resolved = previous.get("resolved_place")
            if isinstance(prior_resolved, dict):
                record["resolved_place"] = dict(prior_resolved)
    try:
        update_record = dict(record)
        if resolution is None and not isinstance(record.get("resolved_place"), dict):
            # Do not turn a missing place field into an explicit null on a
            # partial update.  Mongo's $set then preserves any prior trusted
            # binding if one exists.
            update_record.pop("resolved_place", None)
        collection.update_one(
            query,
            {"$set": update_record},
            upsert=True,
        )
    except Exception as exc:
        raise PlaceFollowupPersistenceError("place_followup_store_write_failed") from exc
    return dict(record)


def get_followup(user_id: str, room_id: str) -> dict[str, Any] | None:
    key = (str(user_id), str(room_id))
    record: dict[str, Any] | None = None
    if _test_mode_enabled():
        with _LOCK:
            raw = _MEMORY.get(key)
            record = dict(raw) if raw else None
    else:
        collection = _collection()
        if collection is None:
            raise PlaceFollowupPersistenceError("place_followup_store_unavailable")
        try:
            raw = collection.find_one({"user_id": key[0], "room_id": key[1]})
            record = dict(raw) if isinstance(raw, dict) else None
        except Exception as exc:
            raise PlaceFollowupPersistenceError("place_followup_store_unavailable") from exc
    if not record:
        return None
    return record


def public_projection(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    command = dict(record.get("command") or {})
    projection: dict[str, Any] = {
        "version": str(record.get("version") or PLACE_FOLLOWUP_VERSION),
        "fields": {
            key: value for key, value in command.items()
            if key in {
                "title", "date", "end_date", "all_day", "start_time", "end_time",
                "duration_minutes", "timezone", "location", "notes",
            }
        },
        "missing_fields": [str(item)[:40] for item in (record.get("missing_fields") or [])[:8]],
        "candidate_options": [
            {
                "label": _safe_text(item.get("label"), 80),
                "address_summary": _safe_text(item.get("address_summary"), 120),
            }
            for item in (record.get("candidate_options") or [])[:8]
            if isinstance(item, dict) and _safe_text(item.get("label"), 80)
        ],
    }
    resolved = record.get("resolved_place")
    if isinstance(resolved, dict) and resolved.get("label"):
        projection["resolved_place"] = {
            key: resolved[key]
            for key in ("reference", "ordinal", "label")
            if resolved.get(key) not in (None, "")
        }
    return projection


def merge_command(command: Any, record: dict[str, Any] | None) -> Any:
    """Merge explicit fields from the current create into the saved follow-up."""
    if not record or str(getattr(command, "action", "")) != "create":
        return command
    if str(getattr(command, "draft_mode", "none") or "none") == "replace":
        return command
    values = _safe_command_values(command)
    prior = dict(record.get("command") or {})
    for key, value in prior.items():
        if key not in values or values.get(key) in (None, "", []):
            values[key] = value
    resolved_place = record.get("resolved_place")
    trusted_label = (
        _safe_text(resolved_place.get("label"), 80)
        if isinstance(resolved_place, dict) else ""
    )
    if trusted_label:
        # A provider or model cannot replace a place already bound to this
        # room-scoped draft merely by copying a different title into a later
        # continuation.  An explicit new selection is revalidated by Calendar
        # runtime after this merge.
        values["title"] = trusted_label
        values["location"] = trusted_label
    values["draft_mode"] = "continue"
    return command.__class__.model_validate(values)


def clear_followup(user_id: str, room_id: str) -> None:
    key = (str(user_id), str(room_id))
    if _test_mode_enabled():
        with _LOCK:
            _MEMORY.pop(key, None)
        return
    try:
        collection = _collection()
    except PlaceFollowupPersistenceError:
        return
    if collection is None:
        return
    try:
        collection.delete_one({"user_id": key[0], "room_id": key[1]})
    except Exception as exc:
        _LOGGER.warning("Unable to clear V3 place follow-up: %s", type(exc).__name__)


__all__ = [
    "PLACE_FOLLOWUP_VERSION",
    "PlaceFollowupPersistenceError",
    "abandonment_requested",
    "clear_followup",
    "clear_runtime_state",
    "ensure_indexes",
    "get_followup",
    "merge_command",
    "public_projection",
    "save_followup",
]
