"""Demo-only maintenance endpoints, isolated from user chat routing."""

from fastapi import APIRouter

from database import matches_coll, messages_coll, profiles_coll


router = APIRouter()


@router.post("/demo/reset_db_state")
def reset_db_state():
    messages_coll.delete_many({"room_id": {"$regex": "mediator_private"}})
    matches_coll.update_many({}, {"$unset": {"date_coordination": ""}})
    profiles_coll.update_many({}, {"$set": {"mediator_inbox": []}})
    print("\n[DEBUG] Demo Tool: Database reset successfully!\n")
    return {"status": "success", "message": "DB state reset"}
