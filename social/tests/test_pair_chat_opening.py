"""Pair messages never impersonate people; acceptance welcomes are shared once."""

from unittest.mock import MagicMock

import mongomock
import pytest
from fastapi import BackgroundTasks

from models import DirectChatRequest
from routers import public_chat
from services import chat_service, match_action_service as actions
from services.match_reason_service import shared_match_opening


@pytest.mark.parametrize("sender,receiver", [("first", "second"), ("second", "first")])
def test_first_message_from_either_person_has_no_generated_recipient_reply(monkeypatch, sender, receiver):
    risk = MagicMock(may_persist=True)
    risk.public_projection.return_value = {"level": "safe", "delivery": "delivered", "ui_priority": "coach"}
    monkeypatch.setattr(public_chat, "_validated_requested_mentions", lambda _: ([], False))
    monkeypatch.setattr(public_chat, "find_accepted_match", lambda *_: {"_id": "match"})
    monkeypatch.setattr(public_chat.pair_message_risk_gate, "evaluate", MagicMock(return_value=risk))
    monkeypatch.setattr(public_chat.risk_block_service, "is_pair_blocked", lambda *_: False)
    persist = MagicMock(return_value={"message_id": "owner-message", "created": True})
    monkeypatch.setattr(public_chat, "save_pair_owner_message_once", persist)
    monkeypatch.setattr(public_chat, "profiles_coll", mongomock.MongoClient().test.profiles)
    monkeypatch.setattr(public_chat, "mark_post_chat_activity", lambda *_: 1)
    model = MagicMock(side_effect=AssertionError("pair chat must not call a reply model"))
    save = MagicMock(side_effect=AssertionError("must not write on behalf of recipient"))
    monkeypatch.setattr(public_chat, "generate_chat_completion", model)
    monkeypatch.setattr(public_chat, "save_message", save)
    result = public_chat.direct_chat(DirectChatRequest(user_id=sender, contact_id=receiver, message="嗨"), BackgroundTasks())
    assert result["reply"] == ""
    assert result["opening_assist"] is False
    assert persist.call_args.args[1] == sender
    model.assert_not_called()
    save.assert_not_called()


def test_acceptance_replay_keeps_one_shared_ayue_message(monkeypatch):
    database = mongomock.MongoClient().test
    monkeypatch.setattr(chat_service, "messages_coll", database.messages)
    mirror, push = MagicMock(), MagicMock()
    monkeypatch.setattr(chat_service, "mirror_message_to_appwrite_async", mirror)
    monkeypatch.setattr(chat_service, "queue_push_notification", push)
    monkeypatch.setattr(actions, "profiles_coll", database.profiles)
    monkeypatch.setattr(actions, "queue_mediator_event", MagicMock())
    monkeypatch.setattr(actions, "schedule_match_celebration_gifs", MagicMock())
    database.profiles.insert_many([
        {"user_id": "first", "display_name": "小安"}, {"user_id": "second", "display_name": "小林"},
    ])
    match = {"_id": "match", "from_user": "first", "to_user": "second", "status": "accepted",
        "friend_intro_v4": {"initiator_preview": {"viewer_id": "first", "counterparty_id": "second",
            "counterparty_context_snapshot": "想逛書店", "accepted_opening": "私人建議不可公開"}}}
    for _ in range(2):
        actions.apply_transition_effects(match, "accept", "pending", [])
    assert database.messages.count_documents({}) == 1
    message = database.messages.find_one()
    assert message["sender_id"] == "ai_assistant"
    assert message["message_type"] == "system"
    assert message["room_id"] == chat_service.generate_room_id("first", "second")
    assert message["metadata"]["event_type"] == "match_pair_opening"
    assert "小安" in message["content"] and "小林" in message["content"]
    assert "想逛書店" in message["content"]
    assert "私人建議不可公開" not in message["content"]
    mirror.assert_called_once()
    push.assert_called_once()


def test_shared_opening_ignores_misbound_evidence_and_private_reason():
    match = {"from_user": "first", "to_user": "second", "reason": "私人理由",
        "friend_intro_v4": {"initiator_preview": {"viewer_id": "stranger", "counterparty_id": "second",
            "counterparty_context_snapshot": "別人的資料"}}}
    text = shared_match_opening(match, "小安", "小林")
    assert "別人的資料" not in text
    assert "私人理由" not in text
    assert "探索是否聊得來" in text
