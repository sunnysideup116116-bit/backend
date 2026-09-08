"""Recover Event cards from canonical matches, independent of app polls."""

import os
import threading
import time
import uuid
from pymongo import ReturnDocument
from database import matches_coll, messages_coll, profiles_coll
from services.ai_room_service import match_hub_room_id, match_hub_v1_enabled
from services.proactive_delivery_service import _deliver_global_event, _metadata
from services.proposal_namespace import EVENT_INVITATION_NAMESPACE

_stop = threading.Event()
_thread = None


def deliver_event_proposals_once(limit=10):
    """Ack saved cards only. Draft exposes initiator; pending exposes receiver.

    Canonical recovery covers crashes between match insertion and inbox enqueue.
    Stable room/match keys deduplicate a concurrent legacy app poll.
    """
    if not match_hub_v1_enabled():
        return {"status": "disabled", "delivered": 0}
    counts = {"status": "success", "delivered": 0, "failed": 0}
    for _ in range(max(1, min(int(limit), 50))):
        now = time.time()
        token = uuid.uuid4().hex
        doc = matches_coll.find_one_and_update(
            {"proposal_namespace": EVENT_INVITATION_NAMESPACE,
             "proposal_suppressed": {"$ne": True},
             "status": {"$in": ["draft", "pending"]},
             "$and": [
                 {"$or": [{"status": "draft", "event_delivery.initiator": {"$ne": True}},
                           {"status": "pending", "event_delivery.receiver": {"$ne": True}}]},
                 {"$or": [{"event_delivery.lease_until": {"$exists": False}},
                           {"event_delivery.lease_until": {"$lte": now}}]},
                 {"$or": [{"event_delivery.retry_at": {"$exists": False}},
                           {"event_delivery.retry_at": {"$lte": now}}]},
             ]},
            {"$set": {"event_delivery.token": token, "event_delivery.lease_until": now + 120},
             "$inc": {"event_delivery.attempts": 1}},
            sort=[("created_at", 1)], return_document=ReturnDocument.AFTER,
        )
        if not doc:
            break
        role = "initiator" if doc["status"] == "draft" else "receiver"
        user = doc["from_user"] if role == "initiator" else doc["to_user"]
        mid = str(doc["_id"])
        event = {"type": "match_proposal" if role == "initiator" else "incoming_match_interest",
                 "match_id": mid, "proposal_role": role,
                 "proposal_namespace": EVENT_INVITATION_NAMESPACE,
                 "event_key": f"event-delivery:{mid}:{role}",
                 "message": "阿月看到一個活動，也想到一位可能接得上你的人。"}
        guard = {"_id": doc["_id"], "event_delivery.token": token}
        try:
            _deliver_global_event(user, event, _metadata(event))
            saved = messages_coll.find_one({
                "room_id": match_hub_room_id(user), "message_type": "mediator_card",
                "metadata.match_id": mid,
            }, {"_id": 1})
            if not saved:
                raise RuntimeError("event_card_not_persisted")
            profiles_coll.update_one({"user_id": user}, {"$pull": {"mediator_inbox": {
                "match_id": mid, "type": {"$in": ["match_proposal", "incoming_match_intro", "incoming_match_interest"]},
            }}})
            matches_coll.update_one(guard, {
                "$set": {f"event_delivery.{role}": True, f"event_delivery.{role}_at": time.time()},
                "$unset": {"event_delivery.token": "", "event_delivery.lease_until": "",
                           "event_delivery.retry_at": "", "event_delivery.error": ""},
            })
            counts["delivered"] += 1
        except Exception as exc:
            attempts = int((doc.get("event_delivery") or {}).get("attempts", 1))
            matches_coll.update_one(guard, {
                "$set": {"event_delivery.error": type(exc).__name__,
                         "event_delivery.retry_at": time.time() + min(3600, 30 * 2 ** min(attempts, 7))},
                "$unset": {"event_delivery.token": "", "event_delivery.lease_until": ""},
            })
            counts["failed"] += 1
    if counts["failed"]:
        counts["status"] = "partial"
    return counts


def _loop():
    while not _stop.wait(10):
        try:
            deliver_event_proposals_once()
        except Exception as exc:
            print(f"[EVENT_DELIVERY] retryable error={type(exc).__name__}", flush=True)


def start_event_delivery_worker():
    global _thread
    if os.getenv("EVENT_DELIVERY_WORKER_ENABLED", "on").lower() not in {"on", "true", "1"}:
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="event-delivery", daemon=True)
    _thread.start()


def stop_event_delivery_worker():
    _stop.set()
    if _thread:
        _thread.join(timeout=3)
