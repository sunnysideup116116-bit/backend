"""Claim and deliver one safe proactive/mediator event for a user poll."""

from __future__ import annotations

import time

from bson.objectid import ObjectId
from pymongo import ReturnDocument

from database import matches_coll, messages_coll, profiles_coll
from services.ayue_agent.proactive_care import consume_proactive_delivery
from services.chat_service import generate_proposal_ai_room_id, generate_room_id, save_message
from services.ai_room_service import create_proposal_room, most_recent_ai_room
from services.mediator_event_service import claim_next_mediator_event
from services.match_reason_service import reason_for_viewer
from services.relationship_engagement_service import (
    find_accepted_match,
    generate_mediator_private_room_id,
    participant_probe_field,
    participant_probe_state,
    queue_due_feedback,
    relationship_unread_field,
)


RELATIONSHIP_EVENT_TYPES = {
    "feedback_request", "feedback_consent_request", "probe_result", "gentle_closure",
    "mutual_interest", "probe_question", "date_coordination_request", "date_coordination_result",
}
PROPOSAL_EVENT_TYPES = {"incoming_match_intro", "match_proposal", "incoming_match_interest"}


def proactive_check(user_id: str, conversation_active: bool = False) -> dict:
    user_doc = profiles_coll.find_one({"user_id": user_id})
    if not user_doc:
        return {"has_new": False}

    queue_due_feedback(user_id)
    notice_doc = profiles_coll.find_one_and_update(
        {"user_id": user_id, "memory_notices.0": {"$exists": True}},
        {"$pop": {"memory_notices": -1}},
        projection={"memory_notices": 1},
        return_document=ReturnDocument.BEFORE,
    )
    if notice_doc and notice_doc.get("memory_notices"):
        notice = notice_doc["memory_notices"][0]
        return {
            "has_new": True, "surface": "ephemeral_notice", "type": "memory_learned",
            "message": notice.get("message"), "memory": notice.get("memory"),
        }

    event = claim_next_mediator_event(user_id)
    if event:
        return _deliver_claimed_event(user_id, event)
    if not conversation_active:
        marker = consume_proactive_delivery(user_id)
        if marker:
            return {
                "has_new": True, "message": marker["message"], "type": "proactive_care",
                "surface": "global_mediator", "metadata": {"event_type": "proactive_care"},
            }
    return {"has_new": False}


def _event_match(user_id: str, event: dict) -> dict | None:
    if event.get("match_id"):
        try:
            match = matches_coll.find_one({"_id": ObjectId(event["match_id"])})
            if match:
                return match
        except Exception:
            pass
    if event.get("other_id"):
        return find_accepted_match(user_id, event["other_id"])
    return None


def _metadata(event: dict) -> dict:
    return {
        "event_id": event.get("event_id"), "event_type": event.get("type", "mediator_message"),
        "match_id": event.get("match_id"), "other_id": event.get("other_id"),
        "probe_id": event.get("probe_id"), "proposal_role": event.get("proposal_role"),
        "matches": event.get("matches", []), "actions": event.get("actions", []),
        "media": event.get("media"),
    }


def _deliver_claimed_event(user_id: str, event: dict) -> dict:
    event_type = event.get("type", "mediator_message")
    other_id = event.get("other_id")
    event_match = _event_match(user_id, event)
    relationship_private = bool(
        other_id and event_match and event_match.get("status") == "accepted"
        and (event_type in RELATIONSHIP_EVENT_TYPES or event.get("match_id")),
    )
    message_metadata = _metadata(event)
    if relationship_private:
        return _deliver_relationship_event(user_id, other_id, event, event_match, message_metadata)
    return _deliver_global_event(user_id, event, message_metadata)


def _deliver_relationship_event(
    user_id: str, other_id: str, event: dict, event_match: dict, message_metadata: dict,
) -> dict:
    event_type = event.get("type", "mediator_message")
    room_id = generate_mediator_private_room_id(user_id, other_id)
    if event_type in {"feedback_request", "probe_question"}:
        profiles_coll.update_one(
            {"user_id": user_id},
            {"$pull": {"mediator_inbox": {
                "type": {"$in": ["feedback_request", "probe_question"]},
                "match_id": event.get("match_id"),
            }}},
        )
        state = participant_probe_state(event_match, user_id)
        if event.get("probe_id") and state.get("probe_id") != event.get("probe_id"):
            return {"has_new": False, "deduplicated": True}
        asked_at = float(state.get("asked_at", 0))
        duplicate_query = {"room_id": room_id, "metadata.event_type": event_type}
        if event.get("probe_id"):
            duplicate_query["metadata.probe_id"] = event["probe_id"]
        else:
            duplicate_query["timestamp"] = {"$gte": asked_at - 1}
        duplicate = asked_at and messages_coll.find_one(duplicate_query)
        if duplicate and state.get("status") in {"awaiting_answer", "awaiting_sentiment", "awaiting_consent"}:
            return {"has_new": False, "deduplicated": True}

    delivered_message = event.get("message", "阿月有一則新消息。")
    if event_type == "feedback_request":
        delivered_message = "你跟這位聊起來感覺如何？"
        message_metadata["actions"] = []
    message_type = "gif" if event_type == "match_connected_gif" else (
        "mediator_card" if message_metadata["actions"] else "text"
    )
    save_message(
        room_id, "ai_assistant", delivered_message,
        message_type=message_type,
        metadata=message_metadata,
    )
    unread_field = relationship_unread_field(event_match, user_id)
    updated_match = matches_coll.find_one_and_update(
        {"_id": event_match["_id"]}, {"$inc": {unread_field: 1}},
        return_document=ReturnDocument.AFTER,
    ) or event_match
    role = "from" if event_match.get("from_user") == user_id else "to"
    unread_count = int((updated_match.get("private_unread", {}) or {}).get(role, 1))
    if event_type in {"feedback_request", "probe_question"}:
        state = participant_probe_state(event_match, user_id)
        requester_id = event.get("requester_id") or state.get("requester_id")
        probe_kind = event.get("probe_kind") or state.get("kind", "sentiment")
        stage = "sentiment" if probe_kind == "sentiment" else "probe_answer"
        profiles_coll.update_one({"user_id": user_id}, {"$set": {"pending_private_feedback": {
            "match_id": str(event_match["_id"]), "other_id": other_id, "stage": stage,
            "kind": probe_kind, "origin": event.get("origin", "auto"),
            "requester_id": requester_id, "probe_id": event.get("probe_id"),
        }}})
        matches_coll.update_one({"_id": event_match["_id"]}, {"$set": {
            participant_probe_field(event_match, user_id) + ".status": "awaiting_sentiment" if stage == "sentiment" else "awaiting_answer",
            participant_probe_field(event_match, user_id) + ".asked_at": time.time(),
        }})
    elif event_type == "date_coordination_request":
        profiles_coll.update_one({"user_id": user_id}, {"$set": {"pending_date_coordination": {
            "match_id": str(event_match["_id"]), "other_id": other_id,
            "stage": "availability", "data": {},
        }}})
    return {
        "has_new": True, "surface": "relationship_private", "other_id": other_id,
        "unread_count": unread_count, "message": delivered_message,
        "type": event_type, "metadata": message_metadata,
    }


def _deliver_global_event(user_id: str, event: dict, message_metadata: dict) -> dict:
    event_type = event.get("type", "mediator_message")
    if event_type in PROPOSAL_EVENT_TYPES:
        live_match = None
        if event.get("match_id"):
            try:
                live_match = matches_coll.find_one({
                    "_id": ObjectId(event["match_id"]), "status": {"$in": ["draft", "pending"]},
                    "$or": [{"from_user": user_id}, {"to_user": user_id}],
                })
            except Exception:
                pass
        if not live_match:
            return {"has_new": False, "stale": True}
        viewer_reason = reason_for_viewer(live_match, user_id)
        if not viewer_reason:
            viewer_reason = "我找到一位可能適合你的人，想先問問你願不願意認識對方。"
        public_match = {
            "match_id": str(live_match["_id"]),
            "viewer_reason": viewer_reason,
            "reason_version": str(live_match.get("reason_version") or "legacy"),
        }
        # Rebuild proposal metadata from the canonical match at delivery time.
        # Queued events cannot smuggle stale profile snippets, the opposite
        # direction reason, or participant identifiers into the public card.
        if event_type != "incoming_match_intro":
            message_metadata["other_id"] = None
            message_metadata["matches"] = [public_match]
        match_id = str(live_match["_id"])
        # Proposals get their own dedicated AI room so the default 找阿月配對
        # room is never interrupted by incoming matchmaking events.
        create_proposal_room(
            user_id,
            match_id,
            event.get("message", "阿月有一則新的媒合消息。"),
            metadata=message_metadata,
            event_key=event.get("event_key") or f"delivery:{event.get('event_id') or event_type}:{match_id}",
        )
        return {
            "has_new": True, "surface": "global_mediator", "message": event.get("message"),
            "type": event_type, "matches": message_metadata.get("matches", []),
            "metadata": message_metadata, "proposal_room_id": generate_proposal_ai_room_id(user_id, match_id),
            "debug_info": event.get("debug_info", []),
        }
    room_id = most_recent_ai_room(user_id)
    save_message(
        room_id, "ai_assistant", event.get("message", "阿月有一則新的媒合消息。"),
        message_type="text", metadata=message_metadata,
    )
    return {
        "has_new": True, "surface": "global_mediator", "message": event.get("message"),
        "type": event_type, "matches": message_metadata.get("matches", []),
        "metadata": message_metadata, "debug_info": event.get("debug_info", []),
    }
