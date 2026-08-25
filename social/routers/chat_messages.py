"""Message history and contact-list HTTP adapters for the chat surface."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from database import matches_coll, messages_coll, profiles_coll
from models import ClearRequest
from services.ai_room_service import (
    create_room as create_ai_room,
    delete_room as delete_ai_room,
    get_room as get_ai_room,
    list_rooms as list_ai_rooms,
    mark_room_read,
    maybe_backfill_title,
    rename_room as rename_ai_room,
)
from services.ayue_agent.onboarding import (
    complete_public_ayue_onboarding, public_ayue_onboarding_state,
)
from services.assessment_session_service import assessment_public_state_for_room
from services.ayue_agent.public_relationship_projection import mentioned_contact_refs
from services.chat_service import generate_room_id
from services.match_state_service import verified_accepted_match_query


router = APIRouter()


def _find_accepted_match(user_id: str, other_id: str):
    return matches_coll.find_one(verified_accepted_match_query(user_id, other_id))


@router.get("/messages/{contact_id}")
def get_messages(
    contact_id: str,
    user_id: str,
    ai_room_id: str | None = None,
    limit: int | None = None,
    before: float | None = None,
):
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
    cursor = messages_coll.find(query, {"_id": 0}).sort("timestamp", 1)
    if limit is not None and limit > 0:
        # Fetch one extra message to detect whether older history exists.
        fetched = list(cursor.limit(limit + 1))
        has_more = len(fetched) > limit
        messages = fetched[:limit]
    else:
        messages = list(cursor)
        has_more = False
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


@router.get("/contacts")
def get_contacts(user_id: str):
    user_doc = profiles_coll.find_one({"user_id": user_id})
    ai_locked = user_doc.get("ai_chat_locked", False) if user_doc else False
    matches = list(matches_coll.find(verified_accepted_match_query(user_id)))
    contacts = [{
        "id": "ai_assistant",
        "name": "阿月",
        "role": "system",
        "context": "先懂你，再在合適時機陪你牽線的媒人朋友。",
        "is_locked": ai_locked,
    }]
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
        contacts.append({
            "id": other_id,
            "name": mentioned_contact_refs(user_id, [other_id])[0]["display_name"],
            "role": "user",
            "context": other_doc.get("current_context", "尚無近期情境") if other_doc else "尚無近期情境",
            "latest_message": latest_by_room.get(generate_room_id(user_id, other_id), ""),
        })
    return {"contacts": contacts}


# --- AI multi-room surface ---

class CreateAiRoomRequest(BaseModel):
    user_id: str


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
    room = create_ai_room(req.user_id)
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
