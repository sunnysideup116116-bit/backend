from types import SimpleNamespace
from unittest.mock import MagicMock

import mongomock
import pytest

from services import pair_opening_service as opening, match_action_service as actions, chat_service


def proposal():
    return {
        "_id": "match", "status": "accepted", "from_user": "owner-a", "to_user": "owner-b",
        "search_context": {"invitation_topic": "運動"},
        "private_memory": "秘密不要送出",
        "friend_intro_v4": {
            "initiator_preview": {"viewer_id": "owner-a", "counterparty_id": "owner-b",
                "counterparty_public_personality": "喜歡慢慢熟悉", "accepted_opening": "私人提醒"},
        },
    }


def model_response():
    return SimpleNamespace(tool_calls=[{"name": "write_pair_icebreaker", "arguments": {
        "opening": "運動剛好能讓你們邊動邊聊，不急著一次聊完所有話題。",
        "question": "第一次一起動一動，你們比較想散步暖身，還是試點會流汗的活動？",
        "evidence_key": "invitation_topic", "evidence_quote": "運動",
    }}])


def test_model_writes_custom_question_but_never_receives_names_or_private_data(monkeypatch):
    model = MagicMock(return_value=model_response())
    monkeypatch.setattr(opening, "generate_chat_completion_with_tools", model)
    text, outcome = opening.compose_pair_opening(proposal(), "小安", "小林")
    assert outcome == "generated"
    assert text.startswith("阿月：小安、小林，")
    assert "散步暖身" in text
    assert "這件事最吸引" not in text
    prompt = model.call_args.args[0]
    for forbidden in ("小安", "小林", "owner-a", "owner-b", "秘密不要送出", "私人提醒"):
        assert forbidden not in prompt
    assert "喜歡慢慢熟悉" in prompt
    assert "deadline_monotonic" in model.call_args.kwargs


@pytest.mark.parametrize("labels", [("", ""), ("對方", "對方"), ("小安", "")])
def test_missing_names_never_produce_two_counterparties(monkeypatch, labels):
    monkeypatch.setattr(opening, "generate_chat_completion_with_tools", MagicMock(return_value=model_response()))
    text, _ = opening.compose_pair_opening(proposal(), *labels)
    assert text.startswith("阿月：運動")
    assert "對方" not in text


def test_provider_failure_keeps_safe_opener_without_fake_names(monkeypatch):
    monkeypatch.setattr(opening, "generate_chat_completion_with_tools", MagicMock(side_effect=TimeoutError("secret")))
    text, outcome = opening.compose_pair_opening(proposal(), "", "")
    assert outcome == "provider_fallback"
    assert "對方、對方" not in text
    assert "secret" not in text


def test_unsupported_evidence_or_multiple_questions_rejected(monkeypatch):
    for patch in ({"evidence_quote": "未提供的事情"}, {"question": "喜歡跑步？打球？"}):
        result = model_response()
        result.tool_calls[0]["arguments"].update(patch)
        monkeypatch.setattr(opening, "generate_chat_completion_with_tools", MagicMock(return_value=result))
        assert opening.compose_pair_opening(proposal(), "小安", "小林")[1] == "provider_fallback"


def test_appwrite_names_override_empty_mongo_and_delivery_is_deduplicated(monkeypatch):
    db = mongomock.MongoClient().test
    monkeypatch.setattr(actions, "profiles_coll", db.profiles)
    monkeypatch.setattr(chat_service, "messages_coll", db.messages)
    monkeypatch.setattr(chat_service, "mirror_message_to_appwrite_async", MagicMock())
    monkeypatch.setattr(chat_service, "queue_push_notification", MagicMock())
    monkeypatch.setattr(actions, "queue_mediator_event", MagicMock())
    monkeypatch.setattr(actions, "schedule_match_celebration_gifs", MagicMock())
    from services import public_nickname_service
    lookup = MagicMock(side_effect=lambda user_id: {"owner-a": "Sunny", "owner-b": "kkk"}[user_id])
    monkeypatch.setattr(public_nickname_service, "_read_appwrite_nickname", lookup)
    model = MagicMock(return_value=model_response())
    monkeypatch.setattr(opening, "generate_chat_completion_with_tools", model)
    jobs = []
    actions.apply_transition_effects(proposal(), "accept", "pending", [], schedule_task=lambda task: jobs.append(task))
    model.assert_not_called()
    assert len(jobs) == 1
    jobs[0]()
    jobs[0]()
    message = db.messages.find_one()
    assert "Sunny、kkk" in message["content"]
    assert message["sender_id"] == "ai_assistant"
    assert message["metadata"]["opening_outcome"] == "generated"
    assert db.messages.count_documents({}) == 1
    assert lookup.call_count == 2
    model.assert_called_once()
