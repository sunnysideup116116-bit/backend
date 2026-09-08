"""Regression for semantic search scope and preview-before-invitation."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from services.ayue_agent.contracts import AgentTurnContext
from services.ayue_agent.v3.contracts import SubTask
from services.ayue_agent.v3.confirmation import match_choice_labels
from services.ayue_agent.v3.write_executors import prepare_write_confirmation
from services.match_search_context import extract_invitation_topic
from services.match_reason_service import topic_friend_intro_fallback, reason_for_viewer


def preview(message, request):
    ctx = AgentTurnContext(user_id="owner", room_id="room", message=message)
    with patch("services.ayue_agent.v3.write_executors.assess_match_opportunity", return_value=SimpleNamespace(state="ready")):
        return prepare_write_confirmation("match.start_search", {"search_request": request}, ctx, SimpleNamespace(active_proposal=None))


@pytest.mark.parametrize("message", ["幫我找一位適合認識的人。", "找一位合得來的人", "介紹一位可以認識的人"])
def test_generic_person_is_not_activity(message):
    assert extract_invitation_topic(message) == ""
    payload, text = preview(message, {"kind": "general"})
    assert payload["data"] == {}
    assert "沒有指定活動" in text


def test_activity_search_is_preview_only():
    payload, text = preview("找人一起看展", {"kind": "activity", "topic": "看展"})
    assert payload["data"]["search_context"]["invitation_topic"] == "看展"
    assert "delivery_mode" not in payload["data"]
    assert "你看完再決定" in text
    assert match_choice_labels({"tool_name": "match.start_search", "payload": payload["data"]})["confirm_label"] == "開始搜尋"


def test_only_grounded_explicit_invitation_gets_send_confirmation():
    payload, text = preview("找人一起看展，找到就幫我邀請", {"kind": "activity", "topic": "看展", "invitation_evidence": "找到就幫我邀請"})
    assert payload["data"]["delivery_mode"] == "invite_on_match"
    assert "開始找並送出邀請" in text
    assert match_choice_labels({"tool_name": "match.start_search", "payload": payload["data"]})["confirm_label"] == "開始找並送出邀請"
    payload, _ = preview("找人一起看展", {"kind": "activity", "topic": "看展", "invitation_evidence": "找到就幫我邀請"})
    assert "delivery_mode" not in payload["data"]


def test_missing_activity_evidence_asks_without_write():
    payload, _ = preview("找個伴", {"kind": "activity", "topic": "看展"})
    assert payload is None


def test_planner_preserves_semantic_request():
    task = SubTask(id="m", agent="match", task_brief="找活動伴", match_intent="start_search", match_search_request={"kind": "activity", "topic": "看展"})
    assert task.match_search_request.topic == "看展"


@pytest.mark.parametrize("auto", [False, True])
def test_topic_reason_has_public_basis_and_role_binding(auto):
    owner = {"user_id": "owner", "big_five": {"E": 3}}
    other = {"user_id": "other", "big_five": {"E": 8}, "current_context": "PRIVATE_CONTEXT"}
    entry = topic_friend_intro_fallback(owner, other, "看展", requester_id="owner", auto_invite=auto)
    assert "比較外向" in entry["viewer_text"]
    assert "PRIVATE_CONTEXT" not in entry["viewer_text"]
    doc = {"from_user": "owner", "to_user": "other", "status": "draft", "reason_version": "v4_friend_intro", "delivery_mode": "invite_on_match" if auto else "preview_on_match", "friend_intro_v4": {"initiator_preview": {**entry, "viewer_id": "owner", "counterparty_id": "other"}}}
    assert "推薦說明" in reason_for_viewer(doc, "owner")
    assert reason_for_viewer(doc, "stranger") == ""
    receiver = topic_friend_intro_fallback(other, owner, "看展", requester_id="owner")
    assert "偏安靜" in receiver["viewer_text"]
    assert "邀請你先認識" in receiver["viewer_text"]


def test_sparse_profile_does_not_invent_common_interest():
    entry = topic_friend_intro_fallback({"user_id": "owner"}, {"user_id": "other"}, "看展", requester_id="owner")
    assert "沒有足夠共同活動依據" in entry["viewer_text"]
    assert "也喜歡" not in entry["viewer_text"]


def test_history_reload_preserves_invite_label_without_exposing_payload():
    import mongomock
    from services.ayue_agent.v3.confirmation import project_match_choice_history
    collection = mongomock.MongoClient().db.confirmations
    collection.insert_one({"_id": "choice", "user_id": "owner", "room_id": "room", "surface": "public", "interaction_mode": "bubble_buttons", "tool_name": "match.start_search", "payload": {"delivery_mode": "invite_on_match", "secret": "private"}, "status": "completed", "selected_choice": "confirm"})
    # Use the production constants, keeping this test independent of enum spelling.
    from services.ayue_agent.v3.confirmation import SURFACE_PUBLIC, INTERACTION_BUBBLE
    collection.update_one({"_id": "choice"}, {"$set": {"surface": SURFACE_PUBLIC, "interaction_mode": INTERACTION_BUBBLE}})
    messages = [{"metadata": {"choice_prompt": {"id": "choice"}}}]
    result = project_match_choice_history(messages, user_id="owner", room_id="room", collection=collection)
    choice = result[0]["metadata"]["choice_prompt"]
    assert choice["confirm_label"] == "開始找並送出邀請"
    assert "payload" not in choice
    assert "private" not in str(choice)


@pytest.mark.parametrize("auto", [False, True])
def test_long_topic_keeps_consent_and_reason_within_card_budget(auto):
    entry = topic_friend_intro_fallback({"user_id": "owner"}, {"user_id": "other", "big_five": {"E": 8}}, "展" * 80, requester_id="owner", auto_invite=auto)
    assert len(entry["viewer_text"]) <= 220
    assert "推薦說明" in entry["viewer_text"]
    assert "邀請已送出" in entry["viewer_text"] if auto else entry["viewer_text"].endswith("？")


@pytest.mark.parametrize("search_request,auto", [({"kind": "general", "topic": "適合認識的人"}, False), ({"kind": "activity", "topic": "看展"}, False), ({"kind": "activity", "topic": "看展", "invitation_evidence": "找到就幫我邀請"}, True)])
def test_runtime_carries_semantic_request_into_confirmation(search_request, auto, monkeypatch):
    from unittest.mock import Mock
    from services.ayue_agent.contracts import PublicAgentTurnContext
    from services.ayue_agent.v3 import match_runtime, write_executors
    message = "幫我找一位適合認識的人" if search_request["kind"] == "general" else "找人一起看展，找到就幫我邀請"
    turn = PublicAgentTurnContext(user_id="owner", room_id="room", message=message)
    turn._raw_ctx = AgentTurnContext(user_id="owner", room_id="room", message=message)
    monkeypatch.setattr(match_runtime, "load_match_state", lambda _: {"ambiguous": False, "active_proposal": None, "search": {"status": "idle"}})
    monkeypatch.setattr(write_executors, "assess_match_opportunity", lambda *a, **kw: SimpleNamespace(state="ready"))
    services = SimpleNamespace(turn_ctx=turn, trace={"guard_results": []}, create_confirmation=Mock(), run_id="run")
    task = SubTask(id="m", agent="match", task_brief=message, match_intent="start_search", match_search_request=search_request)
    result, _ = match_runtime.run(None, task=task, services=services)
    services.create_confirmation.assert_called_once()
    data = services.create_confirmation.call_args.kwargs["payload"]
    assert (data.get("delivery_mode") == "invite_on_match") == auto
    assert bool(data.get("search_context")) == (search_request["kind"] == "activity")
