"""Message history and contact-list HTTP adapters for the chat surface."""

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field
from typing import Literal

from database import db, matches_coll, messages_coll, profiles_coll
from services.ayue_agent.v3.confirmation import project_match_choice_history
from models import ClearRequest
from services.ai_room_service import (
    create_room as create_ai_room,
    delete_room as delete_ai_room,
    get_room as get_ai_room,
    list_rooms as list_ai_rooms,
    mark_room_read,
    maybe_backfill_title,
    rename_room as rename_ai_room,
    ensure_match_hub,
    MATCH_HUB_ROOM_KIND,
)
from services.ayue_agent.onboarding import (
    complete_public_ayue_onboarding, ensure_public_ayue_onboarding,
    public_ayue_onboarding_state,
)
from services.assessment_session_service import assessment_public_state_for_room
from services.ayue_agent.public_relationship_projection import (
    mentioned_contact_refs, display_name as public_display_name,
)
from services.chat_service import generate_room_id
from services.notification_service import (
    PAIR,
    MEDIATOR_PRIVATE,
    notification_unread_map,
)
from services.relationship_engagement_service import generate_mediator_private_room_id
from services.match_state_service import verified_accepted_match_query
from services.match_card_projection import project_match_card_history
from services.public_nickname_service import proposal_display_name
from services.risk_block_service import (
    RiskBlockServiceUnavailable,
    risk_block_service,
)


router = APIRouter()


def _find_accepted_match(user_id: str, other_id: str):
    return matches_coll.find_one(verified_accepted_match_query(user_id, other_id))


def _strip_internal_message_use(messages: list[dict]) -> list[dict]:
    """Keep server-owned reuse policy out of the public history contract."""
    projected: list[dict] = []
    for message in messages:
        item = dict(message or {})
        metadata = item.get("metadata")
        if isinstance(metadata, dict) and "message_use" in metadata:
            metadata = dict(metadata)
            metadata.pop("message_use", None)
            if metadata:
                item["metadata"] = metadata
            else:
                item.pop("metadata", None)
        projected.append(item)
    return projected


def _project_public_message_ids(messages: list[dict]) -> list[dict]:
    """Expose one stable public id without migrating legacy message rows.

    Newer writers persist ``message_id`` while older rows may only have
    Mongo's ``_id``.  The client needs the same identity for history and live
    events, so derive the public value at read time and always remove the
    database-only field before returning the payload.
    """
    projected: list[dict] = []
    for message in messages:
        item = dict(message or {})
        message_id = item.get("message_id")
        if message_id is None or not str(message_id).strip():
            message_id = item.get("_id")
        if message_id is not None and str(message_id).strip():
            item["message_id"] = str(message_id)
        item.pop("_id", None)
        projected.append(item)
    return projected


@router.get("/messages/{contact_id}")
def get_messages(
    contact_id: str,
    user_id: str,
    ai_room_id: str | None = None,
    limit: int | None = None,
    before: float | None = None,
    response: Response = None,
):
    if response is not None:
        # Message history is a live polling resource. Intermediary/CDN caching
        # previously returned old risk metadata and made handled prompts appear
        # again after users reopened a room.
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    # Multi-room AI surface: an explicit AI room id overrides the derived
    # legacy room. Ownership is enforced; only AI rooms take this path.
    if ai_room_id:
        room = get_ai_room(ai_room_id, user_id)
        if not room:
            raise HTTPException(status_code=403, detail="無權存取此聊天室")
        room_id = ai_room_id
        # Retry pending title generation when the user reopens the room.
        maybe_backfill_title(room_id, user_id)
        # Opening the room clears its NEW flag so the badge disappears.
        mark_room_read(room_id, user_id)
    else:
        room_id = generate_room_id(user_id, contact_id)

    is_ai_contact = contact_id == "ai_assistant"
    query: dict = {"room_id": room_id, "is_blocked": {"$ne": True}}
    if before is not None:
        query["timestamp"] = {"$lt": before}
    # Keep _id available long enough to derive a stable public identity for
    # legacy rows.  _project_public_message_ids removes it before serialization.
    cursor = messages_coll.find(query).sort("timestamp", -1)
    if limit is not None and limit > 0:
        # Fetch one extra message to detect whether older history exists.
        fetched = list(cursor.limit(limit + 1))
        has_more = len(fetched) > limit
        # Sort is descending (newest first); re-reverse so the client always
        # receives messages in chronological (oldest → newest) order.
        messages = list(reversed(fetched[:limit]))
    else:
        # Legacy clients expect ascending order without a limit.
        messages = list(cursor)[::-1]
        has_more = False
    messages = _project_public_message_ids(messages)
    messages = _strip_internal_message_use(messages)
    if is_ai_contact:
        messages = project_match_card_history(
            messages, user_id,
            nickname_lookup=lambda uid: proposal_display_name(uid, fallback_lookup=public_display_name),
        )
        messages = project_match_choice_history(
            messages, user_id=user_id, room_id=room_id, collection=db["v3_pending_confirmations"],
        )
    user_doc = profiles_coll.find_one({"user_id": user_id})
    active_proposal_id = (user_doc or {}).get("active_match_proposal_id")
    date_coordination = None
    established_dates = []
    if not is_ai_contact:
        match_doc = _find_accepted_match(user_id, contact_id)
        if match_doc:
            date_coordination = match_doc.get("date_coordination")
            established_dates = match_doc.get("established_dates", [])
    payload = {
        "messages": messages,
        "has_more": has_more,
        "public_ayue_onboarding": (
            public_ayue_onboarding_state(user_id)
            if is_ai_contact and not ai_room_id  # onboarding only in legacy room
            else None
        ),
        "active_match_proposal_id": active_proposal_id,
        "date_coordination": date_coordination,
        "established_dates": established_dates,
    }
    if is_ai_contact:
        payload.update(assessment_public_state_for_room(
            user_doc or {}, room_id, include_unscoped=not bool(ai_room_id),
        ))
    if ai_room_id:
        room = get_ai_room(room_id, user_id) or {}
        payload["ai_room"] = room
    return payload


@router.post("/public-ayue/onboarding/complete")
def complete_public_ayue_onboarding_route(req: ClearRequest):
    complete_public_ayue_onboarding(req.user_id)
    return {"status": "ok", "version": 1}


class PublicAyueOnboardingEnsureRequest(BaseModel):
    """Optional room hint used to keep onboarding scoped to the general room."""

    user_id: str
    ai_room_id: str | None = Field(default=None, min_length=1, max_length=128)


@router.post("/public-ayue/onboarding/ensure")
def ensure_public_ayue_onboarding_route(req: PublicAyueOnboardingEnsureRequest):
    """Persist the one-time Public Ayue self-introduction, if applicable."""
    return ensure_public_ayue_onboarding(req.user_id, room_id=req.ai_room_id)


@router.get("/contacts")
def get_contacts(user_id: str):
    try:
        excluded_user_ids = risk_block_service.excluded_user_ids(user_id)
    except RiskBlockServiceUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="安全關係狀態暫時無法確認",
        ) from exc
    user_doc = profiles_coll.find_one({"user_id": user_id})
    ai_locked = user_doc.get("ai_chat_locked", False) if user_doc else False
    matches = [
        match_doc
        for match_doc in matches_coll.find(verified_accepted_match_query(user_id))
        if (
            match_doc["to_user"]
            if match_doc["from_user"] == user_id
            else match_doc["from_user"]
        ) not in excluded_user_ids
    ]
    contacts = [{
        "id": "ai_assistant",
        "name": "阿月",
        "role": "system",
        "context": "先懂你，再在合適時機陪你牽線的媒人朋友。",
        "is_locked": ai_locked,
    }]
    pair_unread = notification_unread_map(user_id, PAIR)
    mediator_unread = notification_unread_map(user_id, MEDIATOR_PRIVATE)
    room_ids = [
        generate_room_id(
            user_id,
            match_doc["to_user"] if match_doc["from_user"] == user_id else match_doc["from_user"],
        )
        for match_doc in matches
    ]
    latest_by_room = {}
    if room_ids:
        for msg in messages_coll.find(
            {"room_id": {"$in": room_ids}, "is_blocked": {"$ne": True}},
            {"_id": 0, "room_id": 1, "content": 1},
        ).sort("timestamp", -1):
            room = msg.get("room_id")
            if room and room not in latest_by_room:
                latest_by_room[room] = msg.get("content", "")
    for match_doc in matches:
        other_id = match_doc["to_user"] if match_doc["from_user"] == user_id else match_doc["from_user"]
        other_doc = profiles_coll.find_one({"user_id": other_id})
        room_id = generate_room_id(user_id, other_id)
        mediator_room_id = generate_mediator_private_room_id(user_id, other_id)
        role = "from" if match_doc.get("from_user") == user_id else "to"
        legacy_mediator_unread = int(
            (match_doc.get("private_unread", {}) or {}).get(role, 0) or 0
        )
        pair_unread_count = int(pair_unread.get(room_id, 0))
        mediator_unread_count = max(
            int(mediator_unread.get(mediator_room_id, 0)),
            legacy_mediator_unread,
        )
        contacts.append({
            "id": other_id,
            "name": mentioned_contact_refs(user_id, [other_id])[0]["display_name"],
            "role": "user",
            "context": other_doc.get("current_context", "尚無近期情境") if other_doc else "尚無近期情境",
            "latest_message": latest_by_room.get(room_id, ""),
            "unread": pair_unread_count > 0,
            "unread_count": pair_unread_count,
            "mediator_unread_count": mediator_unread_count,
        })
    return {"contacts": contacts}


# --- AI multi-room surface ---

class CreateAiRoomRequest(BaseModel):
    user_id: str
    room_kind: Literal["conversation", "match_hub"] = "conversation"


class RenameAiRoomRequest(BaseModel):
    user_id: str
    title: str = Field(min_length=1, max_length=60)


class DeleteAiRoomRequest(BaseModel):
    user_id: str


@router.get("/ai_rooms")
def list_ai_rooms_route(user_id: str):
    profile = profiles_coll.find_one(
        {"user_id": user_id}, {"_id": 0, "agentic_assessment_session": 1},
    ) or {}
    return {"rooms": list_ai_rooms(user_id, assessment_profile=profile)}


@router.post("/ai_rooms")
def create_ai_room_route(req: CreateAiRoomRequest):
    room = (
        ensure_match_hub(req.user_id)
        if req.room_kind == MATCH_HUB_ROOM_KIND
        else create_ai_room(req.user_id)
    )
    if not room:
        raise HTTPException(status_code=503, detail="阿月牽線目前暫時無法使用")
    return {"room": room}


@router.patch("/ai_rooms/{room_id}")
def rename_ai_room_route(room_id: str, req: RenameAiRoomRequest):
    room = rename_ai_room(room_id, req.user_id, req.title)
    if not room:
        raise HTTPException(status_code=403, detail="無法重新命名此聊天室")
    return {"room": room}


@router.delete("/ai_rooms/{room_id}")
def delete_ai_room_route(room_id: str, user_id: str):
    ok = delete_ai_room(room_id, user_id)
    if not ok:
        raise HTTPException(status_code=403, detail="無法刪除此聊天室")
    return {"status": "ok"}
