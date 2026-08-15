import hashlib
import time
from database import messages_coll

def generate_room_id(u1, u2):
    return "_".join(sorted([u1, u2]))

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
    return msg


def save_pair_owner_message_once(
    room_id: str,
    sender_id: str,
    content: str,
    *,
    client_message_id: str,
    risk_projection: dict,
):
    """Persist one allowed pair-chat owner message for one client attempt."""
    if not client_message_id:
        raise ValueError("client_message_id is required")
    digest = hashlib.sha256(
        f"{room_id}:{sender_id}:{client_message_id}".encode("utf-8")
    ).hexdigest()
    message_id = f"pair-owner:{digest}"
    msg = {
        "_id": message_id,
        "room_id": room_id,
        "sender_id": sender_id,
        "content": content,
        "message_type": "text",
        "metadata": {"risk": dict(risk_projection)},
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
    return msg
