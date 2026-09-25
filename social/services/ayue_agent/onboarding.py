"""Room-first onboarding state for the Public Ayue surface.

The first Public Ayue greeting is a normal, durable assistant message.  The
old ``public_ayue_onboarding`` envelope is kept as a read-only compatibility
projection for clients that have not migrated yet, but new clients use
``ensure_public_ayue_onboarding`` and render the persisted message from room
history.
"""

from __future__ import annotations

import hashlib
import time

from services.profile_writer import update_profile as write_profile
from database import ai_rooms_coll, messages_coll, profiles_coll
from services.chat_service import generate_room_id
from services.appwrite_mirror import mirror_message_to_appwrite_async
from pymongo.errors import DuplicateKeyError


PUBLIC_AYUE_ONBOARDING_VERSION = 1
# Version 1 is the legacy three-bubble envelope.  Keep its public constant so
# old clients can continue to deserialize their response while the durable
# greeting uses an additive account-level version.
PUBLIC_AYUE_ONBOARDING_MESSAGE_VERSION = 2
PUBLIC_AYUE_ONBOARDING_EVENT_KEY = "public-ayue-onboarding:v2"
PUBLIC_AYUE_ONBOARDING_MESSAGES = (
    "嗨，我是阿月。你可以把我當成一個會陪你聊生活、也幫你留意緣分的媒人朋友。",
    "不用學指令，最近在忙什麼、去了哪裡，或心裡卡著什麼，都可以直接跟我說。我會慢慢把你的近況接起來，也不會把你的原話直接丟給別人。",
    "哪天我真的想到值得認識的人，會先問你，你點頭後才牽線。認識之後，我也會在你們聊天室裡陪你；阿月悄悄話還是我，只是會專心看你跟這個人的互動。",
)
PUBLIC_AYUE_ONBOARDING_MESSAGE = (
    "嗨，我是阿月，是這裡像朋友一樣陪你聊天、也幫你牽線的 AI 媒人。\n\n"
    "你可以先跟我聊最近在做什麼，也可以直接說想認識怎樣的人，像是「想找人一起看展」；"
    "沒有指定條件時，我會參考你最近分享的近況。偶爾看到適合的活動，我也會問你要不要認識活動伴。\n\n"
    "真的開始找之前我會先跟你確認，找到的介紹會放到「阿月牽線」，要不要認識都由你決定。\n\n"
    "你想先隨便聊聊，還是想試著找一位新朋友？"
)


def _onboarding_message_id(room_id: str) -> str:
    """Return the legacy room-scoped id used by pre-v2 compatibility rows."""
    digest = hashlib.sha256(
        f"{room_id}:{PUBLIC_AYUE_ONBOARDING_EVENT_KEY}".encode("utf-8")
    ).hexdigest()
    return f"system-event:{digest}"


def _onboarding_account_message_id(user_id: str) -> str:
    """Return one stable event id for every normal room of an account."""
    digest = hashlib.sha256(
        f"{user_id}:{PUBLIC_AYUE_ONBOARDING_EVENT_KEY}".encode("utf-8")
    ).hexdigest()
    return f"system-event:{digest}"


def _public_message_projection(message: dict | None) -> dict | None:
    """Return the stable message contract used by the onboarding endpoint."""
    if not isinstance(message, dict):
        return None
    message_id = str(message.get("message_id") or message.get("_id") or "").strip()
    content = str(message.get("content") or "").strip()
    if not message_id or not content:
        return None
    try:
        timestamp = float(message.get("timestamp") or 0)
    except (TypeError, ValueError):
        timestamp = 0.0
    metadata = message.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "message_id": message_id,
        "content": content,
        "sender_id": str(message.get("sender_id") or "ai_assistant"),
        "message_type": str(message.get("message_type") or "text"),
        "metadata": dict(metadata),
        "timestamp": timestamp,
    }


def _message_record(room_id: str, *, message_id: str | None = None) -> dict:
    now = time.time()
    return {
        "_id": str(message_id or _onboarding_message_id(room_id)),
        "room_id": room_id,
        "sender_id": "ai_assistant",
        "content": PUBLIC_AYUE_ONBOARDING_MESSAGE,
        "message_type": "text",
        "metadata": {
            "event_type": "public_ayue_onboarding",
            "onboarding_version": PUBLIC_AYUE_ONBOARDING_MESSAGE_VERSION,
            # This message is part of the room's initial state. It must not
            # create a push event or an unread badge for the owner.
            "notification_eligible": False,
        },
        "timestamp": now,
    }


def ensure_public_ayue_onboarding(
    user_id: str, *, room_id: str | None = None,
) -> dict:
    """Ensure one persisted greeting for the user's general Public Ayue room.

    The deterministic message id makes retries and concurrent devices
    idempotent.  Existing room history is treated as an already-started
    conversation, so the new greeting is never inserted after a user has
    already chatted.  This function writes the account version only; it does
    not run profile extraction, quota accounting, search, unread, or push
    flows.
    """
    owner = str(user_id or "").strip()
    legacy_room = generate_room_id(owner, "ai_assistant")
    requested_room = str(room_id or "").strip()
    if not owner:
        return {
            "version": PUBLIC_AYUE_ONBOARDING_MESSAGE_VERSION,
            "created": False,
            "message": None,
        }

    if requested_room:
        # The endpoint may be called while opening a newly-created normal AI
        # room. Validate ownership and room kind through the room service so a
        # Hub, legacy proposal room or foreign room cannot receive onboarding.
        if requested_room != legacy_room:
            try:
                from services.ai_room_service import get_room
                room_projection = get_room(requested_room, owner)
            except Exception:
                room_projection = None
            if not room_projection:
                return {
                    "version": PUBLIC_AYUE_ONBOARDING_MESSAGE_VERSION,
                    "created": False,
                    "message": None,
                }
            room_kind = str(room_projection.get("room_kind") or "")
            if (
                room_kind in {"match_hub", "legacy_proposal"}
                or bool(room_projection.get("is_proposal_room"))
                or bool(room_projection.get("is_legacy_proposal"))
                or "::proposal::" in requested_room
            ):
                return {
                    "version": PUBLIC_AYUE_ONBOARDING_MESSAGE_VERSION,
                    "created": False,
                    "message": None,
                }
        room = requested_room
    else:
        room = legacy_room
    message_id = _onboarding_account_message_id(owner)
    existing_greeting = messages_coll.find_one({"_id": message_id})
    if existing_greeting:
        # A greeting created while another normal room was open belongs to
        # that room. Return no message here so a client cannot accidentally
        # merge it into the currently opened room.
        if str(existing_greeting.get("room_id") or "") != room:
            write_profile(profiles_coll,
                {"user_id": owner},
                {"$max": {"public_ayue_onboarding_version": PUBLIC_AYUE_ONBOARDING_MESSAGE_VERSION}},
                upsert=True,
            )
            return {
                "version": PUBLIC_AYUE_ONBOARDING_MESSAGE_VERSION,
                "created": False,
                "message": None,
            }
        message = _public_message_projection(existing_greeting)
        write_profile(profiles_coll,
            {"user_id": owner},
            {"$max": {"public_ayue_onboarding_version": PUBLIC_AYUE_ONBOARDING_MESSAGE_VERSION}},
            upsert=True,
        )
        return {
            "version": PUBLIC_AYUE_ONBOARDING_MESSAGE_VERSION,
            "created": False,
            "message": message,
        }

    profile = profiles_coll.find_one(
        {"user_id": owner},
        {"onboarding_completed": 1, "public_ayue_onboarding_version": 1},
    ) or {}
    if (
        bool(profile.get("onboarding_completed"))
        or int(profile.get("public_ayue_onboarding_version", 0) or 0)
        >= PUBLIC_AYUE_ONBOARDING_MESSAGE_VERSION
    ):
        return {
            "version": PUBLIC_AYUE_ONBOARDING_MESSAGE_VERSION,
            "created": False,
            "message": None,
        }

    # A normal room with any other message is an existing conversation. The
    # onboarding version is account-scoped, so inspect every normal Ayue room
    # owned by this account before inserting into the selected empty room.
    normal_room_ids = {legacy_room, room}
    try:
        for room_doc in ai_rooms_coll.find({"user_id": owner}, {
            "room_id": 1, "room_kind": 1, "is_proposal_room": 1,
            "is_legacy_proposal": 1,
        }):
            candidate_room = str(room_doc.get("room_id") or room_doc.get("_id") or "")
            kind = str(room_doc.get("room_kind") or "")
            if (
                candidate_room
                and kind not in {"match_hub", "legacy_proposal"}
                and not bool(room_doc.get("is_proposal_room"))
                and not bool(room_doc.get("is_legacy_proposal"))
                and "::proposal::" not in candidate_room
            ):
                normal_room_ids.add(candidate_room)
    except Exception:
        # Legacy installations may not have the AI-room collection yet. The
        # selected room and permanent legacy room still provide a safe check.
        pass
    try:
        existing_history = messages_coll.count_documents({
            "room_id": {"$in": list(normal_room_ids)},
        }) > 0
    except Exception:
        existing_history = any(
            messages_coll.count_documents({"room_id": item}) > 0
            for item in normal_room_ids
        )
    if existing_history:
        write_profile(profiles_coll,
            {"user_id": owner},
            {"$max": {"public_ayue_onboarding_version": PUBLIC_AYUE_ONBOARDING_MESSAGE_VERSION}},
            upsert=True,
        )
        return {
            "version": PUBLIC_AYUE_ONBOARDING_MESSAGE_VERSION,
            "created": False,
            "message": None,
        }

    record = _message_record(room, message_id=message_id)
    try:
        result = messages_coll.update_one(
            # The account-level id is the uniqueness boundary. Including
            # room_id would make a concurrent request for a second room
            # attempt another insert with the same _id.
            {"_id": message_id},
            {"$setOnInsert": record},
            upsert=True,
        )
    except DuplicateKeyError:
        # Another device won the account-level insert between the read and
        # upsert. Read the winner below and never synthesize a second-room
        # message locally.
        result = None
    created = bool(getattr(result, "upserted_id", None))
    if created:
        # Mirror room history for Realtime clients, while the explicit
        # notification_eligible flag keeps this state message out of push and
        # unread projections.
        mirror_message_to_appwrite_async({**record, "message_id": message_id})
    stored = messages_coll.find_one({"_id": message_id})
    if not stored or str(stored.get("room_id") or "") != room:
        write_profile(profiles_coll,
            {"user_id": owner},
            {"$max": {"public_ayue_onboarding_version": PUBLIC_AYUE_ONBOARDING_MESSAGE_VERSION}},
            upsert=True,
        )
        return {
            "version": PUBLIC_AYUE_ONBOARDING_MESSAGE_VERSION,
            "created": False,
            "message": None,
        }
    message = _public_message_projection(stored)
    write_profile(profiles_coll,
        {"user_id": owner},
        {"$max": {"public_ayue_onboarding_version": PUBLIC_AYUE_ONBOARDING_MESSAGE_VERSION}},
        upsert=True,
    )
    return {
        "version": PUBLIC_AYUE_ONBOARDING_MESSAGE_VERSION,
        "created": created,
        "message": message,
    }


def public_ayue_onboarding_state(user_id: str) -> dict | None:
    """Return onboarding bubbles only for a genuinely empty Public room."""
    profile = profiles_coll.find_one({"user_id": user_id}, {"onboarding_completed": 1, "public_ayue_onboarding_version": 1}) or {}
    if bool(profile.get("onboarding_completed")):
        return None
    if int(profile.get("public_ayue_onboarding_version", 0) or 0) >= PUBLIC_AYUE_ONBOARDING_MESSAGE_VERSION:
        return None
    room_id = generate_room_id(user_id, "ai_assistant")
    if messages_coll.count_documents({"room_id": room_id}) > 0:
        return None
    return {
        "version": PUBLIC_AYUE_ONBOARDING_VERSION,
        "messages": list(PUBLIC_AYUE_ONBOARDING_MESSAGES),
    }


def complete_public_ayue_onboarding(user_id: str) -> None:
    """Idempotently mark the additive onboarding version complete."""
    write_profile(profiles_coll,
        {"user_id": user_id},
        {"$max": {"public_ayue_onboarding_version": PUBLIC_AYUE_ONBOARDING_VERSION}},
        upsert=True,
    )
