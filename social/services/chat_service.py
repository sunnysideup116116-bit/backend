import hashlib
import time
import uuid
from database import messages_coll
from services.appwrite_mirror import mirror_message_to_appwrite_async
from services.push_service import queue_push_notification

def generate_room_id(u1, u2):
    return "_".join(sorted([u1, u2]))

# --- AI chat rooms (multi-room surface) ---
# Public Ayue used to expose exactly one deterministic room per user
# (`generate_room_id(user_id, "ai_assistant")`). To let a user open many
# independent AI conversations, new rooms use a namespaced, random id in the
# same spirit as `mediator_private::{user}::{other}`. The legacy room stays put
# so onboarding, assessment, proactive care, and old clients keep working.
_AI_ROOM_PREFIX = "ai_room::"


def generate_ai_room_id(user_id: str) -> str:
    """Return a new random AI room id owned by ``user_id``.

    The owner is embedded in the id so :func:`ai_room_owner` can recover it
    without a DB lookup, and so room ids are self-validating.
    """
    return f"{_AI_ROOM_PREFIX}{user_id}::{uuid.uuid4().hex}"


def is_ai_room(room_id: str | None) -> bool:
    return bool(room_id) and str(room_id).startswith(_AI_ROOM_PREFIX)


def ai_room_owner(room_id: str | None) -> str | None:
    """Recover the owner user_id embedded in an AI room id, or None."""
    if not is_ai_room(room_id):
        return None
    body = str(room_id)[len(_AI_ROOM_PREFIX):]
    if "::" not in body:
        return None
    return body.split("::", 1)[0]


def generate_proposal_ai_room_id(user_id: str, match_id: str) -> str:
    """Return the deterministic AI room id for one match proposal.

    Proposals for the same match always land in the same room (idempotent
    without a lookup), so follow-up events for one match share one surface.
    """
    return f"{_AI_ROOM_PREFIX}{user_id}::proposal::{match_id}"


def save_message(room_id, sender_id, content, message_type="text", metadata=None):
    msg = {
        "room_id": room_id,
        "sender_id": sender_id,
        "content": content,
        "message_type": message_type,
        "metadata": metadata or {},
        "timestamp": time.time()
    }
    result = messages_coll.insert_one(msg)
    msg["message_id"] = str(result.inserted_id)
    mirror_message_to_appwrite_async(msg)
    queue_push_notification(msg)
    return msg


def save_pair_owner_message_once(
    room_id: str,
    sender_id: str,
    content: str,
    *,
    client_message_id: str,
    risk_projection: dict,
    message_type: str = "text",
    file_id: str | None = None,
):
    """Persist one allowed pair-chat owner message for one client attempt."""
    if not client_message_id:
        raise ValueError("client_message_id is required")
    digest = hashlib.sha256(
        f"{room_id}:{sender_id}:{client_message_id}".encode("utf-8")
    ).hexdigest()
    message_id = f"pair-owner:{digest}"
    metadata: dict = {"risk": dict(risk_projection)}
    if file_id:
        metadata["file_id"] = file_id
    msg = {
        "_id": message_id,
        "room_id": room_id,
        "sender_id": sender_id,
        "content": content,
        "message_type": message_type,
        "metadata": metadata,
        "risk_level": str(risk_projection.get("level") or "safe"),
        "is_blocked": False,
        "delivery_status": "delivered",
        "timestamp": time.time(),
    }
    result = messages_coll.update_one(
        {"_id": message_id}, {"$setOnInsert": msg}, upsert=True,
    )
    msg["message_id"] = message_id
    msg["created"] = bool(getattr(result, "upserted_id", None))
    if msg["created"]:
        mirror_message_to_appwrite_async(msg)
        queue_push_notification(msg)
    return msg


def save_system_message_once(room_id, content, message_type="text", metadata=None, *, event_key: str):
    """Persist a system-authored message at most once for one durable event.

    The deterministic Mongo ``_id`` keeps retrying a committed match transition
    from duplicating shared-room welcome messages, without exposing the key in
    the public message payload.
    """
    if not event_key:
        raise ValueError("event_key is required")
    digest = hashlib.sha256(f"{room_id}:{event_key}".encode("utf-8")).hexdigest()
    message_id = f"system-event:{digest}"
    msg = {
        "_id": message_id,
        "room_id": room_id,
        "sender_id": "ai_assistant",
        "content": content,
        "message_type": message_type,
        "metadata": metadata or {},
        "timestamp": time.time(),
    }
    result = messages_coll.update_one({"_id": message_id}, {"$setOnInsert": msg}, upsert=True)
    msg["message_id"] = message_id
    msg["created"] = bool(getattr(result, "upserted_id", None))
    if msg["created"]:
        mirror_message_to_appwrite_async(msg)
    return msg
