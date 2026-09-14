"""Onboarding HTTP adapters extracted from the legacy chat router."""

from fastapi import APIRouter, HTTPException

from database import profiles_coll
from models import ChatRequest, ResetRequest
from services.assessment_session_service import (
    assessment_ui_projection,
    handle_assessment_ui_message,
    reset_assessment_session,
)


router = APIRouter()


@router.post("/chat")
def chat_endpoint(req: ChatRequest):
    if req.state not in {"big_five", "deep_profile"}:
        raise HTTPException(status_code=400, detail="Invalid state")
    outcome = handle_assessment_ui_message(
        req.user_id, req.state, req.message,
        initial_interest=req.initial_interest, initialize=req.initialize,
        **({"message_id": req.client_message_id} if req.client_message_id else {}),
    )
    # Keep the established response keys. The payload now comes from the same
    # typed draft/commit state that Public Ayue uses.
    profile = profiles_coll.find_one({"user_id": req.user_id}, {"_id": 0}) or {}
    ui_projection = assessment_ui_projection(profile, req.state)
    completed = outcome.get("status") in {"committed", "already_committed"}
    payload = {
        "status": "error" if outcome.get("status") == "provider_error" else "success",
        "outcome": outcome.get("status"),
        "reply": str(outcome.get("reply") or "回覆服務暫時無法完成，請稍後重試。"),
        "is_complete": completed,
        "assessment_state": ui_projection.get("assessment_state"),
        "assessment_kind": ui_projection.get("assessment_kind"),
        "assessment_revision": ui_projection.get("assessment_revision"),
    }
    if outcome.get("status") == "provider_error":
        payload["error_code"] = outcome.get("error_code", "provider_error")
        payload["retryable"] = outcome.get("retryable", False)
    if req.state == "big_five":
        payload["big_five"] = ui_projection.get("value")
    else:
        payload["deep_profile"] = ui_projection.get("value")
    return payload


@router.post("/chat/reset")
def reset_chat_state(req: ResetRequest):
    if req.state not in {"big_five", "deep_profile"}:
        raise HTTPException(status_code=400, detail="Invalid state")
    reset_assessment_session(req.user_id, req.state)
    return {"status": "success"}
