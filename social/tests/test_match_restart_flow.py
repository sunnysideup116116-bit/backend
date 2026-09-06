from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor

from bson import ObjectId
import pytest

from services import match_state_service as state, match_search_job_service as jobs
from services import match_action_service as actions, match_decision_service as decisions
from services.ayue_agent import match_opportunity
from services.ayue_agent.contracts import AgentTurnContext, PublicAgentTurnContext
from services.ayue_agent.v3 import scheduler, planner, match_runtime, write_executors
from services.ayue_agent.v3.confirmation import ConfirmationManager
from services.ai_service import ToolCallResult
from tests.match_flow_store import Collection


@pytest.fixture
def flow(monkeypatch):
    old_id, expired_id = ObjectId(), ObjectId()
    matches = Collection([
        {"_id": old_id, "from_user": "owner", "to_user": "first", "status": "pending",
         "proposal_revision": 2, "created_at": 100, "proposal_namespace": "relationship_match"},
        {"_id": expired_id, "from_user": "owner", "to_user": "historical", "status": "expired",
         "expired_reason": "draft_timeout", "proposal_revision": 1, "created_at": 200,
         "proposal_namespace": "relationship_match"},
    ])
    profiles = Collection([{"user_id": "owner", "current_context": "週末想看展覽", "big_five": {"summary": "喜歡探索"}, "current_context_revision": 1}])
    searches, confirmations, messages, calls = Collection(), Collection(), Collection(), Collection()
    for module in (state, actions, decisions, match_opportunity):
        if hasattr(module, "matches_coll"):
            monkeypatch.setattr(module, "matches_coll", matches)
    for module in (state, actions, jobs, match_opportunity):
        if hasattr(module, "profiles_coll"):
            monkeypatch.setattr(module, "profiles_coll", profiles)
    monkeypatch.setattr(jobs, "MATCH_SEARCH_JOBS", searches)
    monkeypatch.setattr(scheduler, "_CONFIRMATIONS", confirmations)
    monkeypatch.setattr(scheduler, "RUNS", Collection())
    monkeypatch.setattr(scheduler, "messages_coll", messages)
    monkeypatch.setattr(write_executors, "TOOL_CALLS", calls)
    monkeypatch.setattr(actions, "apply_transition_effects", lambda *_a, **_kw: None)

    def context(ctx, **_kwargs):
        snapshot = state.load_match_state(ctx.user_id)
        active = snapshot["active_proposal"]
        turn = PublicAgentTurnContext(user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message,
                                      match_search=snapshot["search"])
        if active:
            turn.active_proposal = {"status": active["status"], "stage": snapshot["stage"],
                                    "allowed_actions": snapshot["allowed_actions"], "counterparty": "目前對象",
                                    "proposal_revision": active["proposal_revision"], "created_at": active["created_at"]}
            turn._active_proposal_authority = {"match_id": str(active["_id"]), "expected_status": active["status"], "proposal_namespace": "relationship_match"}
        return turn
    monkeypatch.setattr(scheduler, "build_public_agent_turn_context", context)
    provider = Mock()
    monkeypatch.setattr(planner, "generate_chat_completion_with_tools", provider)

    def send(message="", intent="start_search", choice=None, action=None, room="room", persist=True):
        provider.return_value = ToolCallResult(content="", tool_calls=[{
            "name": "decompose_tasks", "arguments": {"write_intent": "none", "tasks": [
                {"id": "m", "agent": "match", "match_intent": intent, "task_brief": message or "confirmation"},
                {"id": "s", "agent": "synthesizer", "depends_on": ["m"], "task_brief": "呈現確認"},
            ]},
        }])
        ctx = AgentTurnContext(user_id="owner", room_id=room, message=message,
                               user_profile=profiles.find_one({"user_id": "owner"}), choice_id=choice, choice_action=action)
        result = scheduler.run_public_agent_turn_v3(ctx)
        if persist:
            message_id = str(messages.insert_one({"room_id": room, "content": result.reply,
                                "metadata": {"choice_prompt": result.choice_prompt}}).inserted_id)
            if result.choice_prompt:
                ConfirmationManager(confirmations).mark_presented(user_id="owner", origin_run_id=result.agent_run_id,
                                                                  message_id=message_id, persisted_content=result.reply)
        return result
    return SimpleNamespace(send=send, matches=matches, profiles=profiles, jobs=searches,
                           choices=confirmations, messages=messages, old_id=old_id, expired_id=expired_id,
                           provider=provider, context=context)


def test_withdraw_then_new_search_never_revives_expired_proposal(flow):
    # Decisions are made from the Hub card; the canonical service remains the
    # shared transition used by the UI and agent status reads.
    actions.decide_match(
        user_id="owner", match_id=str(flow.old_id), action="cancel",
        expected_status="pending", expected_revision=2,
        expected_namespace="relationship_match",
    )
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == "declined"
    assert flow.matches.find_one({"_id": flow.expired_id})["status"] == "expired"
    before = (flow.matches.writes, flow.profiles.writes)
    for _ in range(5):
        assert state.load_match_state("owner")["active_proposal"] is None
        state.get_match_status_snapshot("owner")
        jobs.public_match_search_status("owner")
    assert before == (flow.matches.writes, flow.profiles.writes)
    search = flow.send("我想配對")
    assert "有興趣" not in search.reply
    assert not flow.jobs.rows
    started = flow.send(choice=search.choice_prompt["id"], action="confirm")
    assert started.match_state_changed
    assert len(flow.jobs.rows) == 1
    assert flow.jobs.rows[0]["origin_room_id"] == "room"
    flow.send(choice=search.choice_prompt["id"], action="confirm")
    assert len(flow.jobs.rows) == 1


@pytest.mark.parametrize("stage", ["draft", "pending"])
@pytest.mark.parametrize("intent", ["start_search", "restart_search"])
def test_search_request_never_abandons_active_proposal(flow, stage, intent):
    flow.matches.update_one({"_id": flow.old_id}, {"$set": {"status": stage}})
    before = flow.matches.writes
    result = flow.send("撤回再重新找" if intent == "restart_search" else "我想要配對", intent)
    if stage == "draft":
        assert result.choice_prompt is None
        assert not flow.choices.rows
        assert "阿月牽線" in result.reply
    else:
        assert result.choice_prompt
        assert "原本那張邀請會繼續等對方回覆" in result.reply
    assert not flow.jobs.rows
    assert flow.matches.writes == before
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == stage
    assert "確認放棄" not in result.reply


def test_explicit_restart_without_active_proposal_confirms_one_search(flow):
    flow.matches.update_one({"_id": flow.old_id}, {"$set": {"status": "declined"}})
    result = flow.send("重新配對", "restart_search")
    assert result.choice_prompt
    record = flow.choices.find_one({"_id": result.choice_prompt["id"]})
    assert record["tool_name"] == "match.start_search"
    assert not record["payload"].get("continuation")
    assert not flow.jobs.rows


def test_plain_search_uses_current_pending_revision_without_changing_the_card(flow):
    flow.matches.update_one({"_id": flow.old_id}, {"$inc": {"proposal_revision": 1}})
    result = flow.send("重新配對", "start_search")
    assert result.choice_prompt
    assert not flow.jobs.rows


def test_explicit_restart_with_an_undecided_draft_redirects_to_hub(flow):
    flow.matches.insert_one({"_id": ObjectId(), "from_user": "owner", "to_user": "new", "status": "draft", "created_at": 300, "proposal_revision": 1})
    result = flow.send("重新配對", "restart_search")
    assert result.choice_prompt is None
    assert not flow.choices.rows
    assert not flow.jobs.rows
    assert "牽線專區" in result.reply


def test_restart_wording_in_another_room_does_not_change_active_proposal(flow):
    result = flow.send("我要換人重新配對", "restart_search", room="another")
    assert result.choice_prompt
    assert not flow.jobs.rows
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == "pending"


def test_injected_downstream_acceptance_is_rejected_for_start(flow, monkeypatch):
    from services.ayue_agent.v3.contracts import ToolProposal
    from services.ayue_agent.v3.runtime_registry import RuntimeRegistration, proposal_runner
    from services.ayue_agent.v3.sub_agents.base import SubAgentMetrics
    monkeypatch.setitem(scheduler._SUB_AGENT_RUNNERS, "match", RuntimeRegistration(runner=proposal_runner(
        lambda *_a, **_kw: ([ToolProposal(tool_name="match.decide_active_proposal", arguments={"decision": "interested"})], SubAgentMetrics()),
    )))
    result = flow.send("我想配對")
    assert result.choice_prompt is None
    assert "不一致" in result.reply
    assert not flow.choices.rows


def test_unknown_match_intent_is_read_only(flow):
    result = flow.send("我要配對", intent=None)
    assert result.fallback_reason == "planner_invalid"
    assert not flow.choices.rows and not flow.jobs.rows
    assert "沒有執行" in result.reply


def test_repeated_search_requests_leave_active_proposal_unchanged(flow):
    for _ in range(3):
        result = flow.send("我想要配對", "start_search")
        assert result.choice_prompt
    assert len([row for row in flow.choices.rows if row.get("status") == "pending"]) == 1
    assert not flow.jobs.rows
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == "pending"


@pytest.mark.parametrize("intent,expected", [("accept_proposal", "pending"), ("dismiss_proposal", "declined")])
def test_new_draft_revision_zero_is_actionable(flow, intent, expected):
    flow.matches.update_one({"_id": flow.old_id}, {"$set": {"status": "draft", "proposal_revision": 0}})
    first = flow.send("接受這張" if intent == "accept_proposal" else "不要這張", intent)
    assert first.choice_prompt is None
    assert "阿月牽線" in first.reply
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == "draft"
    assert not flow.jobs.rows


def test_incoming_proposal_can_be_declined_without_starting_search(flow):
    flow.matches.update_one({"_id": flow.old_id}, {"$set": {"from_user": "other", "to_user": "owner", "proposal_revision": 0}})
    result = flow.send("不要這張", "dismiss_proposal")
    assert result.choice_prompt is None
    assert "阿月牽線" in result.reply
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == "pending"
    assert not flow.jobs.rows


def test_old_restart_confirmation_cannot_execute_or_restore_abandonment(flow):
    first = flow.send("不要這張", "dismiss_proposal")
    assert first.choice_prompt is None
    assert "阿月牽線" in first.reply
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == "pending"
    assert not flow.jobs.rows


def test_failed_new_search_does_not_touch_old_terminal_proposal(flow, monkeypatch):
    flow.matches.update_one({"_id": flow.old_id}, {"$set": {"status": "declined"}})
    first = flow.send("重新配對", "restart_search")
    monkeypatch.setattr(write_executors, "start_match_search", Mock(side_effect=RuntimeError("isolated failure")))
    result = flow.send(choice=first.choice_prompt["id"], action="confirm")
    assert not result.match_state_changed
    assert not flow.jobs.rows
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == "declined"


def test_search_created_before_new_confirmation_blocks_duplicate(flow):
    flow.matches.update_one({"_id": flow.old_id}, {"$set": {"status": "declined"}})
    first = flow.send("重新配對", "restart_search")
    flow.jobs.insert_one({"user_id": "owner", "active_user_id": "owner", "job_id": "other-search",
                         "status": "running", "created_at": 300, "idempotency_key": "other-request"})
    result = flow.send(choice=first.choice_prompt["id"], action="confirm")
    assert "重複" in result.reply or "正在幫你找" in result.reply
    assert len(flow.jobs.rows) == 1
    assert flow.jobs.rows[0]["job_id"] == "other-search"
