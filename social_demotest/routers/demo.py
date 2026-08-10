"""Demo-only maintenance endpoints, isolated from user chat routing."""

import logging

from fastapi import APIRouter, HTTPException

from database import matches_coll, messages_coll, profiles_coll
from services.demo_cleanup_service import (
    DemoCleanupError,
    clear_all_demo_state,
    clear_graph,
)


router = APIRouter()
LOGGER = logging.getLogger(__name__)


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
