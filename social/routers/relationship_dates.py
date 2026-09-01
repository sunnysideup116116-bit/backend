"""Thin HTTP adapters for the date-coordination domain service."""

from fastapi import APIRouter

from models import CalendarActionRequest, DateConfirmRequest, DateInviteResponseRequest, DateUpdateRequest
from services.date_coordination_service import (
    cancel_coordination_or_event,
    confirm_form,
    get_state,
    list_pending_for_user,
    respond_to_invite,
    update_form,
)


router = APIRouter()


@router.get("/relationship/date/pending")
def list_pending_dates(user_id: str):
    return {"items": list_pending_for_user(user_id)}


@router.post("/relationship/date/invite/respond")
def respond_to_date_invite(req: DateInviteResponseRequest):
    return {"coordination": respond_to_invite(req.user_id, req.other_id, req.coordination_id, req.accepted)}


@router.get("/relationship/date/state")
def get_date_state(user_id: str, other_id: str):
    return {"coordination": get_state(user_id, other_id)}


@router.post("/relationship/date/update")
def update_date_form(req: DateUpdateRequest):
    return {"coordination": update_form(req.user_id, req.other_id, req.coordination_id, req.revision, req.form)}


@router.post("/relationship/date/confirm")
def confirm_date_form(req: DateConfirmRequest):
    coordination, event = confirm_form(req.user_id, req.other_id, req.coordination_id, req.revision)
    return {"coordination": coordination, "event": event}


@router.post("/relationship/date/cancel")
def cancel_date_coordination(req: CalendarActionRequest, other_id: str, coordination_id: str):
    return {"coordination": cancel_coordination_or_event(req.user_id, other_id, coordination_id)}
