from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from database import audit_logs_coll, messages_coll
from services.chat_service import generate_room_id
from services.guidance_service import get_suggestion, get_or_reset_plan
from services.idle_service import update_activity, check_idle_state, set_idle_notified

router = APIRouter(prefix="/api/guidance", tags=["Guidance"])

class SuggestionRequest(BaseModel):
    user_id: str
    contact_id: str
    input_text: str

class ActivityRequest(BaseModel):
    user_id: str
    contact_id: str

@router.post("/suggestion")
def get_guidance_suggestion(req: SuggestionRequest):
    room_id = generate_room_id(req.user_id, req.contact_id)
    history = list(messages_coll.find({"room_id": room_id}).sort("timestamp", 1))
    
    suggestion_dto = get_suggestion(room_id, req.user_id, req.input_text, history)
    return suggestion_dto

@router.post("/activity")
def report_activity(req: ActivityRequest):
    room_id = generate_room_id(req.user_id, req.contact_id)
    welcome_back = update_activity(room_id, req.user_id)
    return {"status": "active", "welcome_back_draft": welcome_back}

@router.get("/status")
def get_guidance_status(user_id: str, contact_id: str):
    room_id = generate_room_id(user_id, contact_id)
    plan = get_or_reset_plan(room_id)
    current_role = plan.get("current_role", "FRIEND")
    
    current_user_idle_info = check_idle_state(room_id, user_id, current_role)
    partner_idle_info = check_idle_state(room_id, contact_id, current_role)
    
    # We set notified to True after returning the state
    if current_user_idle_info["is_idle"] and not current_user_idle_info.get("notified"):
        set_idle_notified(room_id, user_id)
        current_user_idle_info["is_new_notification"] = True
    else:
        current_user_idle_info["is_new_notification"] = False
        
    if partner_idle_info["is_idle"] and not partner_idle_info.get("notified"):
        set_idle_notified(room_id, contact_id)
        partner_idle_info["is_new_notification"] = True
        
        from services.chat_service import save_message
        from database import messages_coll
        notif_text = partner_idle_info.get("system_notification_text", "[AI Copilot] 對方暫時離開。")
        msg = save_message(room_id, "ai_assistant", notif_text)
        messages_coll.update_one({"_id": msg["_id"]}, {"$set": {"is_system_idle": True}})
    else:
        partner_idle_info["is_new_notification"] = False

    return {
        "current_role": current_role,
        "current_user_idle_state": current_user_idle_info,
        "partner_idle_state": partner_idle_info
    }

@router.get("/audit/{suggestion_id}")
def get_audit_log(suggestion_id: str):
    log = audit_logs_coll.find_one({"suggestion_id": suggestion_id}, {"_id": 0})
    if not log:
        raise HTTPException(status_code=404, detail="Audit log not found")
    return log
