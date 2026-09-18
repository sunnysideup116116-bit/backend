"""Claim and deliver one safe proactive/mediator event for a user poll."""

from __future__ import annotations

import time
import re

from bson.objectid import ObjectId
from pymongo import ReturnDocument

from database import db, matches_coll, messages_coll, profiles_coll
from services.ayue_agent.proactive_care import consume_proactive_delivery
from services.chat_service import (
    generate_proposal_ai_room_id, generate_room_id, save_message,
    save_system_message_once,
)
from services.ai_room_service import (
    create_proposal_room, get_room, match_hub_room_id,
    match_hub_v1_enabled, most_recent_ai_room,
)
from services.mediator_event_service import claim_next_mediator_event
from services.match_reason_service import reason_for_viewer
from services.event_card_projection import public_event_card
from services.proposal_namespace import (
    EVENT_INVITATION_NAMESPACE, RELATIONSHIP_MATCH_NAMESPACE,
    namespace_for_document,
)
from services.match_card_projection import proposal_card_state
from services.relationship_engagement_service import (
    LEGACY_PRIVATE_PROBE_EVENT_TYPES,
    cleanup_legacy_private_probe_state,
    find_accepted_match,
    generate_mediator_private_room_id,
    queue_due_feedback,
    relationship_unread_field,
)


RELATIONSHIP_EVENT_TYPES = {
    "gentle_closure", "mutual_interest",
    "date_coordination_request", "date_coordination_result",
}
PROPOSAL_EVENT_TYPES = {"incoming_match_intro", "match_proposal", "incoming_match_interest"}
AUTOMATIC_PROPOSAL_SOURCES = {
    "automatic", "legacy_automatic", "legacy-proactive", "post_chat", "three_message",
}


def _proposal_source(match: dict) -> str:
    source = str(match.get("proposal_source") or "").strip().lower()
    search_job_id = str(match.get("search_job_id") or "").strip()
    if search_job_id:
        job = db["match_search_jobs"].find_one(
            {"job_id": search_job_id}, {"_id": 0, "source": 1},
        ) or {}
        job_source = str(job.get("source") or "").strip().lower()
        if job_source:
            return job_source
    return source


def _source_summary(match: dict, event: dict) -> str:
    """Create a short, public summary for a Hub card's provenance."""
    event_card = public_event_card(match)
    if event_card:
        title = str(event_card.get("title") or "").strip()
        date = str(event_card.get("start_at") or event_card.get("date") or "").strip()
        location = str(event_card.get("location") or "").strip()
        parts = [item for item in (title, date, location) if item]
        if parts:
            return " · ".join(parts)[:180]
    return str(event.get("message") or "近期配對提案")[:180]


def _public_match_basis(match: dict, user_id: str) -> dict:
    """Project the server-owned evidence contract without participant IDs."""
    raw = match.get("match_basis")
    if not isinstance(raw, dict):
        tier = str(match.get("recommendation_tier") or "")
        raw = {
            "level": "direct" if tier in {"grounded", "event_grounded"} else "adjacent",
            "need_evidence": [],
            "counterparty_evidence": [],
            "concrete_overlap": [],
            "cannot_infer": ["對方尚未同意這次介紹或活動安排。"],
        }
    allowed = {"direct", "adjacent", "insufficient"}
    level = str(raw.get("level") or "insufficient")
    if level not in allowed:
        level = "insufficient"

    def strings(value: object, limit: int = 3) -> list[str]:
        values = value if isinstance(value, list) else [value]
        output = []
        for item in values:
            text = str(item or "").strip()[:120]
            # Evidence summaries are UI text, never a contact channel or
            # account identifier. Redact common email/phone forms before the
            # Hub projection crosses the HTTP boundary.
            text = re.sub(
                r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
                "已隱藏聯絡方式",
                text,
            )
            text = re.sub(
                r"(?<!\d)(?:\+?886[-\s]?)?0?9\d{2}[-\s]?\d{3}[-\s]?\d{3}(?!\d)",
                "已隱藏聯絡方式",
                text,
            )
            text = re.sub(
                r"seed_user_[A-Za-z0-9_-]+|(?:user_id|match_id)\s*[:=]\s*\S+",
                "對方",
                text,
                flags=re.IGNORECASE,
            )
            if text and text not in output:
                output.append(text)
            if len(output) >= limit:
                break
        return output

    return {
        "level": level,
        "need_evidence": strings(raw.get("need_evidence")),
        "counterparty_evidence": strings(raw.get("counterparty_evidence")),
        "concrete_overlap": strings(raw.get("concrete_overlap")),
        "cannot_infer": strings(raw.get("cannot_infer"), 4),
    }


def _expire_automatic_proposal(match: dict) -> None:
    """Hide legacy automatic proposals without expiring an actionable proposal."""
    matches_coll.update_one(
        {"_id": match["_id"], "status": {"$in": ["draft", "pending"]}},
        {
            "$set": {
                "proposal_suppressed": True,
                "suppressed_at": time.time(),
                "suppressed_reason": "legacy_automatic_source",
            },
            "$unset": {"live_participants": ""},
        },
    )


def proactive_check(user_id: str, conversation_active: bool = False) -> dict:
    user_doc = profiles_coll.find_one({"user_id": user_id})
    if not user_doc:
        return {"has_new": False}
    # Remove legacy probe events/pending state on every poll.  The operation
    # is idempotent and also closes a race where an old event was queued before
    # this request read the profile.
    cleanup_legacy_private_probe_state(user_id, user_doc=user_doc)

    post_date_doc = profiles_coll.find_one_and_update(
        {"user_id": user_id, "post_date_followup_delivery.message": {"$exists": True}},
        {"$unset": {"post_date_followup_delivery": ""}},
        projection={"post_date_followup_delivery": 1},
        return_document=ReturnDocument.BEFORE,
    )
    post_date = (post_date_doc or {}).get("post_date_followup_delivery") or {}
    if not isinstance(post_date, dict):
        post_date = {}
    if post_date.get("message") and post_date.get("other_id"):
        return {
            "has_new": True,
            "surface": "relationship_private",
            "type": "post_date_followup",
            "other_id": post_date["other_id"],
            "message": post_date["message"],
            "metadata": {"event_type": "post_date_followup"},
        }

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

    event = None
    for _ in range(8):
        candidate = claim_next_mediator_event(user_id)
        if not candidate:
            break
        if _is_legacy_private_probe_event(candidate):
            # A producer racing with cleanup may have inserted one after the
            # profile update.  It has already been removed from the inbox by
            # the claim; skip it and keep looking for valid events.
            continue
        event = candidate
        break
    if event:
        return _deliver_claimed_event(user_id, event)
    if not conversation_active:
        marker = consume_proactive_delivery(user_id)
        if marker:
            response = {
                "has_new": True, "message": marker["message"], "type": "proactive_care",
                "surface": "global_mediator",
                "metadata": {"event_type": "proactive_care"},
            }
            if marker.get("origin_room_id"):
                response["origin_room_id"] = marker["origin_room_id"]
            return response
    return {"has_new": False}


def _is_legacy_private_probe_event(event: dict | None) -> bool:
    return (
        isinstance(event, dict)
        and str(event.get("type") or "") in LEGACY_PRIVATE_PROBE_EVENT_TYPES
    )


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
        "proposal_namespace": event.get("proposal_namespace"),
        "matches": event.get("matches", []), "actions": event.get("actions", []),
        "media": event.get("media"),
    }


def _safe_source_entry(
    saved: dict | None,
    *,
    room_id: str,
    match_id: str,
    destination_room_id: str,
    proposal_namespace: str,
) -> dict | None:
    """Project one saved source-room pointer for immediate chat merging.

    This response is consumed by a polling client before it reloads history.
    Keep it in the same shape as a saved message and copy only server-owned
    navigation metadata; participant ids and the full Hub card never cross
    this optional convenience field.
    """
    if not isinstance(saved, dict):
        return None
    message_id = str(saved.get("message_id") or saved.get("_id") or "").strip()
    safe_room = str(room_id or "").strip()[:240]
    safe_match = str(match_id or "").strip()[:128]
    if not safe_room or not message_id or not safe_match:
        return None
    namespace = str(proposal_namespace or "").strip()
    if namespace not in {RELATIONSHIP_MATCH_NAMESPACE, EVENT_INVITATION_NAMESPACE}:
        namespace = RELATIONSHIP_MATCH_NAMESPACE
    metadata = {
        "event_type": "match_proposal_ready",
        "destination_room_id": str(destination_room_id or "").strip()[:240],
        "proposal_room_id": str(destination_room_id or "").strip()[:240],
        "focus_match_id": safe_match,
        "match_id": safe_match,
        "proposal_namespace": namespace,
    }
    # Preserve the stable saved pointer text/timestamp. The response must
    # represent what was actually written, even when a retry reads it back.
    content = str(saved.get("content") or saved.get("message") or "").strip()[:2000]
    if not content:
        return None
    return {
        "room_id": safe_room,
        "message_id": message_id[:256],
        "sender_id": "ai_assistant",
        "content": content,
        "message_type": str(saved.get("message_type") or "text")[:40],
        "metadata": metadata,
        "timestamp": float(saved.get("timestamp") or saved.get("created_at") or time.time()),
    }


def _source_pointer_message(match: dict, *, fallback: str = "") -> str:
    """Use canonical proposal state when wording the original-room pointer."""
    delivery_mode = str(match.get("delivery_mode") or "").strip()
    status = str(match.get("status") or "").strip()
    if delivery_mode == "invite_on_match" and status == "pending":
        topic = str(
            ((match.get("search_context") or {}).get("invitation_topic") or "")
        ).strip()[:80]
        return (
            f"已替你送出「{topic}」邀請，正在等對方回覆。"
            if topic else "已替你送出邀請，正在等對方回覆。"
        )
    return fallback or "我找到一位可以介紹給你的人，介紹放在阿月牽線。"


def _deliver_claimed_event(user_id: str, event: dict) -> dict:
    if _is_legacy_private_probe_event(event):
        return {"has_new": False, "legacy_probe_suppressed": True}
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
    if _is_legacy_private_probe_event(event):
        return {"has_new": False, "legacy_probe_suppressed": True}
    event_type = event.get("type", "mediator_message")
    room_id = generate_mediator_private_room_id(user_id, other_id)

    delivered_message = event.get("message", "阿月有一則新消息。")
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
    if event_type == "date_coordination_request":
        profiles_coll.update_one({"user_id": user_id}, {"$set": {"pending_date_coordination": {
            "match_id": str(event_match["_id"]), "other_id": other_id,
            "stage": "availability", "data": {},
        }}})
    return {
        "has_new": True, "surface": "relationship_private", "other_id": other_id,
        "unread_count": unread_count, "message": delivered_message,
        "type": event_type, "metadata": message_metadata,
    }


def _proposal_origin_room(user_id: str, match: dict, event: dict) -> str:
    """Bind a per-viewer destination once so concurrent polling cannot split a card."""
    role = "initiator" if match.get("from_user") == user_id else "receiver"
    field = f"proposal_delivery_rooms.{role}"
    existing = (match.get("proposal_delivery_rooms") or {}).get(role)
    if existing and get_room(existing, user_id) is not None:
        return existing
    requested = str(event.get("origin_room_id") or "")
    destination = requested if requested and get_room(requested, user_id) is not None else most_recent_ai_room(
        user_id, include_proposal_rooms=False,
    )
    # A valid binding belongs to the user, is server-owned, and is never derived
    # from model output. Keep independent destinations for both participants.
    bound = matches_coll.find_one_and_update({
        "_id": match["_id"], field: existing if existing else {"$exists": False},
        "$or": [{"from_user": user_id}, {"to_user": user_id}],
    }, {"$set": {field: destination}}, return_document=ReturnDocument.AFTER)
    if not bound:
        bound = matches_coll.find_one({"_id": match["_id"]}) or {}
    room_id = (bound.get("proposal_delivery_rooms") or {}).get(role)
    if not room_id or get_room(room_id, user_id) is None:
        raise RuntimeError("proposal_destination_unavailable")
    return room_id


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
        if _proposal_source(live_match) in AUTOMATIC_PROPOSAL_SOURCES:
            _expire_automatic_proposal(live_match)
            return {"has_new": False, "stale": True, "automatic_proposal_suppressed": True}
        if (
            event_type == "incoming_match_intro"
            and namespace_for_document(live_match) == EVENT_INVITATION_NAMESPACE
        ):
            # Event invitations already carry their own grounded hook in the
            # actionable receiver card. Legacy queued intro events must not
            # become a second mediator card with no match/reason projection.
            return {
                "has_new": False,
                "deduplicated": True,
                "event_intro_suppressed": True,
            }
        requested_origin = str(event.get("origin_room_id") or "")
        source_room_id = (
            requested_origin
            if requested_origin and get_room(requested_origin, user_id) is not None
            else most_recent_ai_room(user_id, include_proposal_rooms=False)
        )
        is_initiator = live_match.get("from_user") == user_id
        viewer_reason = reason_for_viewer(live_match, user_id)
        if not viewer_reason:
            viewer_reason = "我找到一位可能適合你的人，想先問問你願不願意認識對方。"
        public_match = {
            "match_id": str(live_match["_id"]),
            "viewer_reason": viewer_reason,
            "reason_version": str(live_match.get("reason_version") or "legacy"),
            "proposal_namespace": namespace_for_document(live_match),
            "proposal_revision": int(live_match.get("proposal_revision", 0) or 0),
            "match_source_kind": str(
                live_match.get("match_source_kind")
                or ("event" if namespace_for_document(live_match) == EVENT_INVITATION_NAMESPACE
                    else "requested_topic" if (live_match.get("search_context") or {}).get("invitation_topic")
                    else "recent_context")
            ),
        }
        event_card = public_event_card(live_match)
        if event_card:
            public_match["event"] = event_card
        # The card is the canonical, viewer-bound projection.  Keep the
        # matching evidence opaque while exposing a short safety explanation
        # that is useful to the Hub's "詢問這張" flow.
        public_match["match_basis"] = _public_match_basis(live_match, user_id)
        public_match["source_room_id"] = source_room_id
        source_room = get_room(source_room_id, user_id) or {}
        source_room_title = str(
            source_room.get("title")
            or (live_match.get("source_room_title") if is_initiator else "")
            or ""
        )[:60]
        source_summary = (
            str(live_match.get("source_summary") or "").strip()
            or _source_summary(live_match, event)
        )[:180] if is_initiator else ""
        public_match["source_room_title"] = source_room_title
        public_match["source_summary"] = source_summary
        # Rebuild proposal metadata from the canonical match at delivery time.
        # Queued events cannot smuggle stale profile snippets, the opposite
        # direction reason, or participant identifiers into the public card.
        message_metadata["other_id"] = None
        # Every Hub proposal event variant projects the same canonical card.
        # The first intro and the actionable interest event therefore share one
        # durable card instead of allowing an empty intro card to race ahead.
        message_metadata["matches"] = [public_match]
        match_id = str(live_match["_id"])
        source_entry = None
        # Match Hub V1 owns both relationship and event invitations.  The
        # durable card is written once into the fixed room; old proposal rooms
        # remain readable for history and are only used by the rollback path.
        if match_hub_v1_enabled():
            hub_room_id = match_hub_room_id(user_id)
            hub_room = get_room(hub_room_id, user_id)
            # A hub projection is server-owned.  Requiring the explicit room
            # kind also keeps old test doubles/legacy deployments on their
            # existing origin-room compatibility path during rollout.
            if isinstance(hub_room, dict) and hub_room.get("room_kind") == "match_hub":
                # Persist the per-viewer source binding before saving the Hub
                # card. The status/state endpoints read match documents, so
                # message metadata alone cannot provide a stable source link.
                try:
                    source_room_id = _proposal_origin_room(user_id, live_match, event)
                except RuntimeError:
                    source_room_id = ""
                source_room = get_room(source_room_id, user_id) if source_room_id else None
                source_room = source_room if isinstance(source_room, dict) else {}
                source_room_title = str(
                    source_room.get("title")
                    or (live_match.get("source_room_title") if is_initiator else "")
                    or ""
                )[:60]
                source_summary = (
                    str(live_match.get("source_summary") or "").strip()
                    or _source_summary(live_match, event)
                )[:180] if is_initiator else ""
                public_match.update(
                    source_room_id=source_room_id,
                    source_room_title=source_room_title,
                    source_summary=source_summary,
                )
                if is_initiator and source_room_id:
                    matches_coll.update_one(
                        {"_id": live_match["_id"], "from_user": user_id},
                        {"$set": {
                            "source_room_id": source_room_id,
                            "source_room_title": source_room_title,
                            "source_summary": source_summary,
                        }},
                    )
                state = proposal_card_state(live_match, user_id)
                message_metadata.update(
                    match_id=match_id, other_id=None,
                    proposal_role="initiator" if live_match.get("from_user") == user_id else "receiver",
                    canonical_status=state["status"], stage=state["stage"],
                    proposal_revision=state["proposal_revision"], decision_action=state["decision_action"],
                    destination_room_id=hub_room_id, focus_match_id=match_id,
                    source_room_id=source_room_id,
                    source_room_title=source_room_title,
                    source_summary=source_summary,
                )
                hub_event_key = f"match-hub:{match_id}"
                saved = save_system_message_once(
                    hub_room_id, event.get("message", "阿月有一則新的媒合消息。"),
                    message_type="mediator_card",
                    metadata=message_metadata,
                    event_key=hub_event_key,
                )
                # A relationship proposal can still have been discovered from
                # the legacy/general room. Leave one lightweight, idempotent
                # pointer there. The receiver's incoming-interest event does
                # not know the initiator's source room, so it stays Hub-only.
                if (
                    event_type == "match_proposal"
                    and
                    namespace_for_document(live_match) == RELATIONSHIP_MATCH_NAMESPACE
                    and source_room_id
                    and source_room_id != hub_room_id
                ):
                    source_saved = save_system_message_once(
                        source_room_id,
                        _source_pointer_message(live_match),
                        message_type="text",
                        metadata={
                            "event_type": "match_proposal_ready",
                            "proposal_room_id": hub_room_id,
                            "destination_room_id": hub_room_id,
                            "focus_match_id": match_id,
                            "proposal_namespace": namespace_for_document(live_match),
                        },
                        event_key=f"{hub_event_key}:entry",
                    )
                    source_entry = _safe_source_entry(
                        source_saved,
                        room_id=source_room_id,
                        match_id=match_id,
                        destination_room_id=hub_room_id,
                        proposal_namespace=namespace_for_document(live_match),
                    )
                return {
                    "has_new": True, "surface": "global_mediator", "message": event.get("message"),
                    "type": event_type, "matches": message_metadata.get("matches", []),
                    "metadata": message_metadata, "origin_room_id": hub_room_id,
                    "destination_room_id": hub_room_id, "focus_match_id": match_id,
                    "source_room_id": source_room_id,
                    **({"source_entry": source_entry} if source_entry else {}),
                    "message_id": saved.get("message_id"),
                    "debug_info": event.get("debug_info", []),
                }
        if namespace_for_document(live_match) == RELATIONSHIP_MATCH_NAMESPACE:
            origin_room_id = _proposal_origin_room(user_id, live_match, event)
            state = proposal_card_state(live_match, user_id)
            message_metadata.update(
                match_id=match_id, other_id=None,
                proposal_role="initiator" if live_match.get("from_user") == user_id else "receiver",
                canonical_status=state["status"], stage=state["stage"],
                proposal_revision=state["proposal_revision"], decision_action=state["decision_action"],
            )
            saved = save_system_message_once(
                origin_room_id, event.get("message", "阿月有一則新的媒合消息。"),
                message_type="text" if event_type == "incoming_match_intro" else "mediator_card",
                metadata=message_metadata,
                event_key=str(event.get("event_key") or f"delivery:{event.get('event_id') or event_type}:{match_id}"),
            )
            if event_type == "match_proposal":
                source_entry = _safe_source_entry(
                    saved,
                    room_id=origin_room_id,
                    match_id=match_id,
                    destination_room_id=origin_room_id,
                    proposal_namespace=namespace_for_document(live_match),
                )
            return {
                "has_new": True, "surface": "global_mediator", "message": event.get("message"),
                "type": event_type, "matches": message_metadata.get("matches", []),
                "metadata": message_metadata, "origin_room_id": origin_room_id,
                **({"source_entry": source_entry} if source_entry else {}),
                "message_id": saved.get("message_id"),
            }
        # Rollback path for deployments with MATCH_HUB_V1 disabled.  Existing
        # proposal rooms remain intact and can be read without migration.
        proposal_room_id = generate_proposal_ai_room_id(user_id, match_id)
        create_proposal_room(
            user_id,
            match_id,
            event.get("message", "阿月有一則新的媒合消息。"),
            metadata=message_metadata,
            event_key=event.get("event_key") or f"delivery:{event.get('event_id') or event_type}:{match_id}",
        )
        if origin_room_id != proposal_room_id:
            ready_metadata = {
                "event_type": "match_proposal_ready",
                "proposal_room_id": proposal_room_id,
                "match_id": match_id,
            }
            save_system_message_once(
                origin_room_id,
                "我找到一位可以介紹給你的人，提案已經整理好了。",
                metadata=ready_metadata,
                event_key=f"{event.get('event_key') or event.get('event_id') or match_id}:ready",
            )
        message_metadata["proposal_room_id"] = proposal_room_id
        return {
            "has_new": True, "surface": "global_mediator", "message": event.get("message"),
            "type": event_type, "matches": message_metadata.get("matches", []),
            "metadata": message_metadata, "proposal_room_id": proposal_room_id,
            "origin_room_id": origin_room_id,
            "debug_info": event.get("debug_info", []),
        }
    requested_origin = str(event.get("origin_room_id") or "")
    origin_room_id = (
        requested_origin
        if requested_origin and get_room(requested_origin, user_id) is not None
        else most_recent_ai_room(user_id)
    )
    save_system_message_once(
        origin_room_id,
        event.get("message", "阿月有一則新的媒合消息。"),
        message_type="text",
        metadata=message_metadata,
        event_key=str(
            event.get("event_key")
            or event.get("event_id")
            or f"legacy:{event_type}:{event.get('created_at', 0)}"
        ),
    )
    return {
        "has_new": True, "surface": "global_mediator", "message": event.get("message"),
        "type": event_type, "matches": message_metadata.get("matches", []),
        "metadata": message_metadata, "origin_room_id": origin_room_id,
        "debug_info": event.get("debug_info", []),
    }
