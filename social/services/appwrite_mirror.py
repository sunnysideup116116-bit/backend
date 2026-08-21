"""Fire-and-forget mirror of MongoDB chat messages into Appwrite Realtime.

The social server keeps MongoDB as the source of truth. Every saved message
is mirrored into the ``dating_db.chat_messages`` collection so the Flutter
app's Appwrite Realtime subscription can push new messages instead of
polling.

Mirroring is best-effort: failures are logged and never block the request
path. The app keeps its polling fallback, so a failed mirror only degrades
delivery latency, never availability.
"""

import json
import os
import threading
import time
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv

SERVER_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(dotenv_path=SERVER_ROOT / ".env", override=False)

_ENDPOINT = (
    os.getenv("APPWRITE_ENDPOINT") or "http://appwrite.misproject.us.ci/v1"
).rstrip("/")
_PROJECT_ID = os.getenv("APPWRITE_PROJECT_ID") or ""
_API_KEY = os.getenv("APPWRITE_API_KEY") or ""
_DB_ID = "dating_db"
_COLLECTION_ID = "chat_messages"

_ENABLED = bool(_PROJECT_ID and _API_KEY)

_HEADERS = {
    "X-Appwrite-Project": _PROJECT_ID,
    "X-Appwrite-Key": _API_KEY,
    "Content-Type": "application/json",
}

_METADATA_MAX = 1000


def _other_participant(room_id: str, sender_id: str) -> str | None:
    """Recover the other participant from a sorted ``a_b`` room id."""
    if not room_id or not sender_id:
        return None
    prefix = sender_id + "_"
    if room_id.startswith(prefix):
        return room_id[len(prefix):]
    suffix = "_" + sender_id
    if room_id.endswith(suffix):
        return room_id[: -len(suffix)]
    return None


def mirror_message_to_appwrite(msg: dict) -> None:
    """Best-effort mirror of one saved message into Appwrite Realtime."""
    if not _ENABLED:
        return
    room_id = str(msg.get("room_id") or "")
    sender_id = str(msg.get("sender_id") or "")
    if not room_id or not sender_id:
        return
    receiver_id = _other_participant(room_id, sender_id)
    data = {
        "room_id": room_id,
        "sender_id": sender_id,
        "content": str(msg.get("content") or ""),
        "message_type": str(msg.get("message_type") or "text"),
        "timestamp": int(msg.get("timestamp") or time.time()),
        "is_blocked": bool(msg.get("is_blocked", False)),
        "delivery_status": str(msg.get("delivery_status") or "delivered"),
        "risk_level": str(msg.get("risk_level") or "safe"),
    }
    metadata = msg.get("metadata")
    if metadata:
        data["metadata"] = json.dumps(metadata, ensure_ascii=False)[:_METADATA_MAX]
    permissions = [f'read("user:{sender_id}")']
    if receiver_id:
        permissions.append(f'read("user:{receiver_id}")')
    try:
        requests.post(
            f"{_ENDPOINT}/databases/{_DB_ID}/collections/{_COLLECTION_ID}/documents",
            headers=_HEADERS,
            json={
                "documentId": uuid.uuid4().hex,
                "data": data,
                "permissions": permissions,
            },
            timeout=5,
        )
    except Exception as exc:
        print(f"[appwrite_mirror] mirror failed: {type(exc).__name__}: {exc}")


def mirror_message_to_appwrite_async(msg: dict) -> None:
    """Queue a mirror on a daemon thread; never blocks the caller."""
    if not _ENABLED:
        return
    threading.Thread(
        target=mirror_message_to_appwrite,
        args=(msg,),
        name="appwrite-mirror",
        daemon=True,
    ).start()
