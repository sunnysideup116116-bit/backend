"""Cross-service block enforcement contracts for chat, contacts, and matching."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from models import DirectChatRequest
from services.risk_block_service import (
    RiskBlockService,
    RiskBlockServiceUnavailable,
)


def test_block_service_keeps_outgoing_private_from_bidirectional_exclusions():
    service = RiskBlockService(transport=lambda user_id: {
        "user_id": user_id,
        "blocked_user_ids": ["bob"],
        "excluded_user_ids": ["bob", "carol"],
    })
    result = service.get_sets("alice")
    assert result.blocked_user_ids == frozenset({"bob"})
    assert result.excluded_user_ids == frozenset({"bob", "carol"})
    assert service.is_pair_blocked("alice", "carol") is True


def test_block_service_rejects_malformed_or_unavailable_responses():
    with pytest.raises(RiskBlockServiceUnavailable):
        RiskBlockService(transport=lambda _: {"excluded_user_ids": []}).get_sets("alice")
    with pytest.raises(RiskBlockServiceUnavailable):
        RiskBlockService(transport=lambda _: (_ for _ in ()).throw(OSError())).get_sets("alice")


@pytest.mark.parametrize("file_id", [None, "image-file"])
def test_relationship_block_prevents_text_and_image_before_any_side_effect(file_id):
    from routers import public_chat

    request = DirectChatRequest(
        user_id="alice",
        contact_id="bob",
        message="hello" if file_id is None else "",
        client_message_id="attempt",
        file_id=file_id,
    )
    with patch.object(public_chat, "find_accepted_match", return_value={"_id": "match"}), \
         patch.object(public_chat.risk_block_service, "is_pair_blocked", return_value=True), \
         patch.object(public_chat, "save_pair_owner_message_once") as save, \
         patch.object(public_chat.pair_message_risk_gate, "evaluate") as message_risk, \
         patch.object(public_chat.profiles_coll, "update_one") as profile_write:
        with pytest.raises(HTTPException) as raised:
            public_chat.direct_chat(request, MagicMock())

    assert raised.value.status_code == 403
    save.assert_not_called()
    message_risk.assert_not_called()
    profile_write.assert_not_called()


def test_block_lookup_failure_is_fail_closed_before_persistence():
    from routers import public_chat

    request = DirectChatRequest(
        user_id="alice", contact_id="bob", message="hello", client_message_id="attempt",
    )
    with patch.object(public_chat, "find_accepted_match", return_value={"_id": "match"}), \
         patch.object(
             public_chat.risk_block_service,
             "is_pair_blocked",
             side_effect=RiskBlockServiceUnavailable("offline"),
         ), \
         patch.object(public_chat, "save_pair_owner_message_once") as save:
        with pytest.raises(HTTPException) as raised:
            public_chat.direct_chat(request, MagicMock())
    assert raised.value.status_code == 503
    save.assert_not_called()


def test_gateway_block_list_exposes_only_outgoing_ids():
    from routers import risk_actions

    with patch.object(risk_actions, "_get", return_value={
        "blocked_user_ids": ["bob"],
        "excluded_user_ids": ["bob", "carol"],
    }):
        response = risk_actions.list_blocked_users("alice")
    assert response == {"user_id": "alice", "blocked_user_ids": ["bob"], "count": 1}
    assert "excluded_user_ids" not in response


def test_contacts_hide_blocked_relationships_from_both_directions():
    from routers import chat_messages

    accepted = [
        {"from_user": "alice", "to_user": "bob", "status": "accepted"},
        {"from_user": "alice", "to_user": "carol", "status": "accepted"},
    ]
    message_cursor = MagicMock()
    message_cursor.sort.return_value = []

    def profile(query, *_):
        user_id = query.get("user_id")
        return {"user_id": user_id, "current_context": "context"}

    with patch.object(
        chat_messages.risk_block_service,
        "excluded_user_ids",
        return_value={"bob"},
    ), patch.object(
        chat_messages.matches_coll,
        "find",
        return_value=accepted,
    ), patch.object(
        chat_messages.messages_coll,
        "find",
        return_value=message_cursor,
    ), patch.object(
        chat_messages.profiles_coll,
        "find_one",
        side_effect=profile,
    ), patch.object(
        chat_messages,
        "mentioned_contact_refs",
        side_effect=lambda _, ids: [{"display_name": ids[0]}],
    ):
        response = chat_messages.get_contacts("alice")

    assert [contact["id"] for contact in response["contacts"]] == [
        "ai_assistant",
        "carol",
    ]


def test_contacts_fail_closed_when_block_state_is_unavailable():
    from routers import chat_messages

    with patch.object(
        chat_messages.risk_block_service,
        "excluded_user_ids",
        side_effect=RiskBlockServiceUnavailable("offline"),
    ), patch.object(chat_messages.matches_coll, "find") as find_matches:
        with pytest.raises(HTTPException) as raised:
            chat_messages.get_contacts("alice")

    assert raised.value.status_code == 503
    find_matches.assert_not_called()


def test_directive_projection_preserves_bounded_target_and_valid_actions():
    from services.risk_policy_service import PairMessageRiskDecision

    projected = PairMessageRiskDecision._sanitize_directive({
        "action": "show_safety_info_card",
        "target_user_id": "bob",
        "action_options": [
            {"action": "dismiss", "label": "繼續對話"},
            {"action": "unknown", "label": "不應出現"},
            {"action": "block_user", "label": "封鎖"},
        ],
    })
    assert projected["target_user_id"] == "bob"
    assert projected["action_options"] == [
        {"action": "dismiss", "label": "繼續對話"},
        {"action": "block_user", "label": "封鎖"},
    ]


def test_persisted_risk_projection_keeps_target_and_action_order():
    from routers import public_chat

    receiver_directive = {
        "action": "show_safety_info_card",
        "target_user_id": "bob",
        "action_options": [
            {"action": "report_user", "label": "先檢舉"},
            {"action": "block_user", "label": "再封鎖"},
        ],
    }
    projection = {
        "level": "restricted",
        "ui_priority": "risk",
        "delivery": "delivered",
        "triggered_by_msg_id": "risk-1",
        "receiver_directive": receiver_directive,
    }
    decision = MagicMock(
        may_persist=True,
        public_projection=lambda: projection,
    )
    request = DirectChatRequest(
        user_id="alice",
        contact_id="bob",
        message="hello",
        client_message_id="attempt",
    )
    with patch.object(public_chat, "find_accepted_match", return_value={"_id": "match"}), \
         patch.object(public_chat.risk_block_service, "is_pair_blocked", return_value=False), \
         patch.object(public_chat.pair_message_risk_gate, "evaluate", return_value=decision), \
         patch.object(
             public_chat,
             "save_pair_owner_message_once",
             return_value={"created": False},
         ) as save:
        result = public_chat.direct_chat(request, MagicMock())

    persisted = save.call_args.kwargs["risk_projection"]
    assert persisted["receiver_directive"]["target_user_id"] == "bob"
    assert persisted["receiver_directive"]["action_options"] == [
        {"action": "report_user", "label": "先檢舉"},
        {"action": "block_user", "label": "再封鎖"},
    ]
    assert result["duplicate"] is True


def test_standard_and_event_matching_wire_block_exclusions_before_selection():
    root = Path(__file__).resolve().parents[1]
    standard = (root / "routers" / "match.py").read_text(encoding="utf-8")
    function = standard[standard.index("def generate_matches_for_user"):]
    assert function.index("risk_block_service.excluded_user_ids") < function.index('"$vectorSearch"')

    event = (root / "services" / "event_opportunity_service.py").read_text(encoding="utf-8")
    function = event[event.index("def create_event_opportunity"):]
    assert function.index("risk_block_service.excluded_user_ids") < function.index("requests.post")
    assert "candidate_id in requested_exclusions" in function


def test_standard_matching_passes_block_set_into_vector_exclusion():
    from routers import match

    captured = {}

    def aggregate(pipeline):
        captured["pipeline"] = pipeline
        return []

    user = {
        "user_id": "alice",
        "current_context": "make friends",
        "context_embedding": [0.1, 0.2],
        "current_context_revision": 1,
    }
    with patch.object(match.profiles_coll, "find_one", return_value=user), \
         patch.object(match.matches_coll, "find", return_value=[]), \
         patch.object(match.profiles_coll, "aggregate", side_effect=aggregate), \
         patch.object(
             match.risk_block_service,
             "excluded_user_ids",
             return_value={"bob"},
         ):
        result = match.generate_matches_for_user(
            "alice",
            report_progress=lambda _: True,
        )

    excluded = captured["pipeline"][1]["$match"]["user_id"]["$nin"]
    assert set(excluded) == {"alice", "bob"}
    assert result["status"] == "no_suitable_candidate"


def test_standard_matching_fails_closed_before_vector_search():
    from routers import match

    user = {
        "user_id": "alice",
        "current_context": "make friends",
        "context_embedding": [0.1, 0.2],
        "current_context_revision": 1,
    }
    with patch.object(match.profiles_coll, "find_one", return_value=user), \
         patch.object(match.matches_coll, "find", return_value=[]), \
         patch.object(match.profiles_coll, "aggregate") as aggregate, \
         patch.object(
             match.risk_block_service,
             "excluded_user_ids",
             side_effect=RiskBlockServiceUnavailable("offline"),
         ):
        with pytest.raises(HTTPException) as raised:
            match.generate_matches_for_user(
                "alice",
                report_progress=lambda _: True,
            )

    assert raised.value.status_code == 503
    aggregate.assert_not_called()


def test_event_matching_forwards_and_postvalidates_block_exclusions():
    from services import event_opportunity_service as event_service

    matches = MagicMock()
    matches.count_documents.return_value = 0
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "status": "success",
        "first_user_id": "alice",
        "second_user_id": "bob",
        "match": {
            "user_id": "alice",
            "candidate_id": "bob",
            "event_id": "event-1",
        },
    }
    with patch.object(event_service, "matches_coll", matches), \
         patch.object(
             event_service.risk_block_service,
             "excluded_user_ids",
             return_value={"bob"},
         ), patch.object(
             event_service.requests,
             "post",
             return_value=response,
         ) as post:
        result = event_service.create_event_opportunity("alice")

    assert post.call_args.kwargs["json"]["excluded_user_ids"] == ["bob"]
    assert result == {"status": "excluded_candidate"}
    matches.insert_one.assert_not_called()


def test_event_matching_fails_closed_before_agent_request():
    from services import event_opportunity_service as event_service

    matches = MagicMock()
    matches.count_documents.return_value = 0
    with patch.object(event_service, "matches_coll", matches), \
         patch.object(
             event_service.risk_block_service,
             "excluded_user_ids",
             side_effect=RiskBlockServiceUnavailable("offline"),
         ), patch.object(event_service.requests, "post") as post:
        result = event_service.create_event_opportunity("alice")

    assert result == {"status": "risk_block_unavailable"}
    post.assert_not_called()
