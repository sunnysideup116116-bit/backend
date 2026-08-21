"""Presence heartbeat for push-notification suppression.

The Flutter app reports a heartbeat while it is in the foreground. When a new
pair-chat message arrives, the push dispatcher skips notifications for
receivers whose last activity is within the active window, so an open app is
not spammed with a system notification it already sees via Realtime/polling.
"""

from fastapi import APIRouter

from database import profiles_coll
from models import ClearRequest
import time


router = APIRouter()


@router.post("/presence")
def report_presence(req: ClearRequest):
    """Record foreground presence for one user.

    Uses the dedicated ``last_presence_at`` heartbeat field so it never
    touches ``last_user_activity_at``, which drives the proactive-care
    scheduler.
    """
    profiles_coll.update_one(
        {"user_id": req.user_id},
        {"$set": {"last_presence_at": time.time()}},
        upsert=True,
    )
    return {"status": "ok"}
