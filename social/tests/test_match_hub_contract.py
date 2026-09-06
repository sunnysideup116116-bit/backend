"""Match Hub cards keep canonical state and safe navigation projections."""

import json
from unittest.mock import patch

import pytest
from bson import ObjectId

from routers import match as routes
from services import notification_service
from services import proactive_delivery_service as delivery
from tests.match_flow_store import Collection


def _proposal(status="pending", *, viewer="owner", namespace="relationship_match", revision=0, **extra):
    return {
        "_id": ObjectId("64f000000000000000000091"),
        "from_user": "owner",
        "to_user": "candidate",
        "status": status,
        "proposal_namespace": namespace,
        "proposal_revision": revision,
        "reason": "先看看這張提案",
        "receiver_reason": "先看看這張提案",
        "match_context_snapshot": {
            "target": {"user_id": "owner", "current_context": "想認識新朋友"},
            "candidate": {"user_id": "candidate", "current_context": "最近想聊天"},
        },
        **extra,
    }


@pytest.mark.parametrize(
    ("status", "viewer", "expected_stage"),
    [
        ("draft", "owner", "waiting_user"),
        ("pending", "owner", "waiting_other"),
        ("pending", "candidate", "incoming_decision"),
    ],
)
def test_active_hub_card_uses_canonical_status_for_both_participants(
    status, viewer, expected_stage,
):
    document = _proposal(status, revision=0)
    with patch.object(routes, "public_display_name", return_value="對方"), \
            patch.object(routes, "reason_for_viewer", return_value="先看看這張提案"):
        card = routes.build_active_proposal_card(document, viewer)

    assert card["status"] == status
    assert card["canonical_status"] == status
    assert card["stage"] == expected_stage
    assert card["proposal_revision"] == 0


def test_match_status_hub_cards_include_canonical_state(monkeypatch):
    document = _proposal("pending", revision=0)
    monkeypatch.setattr(routes, "reconcile_match_state", lambda _user: document)
    monkeypatch.setattr(routes, "_single_live_namespace_proposal", lambda *_args: None)
    monkeypatch.setattr(routes, "public_match_search_status", lambda _user: {
        "status": "idle", "reason_code": "", "cancellable": False,
    })
    monkeypatch.setattr(routes, "get_match_status_snapshot", lambda _user: {
        "state": "waiting_other", "scope": "relationship_match",
        "is_terminal": False, "chat_opened": False, "counterparty": "對方",
        "reason_code": "",
    })
    monkeypatch.setattr(routes, "public_display_name", lambda _user: "對方")
    monkeypatch.setattr(routes, "reason_for_viewer", lambda *_args: "先看看這張提案")

    result = routes.get_match_status("owner")

    assert result["hub_cards"][0]["status"] == "pending"
    assert result["hub_cards"][0]["canonical_status"] == "pending"
    assert result["hub_cards"][0]["proposal_revision"] == 0


def test_terminal_state_keeps_safe_topic_and_public_event_details(monkeypatch):
    match_id = ObjectId("64f000000000000000000092")
    document = _proposal(
        "accepted",
        namespace="event_invitation",
        revision=0,
        proposal_source="event_opportunity",
        search_context={"invitation_topic": "衝浪"},
        last_decision={"from": "pending", "to": "accepted", "action": "accept", "actor": "candidate"},
        event_snapshot={
            "event_id": "private-event-id",
            "title": "港邊運動日",
            "venue": "高雄港",
            "region": "高雄",
            "category": "運動",
            "starts_at": 1_800_000_000,
            "ends_at": 1_800_003_600,
            "time_precision": "datetime",
            "source_url": "https://example.com/event",
        },
    )
    document["_id"] = match_id
    matches = Collection([document])
    monkeypatch.setattr(routes, "matches_coll", matches)
    monkeypatch.setattr(routes, "public_display_name", lambda _user: "對方")
    monkeypatch.setattr(routes, "proposal_display_name", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(routes, "get_room", lambda *_args: {
        "room_id": "ai_room::owner::source", "title": "和阿月聊聊",
    })

    result = routes.get_single_match_state("owner", str(match_id))

    assert result["status"] == "accepted"
    assert result["canonical_status"] == "accepted"
    assert result["invitation_topic"] == "衝浪"
    assert result["event"]["title"] == "港邊運動日"
    assert result["event"]["venue"] == "高雄港"
    assert result["event"]["category"] == "運動"
    assert result["event"]["starts_at"] == 1_800_000_000.0
    assert "private-event-id" not in json.dumps(result, ensure_ascii=False)


def test_source_projection_rejects_foreign_room_and_opposite_conversation(monkeypatch):
    match_id = ObjectId("64f000000000000000000093")
    document = _proposal(
        "declined",
        revision=0,
        source_room_id="ai_room::candidate::private",
        source_summary="候選人的原對話不應外洩",
    )
    document["_id"] = match_id
    monkeypatch.setattr(routes, "matches_coll", Collection([document]))
    monkeypatch.setattr(routes, "get_room", lambda *_args: None)
    monkeypatch.setattr(routes, "public_display_name", lambda _user: "對方")
    monkeypatch.setattr(routes, "proposal_display_name", lambda *_args, **_kwargs: "")

    result = routes.get_single_match_state("owner", str(match_id))

    assert result["source_room_id"] == ""
    assert result["source_summary"] == ""
    assert "候選人的原對話不應外洩" not in json.dumps(result, ensure_ascii=False)
    assert "ai_room::candidate::private" not in json.dumps(result, ensure_ascii=False)


def test_receiver_source_projection_hides_initiator_global_title_and_summary(monkeypatch):
    document = _proposal(
        "pending",
        source_room_id="ai_room::owner::origin",
        source_room_title="發起者原始聊天室",
        source_summary="發起者原始對話摘要不應外洩",
        proposal_delivery_rooms={"receiver": "ai_room::candidate::bound"},
    )

    def room_for_viewer(room_id, user_id):
        if room_id == "ai_room::candidate::bound" and user_id == "candidate":
            return {"room_id": room_id, "user_id": user_id, "title": "受邀者自己的聊天室"}
        return None

    monkeypatch.setattr(routes, "get_room", room_for_viewer)

    projected = routes._proposal_source_projection(document, "candidate")

    assert projected == {
        "source_room_id": "ai_room::candidate::bound",
        "source_room_title": "受邀者自己的聊天室",
        "source_summary": "",
    }
    assert "發起者原始" not in json.dumps(projected, ensure_ascii=False)


def test_hub_delivery_persists_source_binding_for_status_and_state(monkeypatch):
    match_id = ObjectId("64f000000000000000000095")
    document = _proposal(
        "draft",
        source_summary="這次由發起者的原始對話產生",
        source_room_title="舊的來源標題",
    )
    document["_id"] = match_id
    matches = Collection([document])
    hub_room_id = "ai_room::owner::match_hub"
    source_room_id = "ai_room::owner::origin"
    receiver_source_room_id = "ai_room::candidate::origin"

    def room_for_viewer(room_id, user_id):
        if room_id == f"ai_room::{user_id}::match_hub":
            return {
                "room_id": room_id, "user_id": user_id,
                "room_kind": "match_hub", "title": "阿月牽線",
            }
        if room_id == source_room_id and user_id == "owner":
            return {
                "room_id": room_id, "user_id": user_id,
                "room_kind": "conversation", "title": "發起者原始聊天室",
            }
        if room_id == receiver_source_room_id and user_id == "candidate":
            return {
                "room_id": room_id, "user_id": user_id,
                "room_kind": "conversation", "title": "受邀者自己的聊天室",
            }
        return None

    event = {
        "event_id": "hub-source-1",
        "event_key": "hub-source-job-1",
        "type": "match_proposal",
        "match_id": str(match_id),
        "origin_room_id": source_room_id,
        "message": "找到一位人選。",
    }
    monkeypatch.setattr(delivery, "matches_coll", matches)
    monkeypatch.setattr(delivery, "get_room", room_for_viewer)
    monkeypatch.setattr(delivery, "match_hub_v1_enabled", lambda: True)
    monkeypatch.setattr(delivery, "match_hub_room_id", lambda user: f"ai_room::{user}::match_hub")
    monkeypatch.setattr(
        delivery,
        "most_recent_ai_room",
        lambda user, **_kwargs: receiver_source_room_id if user == "candidate" else source_room_id,
    )
    monkeypatch.setattr(delivery, "reason_for_viewer", lambda *_args: "先看看這張提案")
    monkeypatch.setattr(delivery, "save_system_message_once", lambda *_args, **_kwargs: {
        "message_id": "hub-card-1",
    })

    response = delivery._deliver_global_event("owner", event, delivery._metadata(event))
    stored = matches.find_one({"_id": match_id})

    assert response["origin_room_id"] == hub_room_id
    assert stored["proposal_delivery_rooms"]["initiator"] == source_room_id
    assert stored["source_room_id"] == source_room_id
    assert stored["source_room_title"] == "發起者原始聊天室"

    matches.update_one({"_id": match_id}, {"$set": {"status": "pending"}})
    receiver_event = {
        **event,
        "type": "incoming_match_interest",
        # The initiator's room is not a valid receiver-owned source.
        "origin_room_id": source_room_id,
    }
    receiver_response = delivery._deliver_global_event(
        "candidate", receiver_event, delivery._metadata(receiver_event),
    )
    stored = matches.find_one({"_id": match_id})

    assert receiver_response["origin_room_id"] == "ai_room::candidate::match_hub"
    assert stored["proposal_delivery_rooms"]["receiver"] == receiver_source_room_id
    assert receiver_response["metadata"]["source_room_id"] == receiver_source_room_id
    assert receiver_response["metadata"]["source_summary"] == ""

    monkeypatch.setattr(routes, "matches_coll", matches)
    monkeypatch.setattr(routes, "get_room", room_for_viewer)
    monkeypatch.setattr(routes, "match_hub_v1_enabled", lambda: True)
    monkeypatch.setattr(routes, "match_hub_room_id", lambda user: f"ai_room::{user}::match_hub")
    monkeypatch.setattr(routes, "public_display_name", lambda _user: "對方")
    monkeypatch.setattr(routes, "proposal_display_name", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(routes, "reason_for_viewer", lambda *_args: "先看看這張提案")
    monkeypatch.setattr(routes, "reconcile_match_state", lambda _user: stored)
    monkeypatch.setattr(routes, "_single_live_namespace_proposal", lambda *_args: None)
    monkeypatch.setattr(routes, "public_match_search_status", lambda _user: {
        "status": "idle", "reason_code": "", "cancellable": False,
    })
    monkeypatch.setattr(routes, "get_match_status_snapshot", lambda _user: {
        "state": "waiting_user", "scope": "relationship_match",
        "is_terminal": False, "chat_opened": False, "counterparty": "對方",
        "reason_code": "",
    })

    status = routes.get_match_status("owner")
    state = routes.get_single_match_state("owner", str(match_id))

    assert status["hub_cards"][0]["source_room_id"] == source_room_id
    assert state["source_room_id"] == source_room_id
    assert state["source_room_title"] == "發起者原始聊天室"
    assert state["source_summary"] == "這次由發起者的原始對話產生"

    receiver_projection = routes._proposal_source_projection(stored, "candidate")
    assert receiver_projection["source_room_id"] == receiver_source_room_id
    assert receiver_projection["source_room_title"] == "受邀者自己的聊天室"
    assert receiver_projection["source_summary"] == ""


def test_push_data_carries_hub_navigation_without_triggering_navigation():
    event = notification_service._event(
        {
            "_id": "message-1",
            "room_id": "ai_room::owner::match_hub",
            "sender_id": "ai_assistant",
            "message_type": "mediator_card",
            "timestamp": 10,
            "metadata": {
                "event_type": "match_proposal",
                "destination_room_id": "ai_room::owner::match_hub",
                "focus_match_id": "64f000000000000000000094",
                "proposal_namespace": "relationship_match",
            },
        },
        recipient_id="owner",
        surface=notification_service.PUBLIC_AYUE,
        conversation_id="ai_room::owner::match_hub",
        title="阿月",
        ai_room_id="ai_room::owner::match_hub",
    )

    assert event.data["destination_room_id"] == "ai_room::owner::match_hub"
    assert event.data["focus_match_id"] == "64f000000000000000000094"
    assert event.data["proposal_namespace"] == "relationship_match"
