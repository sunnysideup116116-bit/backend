"""Inline delivery and canonical card state, isolated from all real user data."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import Mock

from bson import ObjectId
import pytest

from services import proactive_delivery_service as delivery, chat_service, ai_room_service
from services import match_action_service as actions
from services.match_card_projection import project_match_card_history
from tests.match_flow_store import Collection
from tests.match_flow_fixture import flow


@pytest.fixture
def inline(monkeypatch):
    mid = ObjectId()
    matches = Collection([{"_id": mid, "from_user": "owner", "to_user": "other", "status": "draft",
                           "proposal_namespace": "relationship_match", "proposal_revision": 0}])
    messages = Collection()
    monkeypatch.setattr(delivery, "matches_coll", matches)
    monkeypatch.setattr(delivery, "get_room", lambda room, user: {"room_id": room} if room.startswith(f"ai_room::{user}::") else None)
    monkeypatch.setattr(delivery, "most_recent_ai_room", lambda user, **_: f"ai_room::{user}::fallback")
    monkeypatch.setattr(delivery, "reason_for_viewer", lambda *_: "這是只給目前使用者看的介绍。")
    create = Mock()
    monkeypatch.setattr(delivery, "create_proposal_room", create)
    monkeypatch.setattr(chat_service, "messages_coll", messages)
    monkeypatch.setattr(chat_service, "mirror_message_to_appwrite_async", Mock())
    monkeypatch.setattr(chat_service, "queue_push_notification", Mock())
    event = {"type": "match_proposal", "event_id": "event", "event_key": "job:proposal", "match_id": str(mid),
             "message": "我找到一位可以介紹的人。", "origin_room_id": "ai_room::owner::origin"}
    return matches, messages, create, event


def test_new_proposal_is_one_inline_card_even_with_two_polling_clients(inline):
    matches, messages, create, event = inline
    def deliver(_):
        return delivery._deliver_global_event("owner", deepcopy(event), delivery._metadata(event))
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(deliver, range(2)))
    assert len(messages.rows) == 1
    assert messages.rows[0]["room_id"] == event["origin_room_id"]
    assert messages.rows[0]["message_type"] == "mediator_card"
    assert all("proposal_room_id" not in response for response in responses)
    assert all(response["metadata"]["canonical_status"] == "draft" for response in responses)
    create.assert_not_called()


def test_foreign_origin_falls_back_and_cannot_move_on_redelivery(inline):
    matches, messages, _, event = inline
    event["origin_room_id"] = "ai_room::stranger::private"
    first = delivery._deliver_global_event("owner", event, delivery._metadata(event))
    event["origin_room_id"] = "ai_room::owner::different"
    second = delivery._deliver_global_event("owner", event, delivery._metadata(event))
    assert first["origin_room_id"] == second["origin_room_id"] == "ai_room::owner::fallback"
    assert len(messages.rows) == 1


def test_receiver_gets_own_room_not_initiator_origin(inline):
    matches, messages, _, event = inline
    matches.update_one({"_id": ObjectId(event["match_id"])}, {"$set": {"status": "pending"}})
    event["type"] = "incoming_match_interest"
    response = delivery._deliver_global_event("other", event, delivery._metadata(event))
    assert response["origin_room_id"] == "ai_room::other::fallback"
    assert response["metadata"]["stage"] == "incoming_decision"
    assert response["metadata"]["proposal_role"] == "receiver"


def test_deleted_bound_room_falls_back_safely(inline):
    matches, _, _, event = inline
    matches.update_one({"_id": ObjectId(event["match_id"])}, {"$set": {
        "proposal_delivery_rooms.initiator": "deleted-room",
    }})
    result = delivery._deliver_global_event("owner", event, delivery._metadata(event))
    assert result["origin_room_id"] == event["origin_room_id"]


def test_ended_proposal_is_not_delivered(inline):
    matches, messages, create, event = inline
    matches.update_one({"_id": ObjectId(event["match_id"])}, {"$set": {"status": "declined"}})
    assert delivery._deliver_global_event("owner", event, delivery._metadata(event))["stale"]
    assert not messages.rows
    create.assert_not_called()


def test_fallback_skips_legacy_proposal_rooms(monkeypatch):
    monkeypatch.setattr(ai_room_service, "list_rooms", lambda _: [
        {"room_id": "ai_room::owner::proposal::old", "latest_message": "old"},
        {"room_id": "ai_room::owner::conversation", "latest_message": "chat"},
    ])
    assert ai_room_service.most_recent_ai_room("owner", include_proposal_rooms=False) == "ai_room::owner::conversation"


def test_saved_cards_update_only_after_explicit_hub_decision(flow):
    card = {"sender_id": "ai_assistant", "message_type": "mediator_card", "metadata": {
        "event_type": "match_proposal", "match_id": str(flow.old_id), "stage": "waiting_other",
        "matches": [{"match_id": str(flow.old_id), "proposal_revision": 2}],
    }}
    saved = [deepcopy(card), deepcopy(card)]
    initial_writes = flow.matches.writes
    preview = flow.send("撤回這張提案", "dismiss_proposal")
    assert preview.choice_prompt is None
    assert "阿月牽線" in preview.reply
    assert not flow.choices.rows and not flow.jobs.rows
    assert flow.matches.writes == initial_writes
    before = project_match_card_history(saved, "owner", collection=flow.matches)
    assert all(row["metadata"]["canonical_status"] == "pending" for row in before)
    # The chat redirect carries no write authority. The explicit Hub card
    # action uses the canonical compare-and-set service shared with its API.
    done = actions.decide_match(
        user_id="owner", match_id=str(flow.old_id), action="cancel",
        expected_status="pending", expected_revision=2,
        expected_namespace="relationship_match",
    )
    assert done["new_status"] == "declined"
    assert not done["idempotent"]
    writes = flow.matches.writes
    after = project_match_card_history(saved, "owner", collection=flow.matches)
    assert all(row["metadata"]["canonical_status"] == "declined" for row in after)
    assert all(row["metadata"]["decision_action"] == "cancel" for row in after)
    assert all(row["metadata"]["proposal_revision"] == 3 for row in after)
    assert all(row["metadata"]["actions"] == [] for row in after)
    assert flow.matches.writes == writes
    assert "canonical_status" not in saved[0]["metadata"]


def test_withdrawal_redirect_followup_keeps_original_card_actionable(flow):
    card = {"metadata": {"event_type": "match_proposal", "match_id": str(flow.old_id), "actions": ["cancel"]}}
    initial = deepcopy(flow.matches.rows)
    initial_writes = flow.matches.writes
    preview = flow.send("撤回", "dismiss_proposal")
    assert preview.choice_prompt is None
    assert "阿月牽線" in preview.reply
    followup = flow.send("那先不要撤回", "dismiss_proposal")
    assert followup.choice_prompt is None
    assert not followup.match_state_changed
    assert not flow.choices.rows and not flow.jobs.rows
    projected = project_match_card_history([card], "owner", collection=flow.matches)[0]["metadata"]
    assert projected["canonical_status"] == "pending"
    assert projected["actions"] == ["cancel"]
    assert flow.matches.rows == initial
    assert flow.matches.writes == initial_writes


def test_foreign_or_missing_card_cannot_expose_an_actionable_state(flow):
    card = {"metadata": {"event_type": "match_proposal", "match_id": str(flow.old_id)}}
    projected = project_match_card_history([card], "stranger", collection=flow.matches)
    assert projected[0]["metadata"]["canonical_status"] == "unavailable"
    assert projected[0]["metadata"]["actions"] == []
