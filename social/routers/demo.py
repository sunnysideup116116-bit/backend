"""Demo-only maintenance endpoints, isolated from user chat routing."""

import logging
from fastapi import APIRouter, HTTPException

from database import matches_coll, messages_coll, profiles_coll
from services.demo_cleanup_service import (
    DemoCleanupError,
    clear_all_demo_state,
    clear_graph,
)
from services.event_cycle_service import EventCycleError, reset_event_inventory
from services.event_discovery_service import DEFAULT_REGION, DEFAULT_WINDOW_DAYS, SUPPORTED_CATEGORIES
from services.event_discovery_job_service import (
    enqueue_event_discovery_job, event_discovery_job_snapshot,
)
from services.event_opportunity_service import scan_event_opportunities


router = APIRouter()
LOGGER = logging.getLogger(__name__)
def _event_job_snapshot() -> dict:
    return event_discovery_job_snapshot()


def _cleanup_error(exc: DemoCleanupError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code})


@router.post("/demo/clear_graph")
def clear_graph_data():
    try:
        return {"status": "success", "graph": clear_graph()}
    except DemoCleanupError as exc:
        raise _cleanup_error(exc) from exc


@router.post("/demo/clear_all")
def clear_all_data():
    try:
        return clear_all_demo_state()
    except DemoCleanupError as exc:
        raise _cleanup_error(exc) from exc
    except Exception as exc:
        # Keep the destructive endpoint fail-closed and return a stable code
        # even if a newly added cleanup hook forgets to wrap its exception.
        LOGGER.exception("unhandled demo cleanup failure: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "demo_cleanup_failed"}) from exc


@router.post("/demo/reset_db_state")
def reset_db_state():
    messages_coll.delete_many({"room_id": {"$regex": "mediator_private"}})
    matches_coll.update_many({}, {"$unset": {"date_coordination": ""}})
    profiles_coll.update_many({}, {"$set": {"mediator_inbox": []}})
    print("\n[DEBUG] Demo Tool: Database reset successfully!\n")
    return {"status": "success", "message": "DB state reset"}


@router.post("/demo/events/reset")
def reset_all_event_demo_data(confirm: bool = False):
    """Reset Event nodes plus Event-opportunity Mongo state, never User nodes."""
    if not confirm:
        raise HTTPException(status_code=400, detail="confirm=true is required")
    if _event_job_snapshot().get("state") in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="活動建圖仍在執行中")
    try:
        return reset_event_inventory()
    except EventCycleError as exc:
        raise HTTPException(status_code=503, detail=exc.code) from exc


@router.post("/demo/events/discover/start")
def start_event_discovery_job():
    """Queue the long pipeline for the dedicated Event worker."""
    return enqueue_event_discovery_job(
        region=DEFAULT_REGION, window_days=DEFAULT_WINDOW_DAYS,
        categories=list(SUPPORTED_CATEGORIES), source="demo",
    )


@router.get("/demo/events/discover/status")
def get_event_discovery_job_status():
    return {"status": "success", **_event_job_snapshot()}


@router.post("/demo/events/invitations/scan")
def send_event_demo_invitations(confirm: bool = False, max_proposals: int = 3):
    if not confirm:
        raise HTTPException(status_code=400, detail="confirm=true is required")
    if _event_job_snapshot().get("state") in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="請等待活動建圖完成")
    return scan_event_opportunities(max_proposals=max(1, min(int(max_proposals or 3), 10)))
