from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query

from database import calendar_events_coll, profiles_coll
from models import (
    CalendarActionRequest, CalendarEventCreateRequest, CalendarEventUpdateRequest,
    CalendarRescheduleRequest, CalendarSettingsRequest,
)
from services.calendar_service import (
    _parse_local_interval, as_utc, cancel_event, create_personal_event, get_timezone, list_events,
    normalize_form, serialize_event, update_personal_event,
)
from services.date_coordination_service import find_accepted_match


router = APIRouter(prefix="/api/calendar", tags=["Calendar"])


def _range(from_value: str | None, to_value: str | None) -> tuple[datetime, datetime]:
    try:
        zone = get_timezone("Asia/Taipei")
        start_local = datetime.combine(date.fromisoformat(from_value), time.min, zone) if from_value else datetime.now(zone) - timedelta(days=1)
        end_local = datetime.combine(date.fromisoformat(to_value) + timedelta(days=1), time.min, zone) if to_value else start_local + timedelta(days=90)
        start = start_local.astimezone(timezone.utc)
        end = end_local.astimezone(timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="日期範圍格式不正確") from exc
    if end <= start:
        raise HTTPException(status_code=400, detail="結束範圍必須晚於開始範圍")
    return start, end


@router.get("/events")
def get_events(user_id: str, from_: str | None = Query(None, alias="from"), to: str | None = None, include_cancelled: bool = False):
    start, end = _range(from_, to)
    return {"events": list_events(user_id, start, end, include_cancelled)}


@router.post("/events")
def create_event(req: CalendarEventCreateRequest):
    return {"event": create_personal_event(req.user_id, req.model_dump())}


@router.patch("/events/{event_id}")
def patch_event(event_id: str, req: CalendarEventUpdateRequest):
    return {"event": update_personal_event(req.user_id, event_id, req.model_dump(exclude_unset=True))}


@router.post("/events/{event_id}/cancel")
def cancel_calendar_event(event_id: str, req: CalendarActionRequest):
    shared = calendar_events_coll.find_one({"event_id": event_id, "source_type": "date", "participants": req.user_id})
    if shared:
        other_id = next(person for person in shared["participants"] if person != req.user_id)
        from services.date_coordination_service import cancel_coordination_or_event
        coordination = cancel_coordination_or_event(req.user_id, other_id, shared["coordination_id"])
        event = calendar_events_coll.find_one({"event_id": event_id})
        return {"event": serialize_event(event, req.user_id), "coordination": coordination}
    return {"event": cancel_event(req.user_id, event_id)}


@router.post("/events/{event_id}/reschedule")
def reschedule_date_event(event_id: str, req: CalendarRescheduleRequest):
    event = calendar_events_coll.find_one({"event_id": event_id, "source_type": "date", "participants": req.user_id, "status": "confirmed"})
    if not event:
        raise HTTPException(status_code=404, detail="找不到可改期的約會")
    other_id = next(person for person in event["participants"] if person != req.user_id)
    from services.date_coordination_service import request_reschedule
    coordination, updated_event = request_reschedule(
        req.user_id,
        other_id,
        event_id,
        req.model_dump(exclude={"user_id"}),
        expected_revision=int(event.get("revision", 1) or 1),
    )
    return {"coordination": coordination, "event": updated_event}


@router.post("/events/{event_id}/reschedule/cancel")
def cancel_reschedule(event_id: str, req: CalendarActionRequest):
    event = calendar_events_coll.find_one({"event_id": event_id, "source_type": "date", "participants": req.user_id, "status": "pending_reconfirmation"})
    if not event:
        raise HTTPException(status_code=404, detail="找不到待確認的改期")
    calendar_events_coll.update_one({"_id": event["_id"]}, {"$set": {"status": "confirmed", "pending_change": None, "updated_at": datetime.now(timezone.utc)}})
    other_id = next(person for person in event["participants"] if person != req.user_id)
    match = find_accepted_match(req.user_id, other_id)
    coordination = match.get("date_coordination") or {}
    if coordination.get("calendar_event_id") == event_id:
        zone = get_timezone(event.get("timezone", "Asia/Taipei"))
        coordination.update({
            "status": "completed",
            "form": {
                "date": as_utc(event["start_at"]).astimezone(zone).date().isoformat(),
                "start_time": as_utc(event["start_at"]).astimezone(zone).strftime("%H:%M"),
                "end_time": as_utc(event["end_at"]).astimezone(zone).strftime("%H:%M"),
                "timezone": event.get("timezone", "Asia/Taipei"),
                "activity": event.get("activity", ""), "location": event.get("location", ""),
                "budget": event.get("budget", ""), "notes": event.get("notes", ""),
            },
            "confirmations": {person: True for person in event["participants"]},
        })
        from database import matches_coll
        from services.date_coordination_service import _sync_card
        matches_coll.update_one({"_id": match["_id"]}, {"$set": {"date_coordination": coordination}})
        _sync_card(match, coordination)
    return {"event": serialize_event(calendar_events_coll.find_one({"_id": event["_id"]}), req.user_id)}


@router.get("/settings")
def get_settings(user_id: str):
    profile = profiles_coll.find_one({"user_id": user_id}, {"mediator_calendar_access": 1}) or {}
    return {"mediator_calendar_access": profile.get("mediator_calendar_access", True)}


@router.patch("/settings")
def update_settings(req: CalendarSettingsRequest):
    profiles_coll.update_one({"user_id": req.user_id}, {"$set": {"mediator_calendar_access": req.mediator_calendar_access}}, upsert=True)
    return {"mediator_calendar_access": req.mediator_calendar_access}
