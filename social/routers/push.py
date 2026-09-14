"""Notification preferences, exact chat presence, and read-state adapters."""

from fastapi import APIRouter, HTTPException

from database import profiles_coll
from models import (
    ClearRequest,
    PushPreferenceRequest,
    PushPresenceRequest,
    PushReadRequest,
)
from services.notification_service import (
    get_notification_preferences,
    mark_notification_thread_read,
    report_notification_presence,
    update_notification_preference,
)
import time


router = APIRouter()


@router.post("/presence")
def report_presence(req: ClearRequest):
    """Legacy foreground heartbeat retained for older clients.

    New clients use ``/push/presence`` because a generic foreground signal is
    not precise enough to suppress notifications safely.
    """
    profiles_coll.update_one(
        {"user_id": req.user_id},
        {"$set": {"last_presence_at": time.time()}},
        upsert=True,
    )
    return {"status": "ok"}


@router.get("/push/preferences")
def notification_preferences(user_id: str):
    return get_notification_preferences(user_id)


@router.patch("/push/preferences")
def patch_notification_preferences(req: PushPreferenceRequest):
    try:
        return update_notification_preference(
            req.user_id, req.scope, req.enabled, req.target_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/push/presence")
def put_notification_presence(req: PushPresenceRequest):
    report_notification_presence(
        user_id=req.user_id,
        session_id=req.session_id,
        visible=req.visible,
        surface=req.surface,
        conversation_id=req.conversation_id,
        other_user_id=req.other_user_id,
    )
    return {"status": "ok"}


@router.post("/push/read")
def mark_notification_read(req: PushReadRequest):
    try:
        mark_notification_thread_read(
            req.user_id, req.surface, req.conversation_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok"}
