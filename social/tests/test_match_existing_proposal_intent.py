"""A new search preserves live cards and only blocks an undecided draft."""
import pytest
from types import SimpleNamespace

from tests.test_match_restart_flow import flow
from services.ayue_agent.v3 import match_runtime


@pytest.mark.parametrize("message", ["我想要配對", "我要配對", "i want match"])
def test_pending_search_request_creates_one_confirmation_without_changing_card(flow, message):
    flow.matches.update_one({"_id": flow.old_id}, {"$set": {"status": "pending"}})
    result = flow.send(message, intent="start_search")
    assert result.choice_prompt
    assert len(flow.choices.rows) == 1
    assert not flow.jobs.rows
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == "pending"
    started = flow.send(choice=result.choice_prompt["id"], action="confirm")
    assert started.match_state_changed
    assert len(flow.jobs.rows) == 1
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == "pending"


def test_draft_search_request_stays_on_the_card_until_user_decides(flow):
    flow.matches.update_one({"_id": flow.old_id}, {"$set": {"status": "draft"}})
    before = flow.matches.writes
    result = flow.send("我想配對", intent="start_search")
    assert result.choice_prompt is None
    assert not flow.choices.rows
    assert not flow.jobs.rows
    assert flow.matches.writes == before
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == "draft"
    assert "阿月牽線" in result.reply
    assert "確認放棄" not in result.reply


def test_explicit_restart_without_active_proposal_only_confirms_search(flow):
    flow.matches.update_one({"_id": flow.old_id}, {"$set": {"status": "declined"}})
    result = flow.send("重新配對", intent="restart_search")
    assert result.choice_prompt
    record = flow.choices.find_one({"_id": result.choice_prompt["id"]})
    assert record["tool_name"] == "match.start_search"
    assert not record["payload"].get("continuation")
    assert not flow.jobs.rows


def test_provider_start_intent_keeps_pending_card_and_can_start_another_search(flow):
    result = flow.send("重新找", intent="start_search")
    assert result.choice_prompt
    started = flow.send(choice=result.choice_prompt["id"], action="confirm")
    assert started.match_state_changed
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == "pending"
    assert len(flow.jobs.rows) == 1


def test_legacy_restart_continuation_is_inert(flow):
    parent = {
        "_id": "old-confirmation",
        "status": "completed",
        "tool_name": "match.decide_active_proposal",
        "arguments": {"decision": "cancelled"},
        "payload": {"continuation": "offer_start_search"},
        "user_id": "owner",
        "room_id": "room",
        "surface": "public_ayue",
        "resolved_at": 9_999_999_999,
    }
    result = match_runtime.offer_restart_continuation(
        flow.choices,
        SimpleNamespace(user_id="owner", room_id="room"),
        SimpleNamespace(),
        parent,
        "run",
    )
    assert result is None
    assert not flow.jobs.rows
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == "pending"
