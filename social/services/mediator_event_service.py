import time
import uuid

from services.profile_writer import update_profile as write_profile
from database import profiles_coll


EVENT_PRIORITIES = {
    "incoming_match_intro": 105,
    "match_proposal": 100,
    "incoming_match_interest": 100,
    "match_connected": 90,
    "event_invitation_accepted": 90,
    "match_connected_gif": 85,
    "probe_result": 80,
    "gentle_closure": 80,
    "mutual_interest": 80,
    "date_coordination_result": 75,
    "date_coordination_request": 70,
    "date_coordination_cancelled": 70,
    "match_search_failed": 60,
    "match_search_empty": 60,
    "match_search_blocked": 50,
    "feedback_consent_request": 45,
    "feedback_request": 35,
    "probe_question": 30,
}
LEGACY_PRIVATE_PROBE_EVENT_TYPES = frozenset({
    "probe_question",
    "feedback_request",
    "feedback_consent_request",
    "probe_result",
})


def event_priority(event_type: str) -> int:
    return EVENT_PRIORITIES.get(event_type, 40)


def queue_mediator_event(user_id: str, message: str, event_type: str, **extra):
    # These events belonged to the removed Private probe workflow.  Keeping
    # this guard at the shared enqueue boundary closes producers that bypass
    # the old engagement helpers.
    if str(event_type or "") in LEGACY_PRIVATE_PROBE_EVENT_TYPES:
        return None
    event = {
        "event_id": uuid.uuid4().hex,
        "type": event_type,
        "message": message,
        "priority": event_priority(event_type),
        "created_at": time.time(),
        **extra,
    }
    event_key = event.get("event_key")
    query = {"user_id": user_id}
    if event_key:
        # A retried transition must not enqueue a second card for the same state change.
        query["mediator_inbox.event_key"] = {"$ne": event_key}
    result = write_profile(profiles_coll,
        query,
        {"$push": {"mediator_inbox": {"$each": [event], "$sort": {"priority": -1, "created_at": 1}}}},
        upsert=not bool(event_key),
    )
    return event if result.modified_count else None

def claim_next_mediator_event(user_id: str):
    """Claim one event by id so concurrent browser polls cannot deliver it twice."""
    for _ in range(4):
        profile = profiles_coll.find_one(
            {"user_id": user_id, "mediator_inbox.0": {"$exists": True}},
            {"mediator_inbox": 1},
        ) or {}
        inbox = profile.get("mediator_inbox") or []
        if not inbox:
            return None
        event = min(
            inbox,
            key=lambda item: (
                -int(item.get("priority", event_priority(item.get("type", "")))),
                float(item.get("created_at", 0)),
            ),
        )
        event_id = event.get("event_id")
        if event_id:
            result = profiles_coll.update_one(
                {"user_id": user_id, "mediator_inbox.event_id": event_id},
                {"$pull": {"mediator_inbox": {"event_id": event_id}}},
            )
        else:
            # Compatibility path for events created before event_id was introduced.
            result = profiles_coll.update_one(
                {"user_id": user_id, "mediator_inbox": event},
                {"$pull": {"mediator_inbox": event}},
            )
        if result.modified_count:
            event.setdefault("priority", event_priority(event.get("type", "")))
            return event
    return None
