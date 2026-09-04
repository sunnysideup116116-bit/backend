import pytest

from services.ayue_agent.v3.confirmation import public_choice_projection
from tests.test_match_restart_flow import flow


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


def test_keep_proposal_cancels_only_the_operation_and_explains_it(flow):
    flow.matches.update_one({"_id": flow.old_id}, {"$set": {"status": "draft"}})
    preview = flow.send("我想配對")
    assert preview.choice_prompt["cancel_label"] == "保留提案"
    assert preview.choice_prompt["confirm_label"] == "確認放棄"
    result = flow.send(choice=preview.choice_prompt["id"], action="cancel")
    assert "沒有更動提案" in result.reply
    assert "沒有開始新搜尋" in result.reply
    assert result.choice_resolution["cancel_label"] == "保留提案"
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == "draft"
    assert not flow.jobs.rows
