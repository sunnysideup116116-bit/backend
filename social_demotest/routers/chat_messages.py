"""Message history and contact-list HTTP adapters for the chat surface."""

from fastapi import APIRouter

from database import matches_coll, messages_coll, profiles_coll
from services.ayue_agent.public_relationship_projection import mentioned_contact_refs
from services.chat_service import generate_room_id, save_message
from services.match_state_service import verified_accepted_match_query


router = APIRouter()


def _find_accepted_match(user_id: str, other_id: str):
    return matches_coll.find_one(verified_accepted_match_query(user_id, other_id))


@router.get("/messages/{contact_id}")
def get_messages(contact_id: str, user_id: str):
    room_id = generate_room_id(user_id, contact_id)
    if contact_id == "ai_assistant" and messages_coll.count_documents({"room_id": room_id}) == 0:
        save_message(
            room_id,
            "ai_assistant",
            "哈囉，我是阿月。最近想做什麼、想去哪裡，儘管跟我說；我會邊聊邊幫你留意合適的人。",
        )

    messages = list(messages_coll.find({"room_id": room_id}, {"_id": 0}).sort("timestamp", 1))
    user_doc = profiles_coll.find_one({"user_id": user_id})
    active_proposal_id = (user_doc or {}).get("active_match_proposal_id")
    date_coordination = None
    established_dates = []
    if contact_id != "ai_assistant":
        match_doc = _find_accepted_match(user_id, contact_id)
        if match_doc:
            date_coordination = match_doc.get("date_coordination")
            established_dates = match_doc.get("established_dates", [])
    return {
        "messages": messages,
        "active_match_proposal_id": active_proposal_id,
        "date_coordination": date_coordination,
        "established_dates": established_dates,
    }


@router.get("/contacts")
def get_contacts(user_id: str):
    user_doc = profiles_coll.find_one({"user_id": user_id})
    ai_locked = user_doc.get("ai_chat_locked", False) if user_doc else False
    matches = list(matches_coll.find(verified_accepted_match_query(user_id)))
    contacts = [{
        "id": "ai_assistant",
        "name": "阿月",
        "role": "system",
        "context": "你的媒人助理，會邊聊天邊幫你留意合適的人。",
        "is_locked": ai_locked,
    }]
    for match_doc in matches:
        other_id = match_doc["to_user"] if match_doc["from_user"] == user_id else match_doc["from_user"]
        other_doc = profiles_coll.find_one({"user_id": other_id})
        contacts.append({
            "id": other_id,
            "name": mentioned_contact_refs(user_id, [other_id])[0]["display_name"],
            "role": "user",
            "context": other_doc.get("current_context", "尚無近期情境") if other_doc else "尚無近期情境",
        })
    return {"contacts": contacts}
