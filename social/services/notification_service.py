"""Account-scoped notification preferences, presence, and unread state.

MongoDB chat messages remain the source of truth.  This module projects each
recipient-visible message into a small notification event, records unread
state, and decides whether a system push should be attempted.  Delivery to
Appwrite Messaging stays in :mod:`services.push_service`.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import time
from typing import Iterable

from pymongo.errors import DuplicateKeyError

from database import (
    notification_presence_coll,
    notification_threads_coll,
    profiles_coll,
)


PAIR = "pair"
MEDIATOR_PRIVATE = "mediator_private"
PUBLIC_AYUE = "public_ayue"
NONE = "none"
VALID_SURFACES = {PAIR, MEDIATOR_PRIVATE, PUBLIC_AYUE, NONE}

ACTIVE_WINDOW_SECONDS = 90.0
PRESENCE_RETENTION_SECONDS = 300.0
BODY_MAX = 160


@dataclass(frozen=True)
class NotificationEvent:
    recipient_id: str
    surface: str
    conversation_id: str
    other_user_id: str
    title: str
    body: str
    data: dict[str, str]
    tag: str
    message_id: str
    timestamp: float


def _thread_id(user_id: str, surface: str, conversation_id: str) -> str:
    raw = f"{user_id}\x00{surface}\x00{conversation_id}".encode("utf-8")
    return "notification-thread:" + hashlib.sha256(raw).hexdigest()


def _presence_id(user_id: str, session_id: str) -> str:
    raw = f"{user_id}\x00{session_id}".encode("utf-8")
    return "notification-presence:" + hashlib.sha256(raw).hexdigest()


def _chat_tag(surface: str, conversation_id: str) -> str:
    digest = hashlib.sha256(f"{surface}:{conversation_id}".encode("utf-8")).hexdigest()
    return f"chat-{digest[:24]}"


def _display_name(user_id: str) -> str:
    try:
        profile = profiles_coll.find_one(
            {"user_id": user_id},
            {"_id": 0, "display_name": 1, "nickname": 1, "name": 1},
        ) or {}
    except Exception:
        profile = {}
    value = str(
        profile.get("display_name")
        or profile.get("nickname")
        or profile.get("name")
        or ""
    ).strip()
    if value and not value.startswith(("seed_user_", "demo_user", "user_")):
        return value[:30]
    return "對方"


def _other_participant(room_id: str, participant_id: str) -> str | None:
    """Recover the other participant without splitting IDs on underscores."""
    if not room_id or not participant_id:
        return None
    prefix = participant_id + "_"
    if room_id.startswith(prefix):
        return room_id[len(prefix):]
    suffix = "_" + participant_id
    if room_id.endswith(suffix):
        return room_id[: -len(suffix)]
    return None


def _legacy_ai_owner(room_id: str) -> str | None:
    return _other_participant(room_id, "ai_assistant")


def _message_body(msg: dict) -> str:
    message_type = str(msg.get("message_type") or "text")
    if message_type == "image":
        return "傳了一張圖片"
    if message_type == "gif":
        return "傳來一張動圖"
    content = str(msg.get("content") or "").strip()
    return (content or "傳了一則訊息")[:BODY_MAX]


def _event(
    msg: dict,
    *,
    recipient_id: str,
    surface: str,
    conversation_id: str,
    other_user_id: str = "",
    title: str,
    ai_room_id: str = "",
) -> NotificationEvent:
    message_id = str(msg.get("message_id") or msg.get("_id") or "")
    timestamp = float(msg.get("timestamp") or time.time())
    sender_id = str(msg.get("sender_id") or "")
    message_type = str(msg.get("message_type") or "text")
    data = {
        "chat_surface": surface,
        "chat_conversation_id": conversation_id,
        "chat_contact_id": other_user_id,
        "chat_contact_name": _display_name(other_user_id) if other_user_id else "",
        "chat_ai_room_id": ai_room_id,
        "chat_message_kind": message_type,
        "chat_sender_id": sender_id,
    }
    return NotificationEvent(
        recipient_id=recipient_id,
        surface=surface,
        conversation_id=conversation_id,
        other_user_id=other_user_id,
        title=title,
        body=_message_body(msg),
        data=data,
        tag=_chat_tag(surface, conversation_id),
        message_id=message_id,
        timestamp=timestamp,
    )


def classify_message_notifications(msg: dict) -> list[NotificationEvent]:
    """Return recipient-specific notification events for one saved message."""
    room_id = str(msg.get("room_id") or "")
    sender_id = str(msg.get("sender_id") or "")
    metadata = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
    event_type = str(metadata.get("event_type") or "")
    if not room_id or not sender_id or msg.get("is_blocked"):
        return []
    if metadata.get("notification_eligible") is False or event_type == "blocked_notice":
        return []

    if room_id.startswith("mediator_private::"):
        parts = room_id.split("::", 2)
        if len(parts) != 3 or sender_id != "ai_assistant":
            return []
        owner_id, other_id = parts[1], parts[2]
        return [
            _event(
                msg,
                recipient_id=owner_id,
                surface=MEDIATOR_PRIVATE,
                conversation_id=room_id,
                other_user_id=other_id,
                title="阿月悄悄話",
            )
        ]

    if room_id.startswith("ai_room::"):
        parts = room_id.split("::", 2)
        if len(parts) != 3 or sender_id != "ai_assistant":
            return []
        owner_id = parts[1]
        return [
            _event(
                msg,
                recipient_id=owner_id,
                surface=PUBLIC_AYUE,
                conversation_id=room_id,
                title="阿月",
                ai_room_id=room_id,
            )
        ]

    legacy_owner = _legacy_ai_owner(room_id)
    if legacy_owner and sender_id == "ai_assistant":
        return [
            _event(
                msg,
                recipient_id=legacy_owner,
                surface=PUBLIC_AYUE,
                conversation_id=room_id,
                title="阿月",
            )
        ]
    if legacy_owner:
        # Owner-authored Public Ayue turns never notify the owner.
        return []

    if sender_id != "ai_assistant":
        receiver_id = _other_participant(room_id, sender_id)
        if not receiver_id:
            return []
        return [
            _event(
                msg,
                recipient_id=receiver_id,
                surface=PAIR,
                conversation_id=room_id,
                other_user_id=sender_id,
                title=_display_name(sender_id),
            )
        ]

    # Shared pair-room cards need an explicit recipient projection.  Date
    # coordination already carries both participants; other producers can use
    # notification_recipients without changing the public message shape.
    recipients = metadata.get("notification_recipients")
    if not isinstance(recipients, list):
        initiator = str(metadata.get("initiator_id") or "")
        invitee = str(metadata.get("invitee_id") or "")
        if event_type == "date_coordination_invite" and invitee:
            recipients = [invitee]
        elif initiator and invitee:
            recipients = [initiator, invitee]
        else:
            recipients = []
    participants = [str(item) for item in recipients if str(item)]
    events: list[NotificationEvent] = []
    for recipient_id in dict.fromkeys(participants):
        other_id = next((item for item in participants if item != recipient_id), "")
        events.append(
            _event(
                msg,
                recipient_id=recipient_id,
                surface=PAIR,
                conversation_id=room_id,
                other_user_id=other_id,
                title="阿月",
            )
        )
    return events


def get_notification_preferences(user_id: str) -> dict:
    try:
        doc = profiles_coll.find_one(
            {"user_id": user_id}, {"_id": 0, "notification_preferences": 1},
        ) or {}
    except Exception:
        doc = {}
    raw = doc.get("notification_preferences") or {}
    return {
        "global_enabled": raw.get("global_enabled", True) is not False,
        "public_ayue_enabled": raw.get("public_ayue_enabled", True) is not False,
        "muted_peer_ids": sorted({str(item) for item in raw.get("muted_peer_ids", []) if str(item)}),
        "muted_mediator_ids": sorted({str(item) for item in raw.get("muted_mediator_ids", []) if str(item)}),
    }


def update_notification_preference(
    user_id: str, scope: str, enabled: bool, target_id: str | None = None,
) -> dict:
    if scope == "global":
        update = {"$set": {"notification_preferences.global_enabled": bool(enabled)}}
    elif scope == "public_ayue":
        update = {"$set": {"notification_preferences.public_ayue_enabled": bool(enabled)}}
    elif scope in {"peer", "mediator_private"}:
        if not target_id:
            raise ValueError("target_id is required for scoped notification preferences")
        field = (
            "notification_preferences.muted_peer_ids"
            if scope == "peer"
            else "notification_preferences.muted_mediator_ids"
        )
        update = {"$pull": {field: target_id}} if enabled else {"$addToSet": {field: target_id}}
    else:
        raise ValueError("unsupported notification preference scope")
    profiles_coll.update_one({"user_id": user_id}, update, upsert=True)
    return get_notification_preferences(user_id)


def report_notification_presence(
    *,
    user_id: str,
    session_id: str,
    visible: bool,
    surface: str = NONE,
    conversation_id: str = "",
    other_user_id: str = "",
    now: float | None = None,
) -> None:
    current_time = float(now or time.time())
    document_id = _presence_id(user_id, session_id)
    if not visible:
        notification_presence_coll.delete_one({"_id": document_id})
    else:
        notification_presence_coll.update_one(
            {"_id": document_id},
            {"$set": {
                "user_id": user_id,
                "session_id": session_id,
                "visible": True,
                "surface": surface if surface in VALID_SURFACES else NONE,
                "conversation_id": conversation_id,
                "other_user_id": other_user_id,
                "updated_at": current_time,
            }},
            upsert=True,
        )
    try:
        notification_presence_coll.delete_many(
            {"updated_at": {"$lt": current_time - PRESENCE_RETENTION_SECONDS}},
        )
    except Exception:
        pass


def _active_contexts(user_id: str, *, now: float | None = None) -> list[dict]:
    current_time = float(now or time.time())
    try:
        return list(notification_presence_coll.find({
            "user_id": user_id,
            "visible": True,
            "updated_at": {"$gte": current_time - ACTIVE_WINDOW_SECONDS},
        }, {"_id": 0}))
    except Exception:
        return []


def _is_exact_surface_active(event: NotificationEvent, contexts: Iterable[dict]) -> bool:
    return any(
        str(item.get("surface") or "") == event.surface
        and str(item.get("conversation_id") or "") == event.conversation_id
        for item in contexts
    )


def _should_suppress_push(event: NotificationEvent, contexts: Iterable[dict]) -> bool:
    contexts = list(contexts)
    if _is_exact_surface_active(event, contexts):
        return True
    if event.surface != PAIR or not event.other_user_id:
        return False
    return any(
        str(item.get("surface") or "") == MEDIATOR_PRIVATE
        and str(item.get("other_user_id") or "") == event.other_user_id
        for item in contexts
    )


def _preference_allows_push(event: NotificationEvent, preferences: dict) -> bool:
    if not preferences.get("global_enabled", True):
        return False
    if event.surface == PUBLIC_AYUE:
        return bool(preferences.get("public_ayue_enabled", True))
    if event.surface == MEDIATOR_PRIVATE:
        return event.other_user_id not in set(preferences.get("muted_mediator_ids", []))
    return event.other_user_id not in set(preferences.get("muted_peer_ids", []))


def _increment_unread(event: NotificationEvent) -> None:
    document_id = _thread_id(event.recipient_id, event.surface, event.conversation_id)
    message_id = event.message_id or hashlib.sha256(
        f"{event.timestamp}:{event.body}".encode("utf-8")
    ).hexdigest()
    try:
        notification_threads_coll.update_one(
            {"_id": document_id, "recent_message_ids": {"$ne": message_id}},
            {
                "$setOnInsert": {
                    "user_id": event.recipient_id,
                    "surface": event.surface,
                    "conversation_id": event.conversation_id,
                },
                "$set": {
                    "last_message_id": message_id,
                    "last_message_at": event.timestamp,
                    "other_user_id": event.other_user_id,
                },
                "$inc": {"unread_count": 1},
                "$push": {
                    "recent_message_ids": {"$each": [message_id], "$slice": -100},
                },
            },
            upsert=True,
        )
    except DuplicateKeyError:
        # The deterministic _id makes a duplicate delivery idempotent.
        pass
    except Exception as exc:
        # Notification projections are auxiliary.  A Mongo outage must never
        # turn an already-persisted chat message into an HTTP failure.
        print(f"[notification_service] unread projection failed: {type(exc).__name__}")


def prepare_message_notifications(msg: dict) -> list[NotificationEvent]:
    """Record unread state and return only events eligible for system push."""
    dispatches: list[NotificationEvent] = []
    for event in classify_message_notifications(msg):
        contexts = _active_contexts(event.recipient_id)
        exact_active = _is_exact_surface_active(event, contexts)
        if not exact_active:
            _increment_unread(event)
        preferences = get_notification_preferences(event.recipient_id)
        if not _preference_allows_push(event, preferences):
            continue
        if _should_suppress_push(event, contexts):
            continue
        dispatches.append(event)
    return dispatches


def mark_notification_thread_read(user_id: str, surface: str, conversation_id: str) -> None:
    if surface not in {PAIR, MEDIATOR_PRIVATE, PUBLIC_AYUE}:
        raise ValueError("unsupported notification surface")
    try:
        notification_threads_coll.update_one(
            {"_id": _thread_id(user_id, surface, conversation_id)},
            {"$set": {
                "user_id": user_id,
                "surface": surface,
                "conversation_id": conversation_id,
                "unread_count": 0,
                "last_read_at": time.time(),
            }},
            upsert=True,
        )
    except Exception as exc:
        print(f"[notification_service] read projection failed: {type(exc).__name__}")


def notification_unread_map(user_id: str, surface: str | None = None) -> dict[str, int]:
    query: dict = {"user_id": user_id, "unread_count": {"$gt": 0}}
    if surface:
        query["surface"] = surface
    try:
        docs = notification_threads_coll.find(
            query, {"_id": 0, "conversation_id": 1, "unread_count": 1},
        )
        return {
            str(item.get("conversation_id") or ""): int(item.get("unread_count") or 0)
            for item in docs
            if item.get("conversation_id")
        }
    except Exception:
        return {}
