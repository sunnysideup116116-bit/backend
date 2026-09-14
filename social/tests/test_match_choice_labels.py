import pytest

from services.ayue_agent.shared.confirmation import match_choice_cancel_reply, public_choice_projection
from tests.match_flow_fixture import flow


@pytest.mark.parametrize("tool,args,cancel,confirm", [
    ("match.decide_active_proposal", {"decision": "declined"}, "保留提案", "確認放棄"),
    ("match.decide_active_proposal", {"decision": "cancelled"}, "繼續等待", "確認撤回"),
    ("match.decide_active_proposal", {"decision": "interested"}, "先不接受", "確認接受"),
    ("match.start_search", {}, "暫不搜尋", "開始搜尋"),
    ("match.cancel_search", {}, "繼續搜尋", "停止搜尋"),
])
def test_match_labels_are_derived_from_private_action_not_model_copy(tool, args, cancel, confirm):
    record = {"_id": "choice", "status": "pending", "tool_name": tool, "arguments": args,
              "payload": {"match_id": "private-id", "proposal_revision": 7}, "confirm_label": "untrusted"}
    public = public_choice_projection(record)
    assert public["cancel_label"] == cancel
    assert public["confirm_label"] == confirm
    assert set(public) == {"id", "state", "selected", "expires_at", "cancel_label", "confirm_label"}
    assert "private-id" not in str(public)


def test_unrelated_choice_keeps_legacy_shape():
    assert set(public_choice_projection({"_id": "c", "tool_name": "calendar.submit_commands"})) == {"id", "state", "selected", "expires_at"}


def test_topic_search_labels_explain_that_confirmation_sends_the_invitation():
    record = {
        "_id": "choice",
        "status": "pending",
        "tool_name": "match.start_search",
        "arguments": {},
        "payload": {
            "search_context": {"invitation_topic": "滑雪"},
            "delivery_mode": "invite_on_match",
        },
    }
    public = public_choice_projection(record)
    assert public["cancel_label"] == "先不找"
    assert public["confirm_label"] == "開始找並送出邀請"
    assert match_choice_cancel_reply(record) == "好，這次先不找，也沒有送出邀請。"


def test_chat_decision_redirects_to_the_hub_without_changing_the_proposal(flow):
    flow.matches.update_one({"_id": flow.old_id}, {"$set": {"status": "draft"}})
    result = flow.send("不要這張", intent="dismiss_proposal")
    assert result.choice_prompt is None
    assert "阿月牽線" in result.reply
    assert "沒有替你改變邀請狀態" in result.reply
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == "draft"
    assert not flow.jobs.rows
