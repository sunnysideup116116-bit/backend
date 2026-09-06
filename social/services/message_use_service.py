"""Server-owned reuse policy for saved Public Ayue messages.

One owner message can feed several asynchronous paths: profile extraction,
durable memory, conversation compaction, and proactive care.  This module
stores a small provenance projection on the source message so every path can
make the same decision without copying the message body into another store.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from bson.objectid import ObjectId

from database import messages_coll


MESSAGE_USE_POLICY_VERSION = "message-use-v1"
_NO_MEMORY_RE = re.compile(r"(?:不要記|別記|不用記|不必記)")
_CONTROL_ONLY_MESSAGES = frozenset({
    "確認", "取消", "好", "好的", "可以", "不要", "不用", "ok", "yes", "no",
})


class MessageUse(str, Enum):
    """The classifications consumed by asynchronous owner-data surfaces."""

    ORDINARY = "ordinary"
    CALENDAR_OPERATION = "calendar_operation"
    ASSESSMENT = "assessment"
    NO_MEMORY = "no_memory"
    UNKNOWN = "unknown"


_EXCLUDED_USES = frozenset({
    MessageUse.CALENDAR_OPERATION.value,
    MessageUse.ASSESSMENT.value,
    MessageUse.NO_MEMORY.value,
    MessageUse.UNKNOWN.value,
})


def _as_use(value: Any) -> MessageUse:
    if isinstance(value, MessageUse):
        return value
    try:
        return MessageUse(str(value or "").strip().lower())
    except ValueError:
        return MessageUse.UNKNOWN


def use_from_turn(
    profile_write_reason: Any = None,
    *,
    owner_message: str = "",
    calendar_operation: bool = False,
) -> MessageUse:
    """Resolve a server-owned turn result into a reusable message use.

    Calendar and assessment reasons are produced by V3 server branches.  A
    short no-memory phrase is a closed protocol and is checked here as a
    defense-in-depth gate.  Any unrecognised control reason fails closed.
    """
    reason = str(profile_write_reason or "").strip().lower()
    if reason in {"assessment", "profile_assessment"}:
        return MessageUse.ASSESSMENT
    if calendar_operation or reason.startswith("calendar"):
        return MessageUse.CALENDAR_OPERATION
    if _NO_MEMORY_RE.search(str(owner_message or "")):
        return MessageUse.NO_MEMORY
    if str(owner_message or "").strip().lower() in _CONTROL_ONLY_MESSAGES:
        return MessageUse.UNKNOWN
    if reason in {"", "casual", "ordinary", "global"}:
        return MessageUse.ORDINARY
    if reason in {item.value for item in MessageUse}:
        return _as_use(reason)
    if reason in {
        "place_reference_clarification",
        "confirmation",
        "confirmation_missing",
        "confirmation_cancelled",
    }:
        return MessageUse.UNKNOWN
    return MessageUse.UNKNOWN


def metadata_for_use(use: MessageUse | str, *, reason: str = "") -> dict[str, Any]:
    normalized = _as_use(use)
    return {
        "version": MESSAGE_USE_POLICY_VERSION,
        "use": normalized.value,
        "status": "excluded" if normalized.value in _EXCLUDED_USES else "allowed",
        "reason": str(reason or normalized.value)[:80],
    }


def message_use(message: dict[str, Any] | None) -> MessageUse:
    """Read a persisted marker; unmarked legacy messages fail closed."""
    metadata = (message or {}).get("metadata") or {}
    projection = metadata.get("message_use") if isinstance(metadata, dict) else None
    if not isinstance(projection, dict):
        return MessageUse.UNKNOWN
    if projection.get("version") != MESSAGE_USE_POLICY_VERSION:
        return MessageUse.UNKNOWN
    return _as_use(projection.get("use"))


def is_reusable_for_profile(message: dict[str, Any] | None) -> bool:
    return message_use(message) is MessageUse.ORDINARY


def is_reusable_for_compaction(message: dict[str, Any] | None) -> bool:
    return message_use(message) is MessageUse.ORDINARY


def is_reusable_for_care(message: dict[str, Any] | None) -> bool:
    return message_use(message) is MessageUse.ORDINARY


def mark_message_use(
    message_id: str | None,
    *,
    user_id: str,
    room_id: str,
    use: MessageUse | str,
    reason: str = "",
    sender_id: str | None = None,
) -> bool:
    """Persist a marker only on the owned source message."""
    try:
        object_id = ObjectId(str(message_id or ""))
    except Exception:
        return False
    query: dict[str, Any] = {"_id": object_id, "room_id": room_id}
    if sender_id is None:
        query["sender_id"] = user_id
    else:
        query["sender_id"] = sender_id
    result = messages_coll.update_one(
        query,
        {"$set": {"metadata.message_use": metadata_for_use(use, reason=reason)}},
    )
    return bool(
        getattr(result, "matched_count", 0)
        or getattr(result, "modified_count", 0)
    )


def mark_message_use_from_turn(
    message_id: str | None,
    *,
    user_id: str,
    room_id: str,
    profile_write_reason: Any = None,
    owner_message: str = "",
    calendar_operation: bool = False,
) -> MessageUse:
    """Classify and persist one saved owner message."""
    use = use_from_turn(
        profile_write_reason,
        owner_message=owner_message,
        calendar_operation=calendar_operation,
    )
    try:
        mark_message_use(
            message_id,
            user_id=user_id,
            room_id=room_id,
            use=use,
            reason=str(profile_write_reason or use.value),
        )
    except Exception:
        # The marker is auxiliary safety metadata.  An unavailable database
        # must not turn a completed chat response into a second failure.
        pass
    return use


__all__ = [
    "MESSAGE_USE_POLICY_VERSION",
    "MessageUse",
    "is_reusable_for_care",
    "is_reusable_for_compaction",
    "is_reusable_for_profile",
    "mark_message_use",
    "mark_message_use_from_turn",
    "message_use",
    "metadata_for_use",
    "use_from_turn",
]
