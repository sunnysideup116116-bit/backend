"""Fire-and-forget push notification dispatch via Appwrite Messaging.

Pair-chat messages are persisted in MongoDB and mirrored to Appwrite for
Realtime (``appwrite_mirror``). When the receiver is not actively using the
app, we additionally dispatch a push notification through the Appwrite
Messaging FCM provider, which forwards it to the receiver's devices.

Dispatch is best-effort: failures are logged and never block the message
save path. The ``APPWRITE_PUSH_ENABLED`` flag lets the deployment disable
push delivery while the FCM provider is being set up.
"""

import json
import os
import threading
import time
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
_ENABLED = bool(
    os.getenv("APPWRITE_PUSH_ENABLED", "false").strip().lower() == "true"
) and bool(_PROJECT_ID and _API_KEY)

# A receiver whose last activity is within this window is considered online;
# their app is already delivering the message via Realtime/polling.
ACTIVE_WINDOW_SECONDS = 90.0

_HEADERS = {
    "X-Appwrite-Project": _PROJECT_ID,
    "X-Appwrite-Key": _API_KEY,
    "Content-Type": "application/json",
}

_BODY_MAX = 160


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


def _display_name(user_id: str) -> str:
    """Best-effort public display name; mirrors the contact-list label."""
    try:
        from database import profiles_coll
        profile = profiles_coll.find_one(
            {"user_id": user_id},
            {"_id": 0, "display_name": 1, "nickname": 1, "name": 1},
        ) or {}
        value = str(
            profile.get("display_name")
            or profile.get("nickname")
            or profile.get("name")
            or ""
        ).strip()
        if value and not value.startswith(("seed_user_", "demo_user", "user_")):
            return value[:30]
    except Exception:
        pass
    return "對方"


def _receiver_active(receiver_id: str) -> bool:
    """True when the receiver's app reported foreground presence recently.

    Uses the dedicated ``last_presence_at`` heartbeat field so push dispatch
    never interferes with ``last_user_activity_at``, which drives the
    proactive-care scheduler.
    """
    try:
        from database import profiles_coll
        profile = profiles_coll.find_one(
            {"user_id": receiver_id}, {"_id": 0, "last_presence_at": 1},
        )
    except Exception:
        return False
    if not profile:
        return False
    last_active = float(profile.get("last_presence_at") or 0)
    return last_active > 0 and (time.time() - last_active) < ACTIVE_WINDOW_SECONDS


def _push_body(msg: dict) -> str:
    if str(msg.get("message_type") or "text") == "image":
        return "傳了一張圖片"
    content = str(msg.get("content") or "").strip()
    if not content:
        return "傳了一則訊息"
    return content[:_BODY_MAX]


def send_push_notification(msg: dict) -> None:
    """Dispatch one push notification through Appwrite Messaging (sync)."""
    if not _ENABLED:
        return
    room_id = str(msg.get("room_id") or "")
    sender_id = str(msg.get("sender_id") or "")
    if not room_id or not sender_id:
        return
    if sender_id == "ai_assistant":
        return
    receiver_id = _other_participant(room_id, sender_id)
    if not receiver_id or receiver_id == "ai_assistant":
        return
    if msg.get("is_blocked"):
        return
    if _receiver_active(receiver_id):
        return
    payload = {
        "messageId": "unique()",
        "title": _display_name(sender_id),
        "body": _push_body(msg),
        "users": [receiver_id],
        "data": {
            "room_id": room_id,
            "sender_id": sender_id,
            "contact_id": sender_id,
            "contact_name": _display_name(sender_id),
            "msg_type": str(msg.get("message_type") or "text"),
        },
        "priority": "high",
    }
    try:
        response = requests.post(
            f"{_ENDPOINT}/messaging/messages/push",
            headers=_HEADERS,
            json=payload,
            timeout=10,
        )
        if response.status_code >= 300:
            print(
                f"[push_service] push dispatch failed: HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
    except Exception as exc:
        print(f"[push_service] push dispatch failed: {type(exc).__name__}: {exc}")


def queue_push_notification(msg: dict) -> None:
    """Queue push dispatch on a daemon thread; never blocks the caller."""
    if not _ENABLED:
        return
    threading.Thread(
        target=send_push_notification,
        args=(msg,),
        name="push-dispatch",
        daemon=True,
    ).start()
