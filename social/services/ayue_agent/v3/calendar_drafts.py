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
    resolved_target: Any | None = None,
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
    if resolved_target:
        target = resolved_target.model_dump() if hasattr(resolved_target, "model_dump") else dict(resolved_target)
        # Store only a safe projection in the draft.  The authority-bearing
        # event id/revision live in calendar_references.draft_target.
        record["resolved_target"] = {
            "bound": True,
            "label": str(target.get("safe_label") or "這筆行程")[:180],
            "reference_key": "draft_target",
        }
    else:
        # Never leave a previous server-owned target attached to a new
        # clarification draft.  The command draft itself is authority-free,
        # so a missing binding means that the next turn must resolve again.
        record["resolved_target"] = None
        try:
            from .calendar_references import clear_reference
            clear_reference(user_id, reference_key="draft_target")
        except Exception:
            pass
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
    projection = {
        "action": command.get("action"),
        "fields": {
            key: value for key, value in command.items()
            if key in {
                "title", "date", "end_date", "all_day", "start_time", "end_time",
                "duration_minutes", "timezone", "location", "notes",
            }
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
    resolved = record.get("resolved_target") or {}
    if isinstance(resolved, dict) and resolved.get("bound"):
        projection["resolved_target"] = {
            "bound": True,
            "label": str(resolved.get("label") or "這筆行程")[:180],
        }
    return projection


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
    if _distinct_create_request(command, record):
        # A new create with both a different activity and date is a fresh
        # request.  The model's draft_mode is only guidance and must not make
        # the previous create's time/location fields authoritative.
        values = command.model_dump(exclude_none=True)
        if str(values.get("draft_mode") or "none") == "continue":
            prior = dict((record.get("command") or {}))
            # A provider may echo fields from the visible draft while calling
            # a new create a continuation.  Equal values are the only safe
            # ones to identify as copied; a genuinely different new value is
            # retained for the new request.
            for key in (
                "end_date", "all_day", "start_time", "end_time", "duration_minutes", "timezone",
                "location", "notes",
            ):
                if key in values and key in prior and values[key] == prior[key]:
                    values.pop(key, None)
            from .calendar_commands import CalendarCommand
            return CalendarCommand.model_validate(values)
        return command
    mode = str(getattr(command, "draft_mode", "none") or "none")
    prior = dict(record.get("command") or {})
    values = command.model_dump(exclude_none=True)
    incoming_target_hint = str(values.get("target_hint") or "").strip()
    incoming_target_reference = str(values.get("target_reference") or "").strip()
    incoming_target_selector = dict(values.get("target_selector") or {})
    prior_target_hint = str(prior.get("target_hint") or "").strip()
    prior_target_selector = dict(prior.get("target_selector") or {})
    has_resolved_target = bool((record.get("resolved_target") or {}).get("bound"))
    same_target_hint = bool(incoming_target_hint and prior_target_hint and incoming_target_hint == prior_target_hint)
    same_target_selector = bool(
        incoming_target_selector and prior_target_selector
        and incoming_target_selector == prior_target_selector
    )
    explicit_new_target = bool(
        incoming_target_reference
        or (incoming_target_hint and not same_target_hint)
        or (incoming_target_selector and not same_target_selector)
    )
    if has_resolved_target and explicit_new_target:
        # A new selector is a new mutation target, not a continuation of the
        # old event's pending changes.
        return command
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
    if values.get("all_day") is True:
        effective_provided_fields.update({"start_time", "end_time"})
    # draft_mode is model-produced guidance, not authority to discard a clear
    # missing-field continuation.  Preserve the draft when the new command
    # supplies only fields it previously reported missing, or those fields
    # plus an explicit correction to an already-present field.
    clear_continuation = bool(missing_fields and provided_fields) and (
        effective_provided_fields <= missing_fields
        or (
            effective_provided_fields & missing_fields
            and (effective_provided_fields - missing_fields) <= set(prior) | {
                "duration_minutes", "all_day", "end_date",
            }
        )
    )
    # Once preflight has uniquely resolved an update/cancel target, a
    # selector-free same-action turn is a continuation even when the
    # clarification has missing_fields=[] or the provider says replace.
    if has_resolved_target and not explicit_new_target:
        clear_continuation = True
    if (
        str(getattr(command, "action", "")) == "create"
        and (
            {"title", "date", "start_time", "end_time"} <= effective_provided_fields
            or (
                values.get("all_day") is True
                and {"title", "date"} <= effective_provided_fields
            )
        )
    ):
        # A complete command is a genuinely actionable replacement, even if
        # its fields happen to overlap the old draft.
        clear_continuation = False
    if mode == "replace" and not clear_continuation:
        return command
    effective_mode = "continue" if clear_continuation else mode
    if effective_mode != "continue":
        required = (
            {"title", "date"}
            if values.get("all_day") is True
            else {"title", "date", "start_time", "end_time"}
        )
        if str(getattr(command, "action", "")) == "create" and required <= set(values):
            return command
    if clear_continuation:
        values["draft_mode"] = "continue"
        # The server-owned draft reference is used by Scheduler.  Do not
        # carry the old natural-language hint back into preflight and resolve
        # the same event a second time.
        if has_resolved_target and not explicit_new_target:
            values.pop("target_hint", None)
            values.pop("target_reference", None)
    if values.get("all_day") is True:
        # A typed all-day continuation replaces an incomplete timed interval;
        # stale clock fields must not be inherited from the draft.
        values.pop("start_time", None)
        values.pop("end_time", None)
        values.pop("duration_minutes", None)
    for key, value in prior.items():
        if key in {"action", "draft_mode"}:
            continue
        if has_resolved_target and not explicit_new_target and key in {"target_hint", "target_reference"}:
            continue
        if has_resolved_target and not explicit_new_target and key == "target_selector":
            continue
        if key == "target_hint" and values.get("target_reference"):
            continue
        if key == "target_reference" and (values.get("target_hint") or values.get("target_selector")):
            continue
        if key == "target_selector" and values.get("target_reference"):
            continue
        if values.get("all_day") is True and key in {
            "start_time", "end_time", "duration_minutes",
        }:
            continue
        if key not in values or values.get(key) in (None, "", []):
            values[key] = value
    # Target selectors are one logical field.  A continuation may replace a
    # recent reference with a natural-language hint (or vice versa), but the
    # two must never be merged into an invalid command.
    if values.get("target_hint"):
        values.pop("target_reference", None)
    if values.get("target_selector"):
        values.pop("target_reference", None)
    elif values.get("target_reference"):
        values.pop("target_hint", None)
    # Import lazily so this module stays usable by context construction without
    # introducing a module cycle during application startup.
    from .calendar_commands import CalendarCommand
    return CalendarCommand.model_validate(values)


def _distinct_create_request(command: Any, record: dict[str, Any] | None) -> bool:
    """Identify a clearly new create instead of a missing-field continuation.

    A same-title date change is still allowed as an explicit correction to an
    existing draft.  Requiring both title and date to differ avoids treating
    that correction as a new request while preventing a new activity/date
    pair from inheriting fields from the old draft.
    """
    if str(getattr(command, "action", "")) != "create":
        return False
    prior_command = (record or {}).get("command") or {}
    if str(prior_command.get("action") or "") != "create":
        return False
    values = command.model_dump(exclude_none=True) if hasattr(command, "model_dump") else dict(command or {})
    incoming_title = str(values.get("title") or "").strip()
    incoming_date = str(values.get("date") or "").strip()
    prior_title = str(prior_command.get("title") or "").strip()
    prior_date = str(prior_command.get("date") or "").strip()
    return bool(
        incoming_title and incoming_date and prior_title and prior_date
        and incoming_title != prior_title
        and incoming_date != prior_date
    )


def resolved_target_replaced(command: Any, record: dict[str, Any] | None) -> bool:
    """Return whether an incoming command explicitly replaces the draft."""
    if _distinct_create_request(command, record):
        return True
    if not record or not (record.get("resolved_target") or {}).get("bound"):
        return False
    values = command.model_dump(exclude_none=True) if hasattr(command, "model_dump") else dict(command or {})
    target_reference = str(values.get("target_reference") or "").strip()
    target_hint = str(values.get("target_hint") or "").strip()
    target_selector = dict(values.get("target_selector") or {})
    prior_hint = str((record.get("command") or {}).get("target_hint") or "").strip()
    prior_selector = dict((record.get("command") or {}).get("target_selector") or {})
    return bool(
        target_reference
        or (target_hint and target_hint != prior_hint)
        or (target_selector and target_selector != prior_selector)
    )


def clear_draft(user_id: str) -> None:
    with _LOCK:
        _MEMORY.pop(user_id, None)
    collection = _collection()
    try:
        if collection is not None:
            collection.delete_one({"user_id": user_id})
    except Exception:
        pass
    # Draft target authority is stored separately from the authority-free
    # command draft and must expire/clear together with it.
    try:
        from .calendar_references import clear_reference
        clear_reference(user_id, reference_key="draft_target")
    except Exception:
        pass
