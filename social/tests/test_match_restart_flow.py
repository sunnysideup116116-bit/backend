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
    preview = flow.send("撤回", "dismiss_proposal")
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == "pending"
    done = flow.send(choice=preview.choice_prompt["id"], action="confirm")
    assert done.choice_prompt is None
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
def test_restart_requires_two_confirmations_and_replay_reuses_child(flow, stage):
    flow.matches.update_one({"_id": flow.old_id}, {"$set": {"status": stage}})
    first = flow.send("撤回再重新找")
    parent_id = first.choice_prompt["id"]
    record = flow.choices.find_one({"_id": parent_id})
    assert record["arguments"]["decision"] == ("declined" if stage == "draft" else "cancelled")
    assert record["payload"]["continuation"] == "offer_start_search"
    second = flow.send(choice=parent_id, action="confirm", persist=False)
    assert second.choice_resolution["state"] == "confirmed"
    assert second.choice_prompt["id"] != parent_id
    assert not flow.jobs.rows
    replay = flow.send(choice=parent_id, action="confirm")
    assert replay.choice_prompt["id"] == second.choice_prompt["id"]
    assert replay.agent_run_id == second.agent_run_id
    assert len(flow.choices.rows) == 2
    started = flow.send(choice=second.choice_prompt["id"], action="confirm")
    assert started.match_state_changed
    assert len(flow.jobs.rows) == 1


@pytest.mark.parametrize("cancel_step", [1, 2])
def test_cancel_each_step_has_no_unrequested_write(flow, cancel_step):
    first = flow.send("我要重新配對")
    if cancel_step == 1:
        flow.send(choice=first.choice_prompt["id"], action="cancel")
        assert flow.matches.find_one({"_id": flow.old_id})["status"] == "pending"
    else:
        second = flow.send(choice=first.choice_prompt["id"], action="confirm")
        flow.send(choice=second.choice_prompt["id"], action="cancel")
        assert flow.matches.find_one({"_id": flow.old_id})["status"] == "declined"
    assert not flow.jobs.rows


def test_stale_first_step_never_creates_search_confirmation(flow):
    first = flow.send("重新配對")
    flow.matches.update_one({"_id": flow.old_id}, {"$inc": {"proposal_revision": 1}})
    result = flow.send(choice=first.choice_prompt["id"], action="confirm")
    assert result.choice_prompt is None
    assert len(flow.choices.rows) == 1
    assert not flow.jobs.rows


def test_new_proposal_between_steps_blocks_start(flow):
    first = flow.send("重新配對")
    second = flow.send(choice=first.choice_prompt["id"], action="confirm")
    flow.matches.insert_one({"_id": ObjectId(), "from_user": "owner", "to_user": "new", "status": "draft", "created_at": 300, "proposal_revision": 1})
    result = flow.send(choice=second.choice_prompt["id"], action="confirm")
    assert not flow.jobs.rows
    assert "不重複" in result.reply or "沒有" in result.reply


def test_wrong_room_and_expired_first_choice_never_change_state(flow):
    first = flow.send("重新配對")
    result = flow.send(choice=first.choice_prompt["id"], action="confirm", room="another")
    assert result.choice_prompt is None
    flow.choices.update_one({"_id": first.choice_prompt["id"]}, {"$set": {"expires_at": 1}})
    result = flow.send(choice=first.choice_prompt["id"], action="confirm")
    assert result.choice_prompt is None
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


def test_two_clients_confirm_first_step_only_once(flow):
    first = flow.send("重新配對")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: flow.send(choice=first.choice_prompt["id"], action="confirm"), range(2)))
    assert len(flow.choices.rows) == 2
    assert flow.matches.find_one({"_id": flow.old_id})["proposal_revision"] == 3
    assert not flow.jobs.rows
    children = {r.choice_prompt["id"] for r in results if r.choice_prompt}
    assert len(children) == 1


@pytest.mark.parametrize("intent,expected", [("accept_proposal", "pending"), ("dismiss_proposal", "declined")])
def test_new_draft_revision_zero_is_actionable(flow, intent, expected):
    flow.matches.update_one({"_id": flow.old_id}, {"$set": {"status": "draft", "proposal_revision": 0}})
    first = flow.send("接受這張" if intent == "accept_proposal" else "不要這張", intent)
    assert first.choice_prompt
    record = flow.choices.find_one({"_id": first.choice_prompt["id"]})
    assert record["payload"]["proposal_revision"] == 0
    result = flow.send(choice=first.choice_prompt["id"], action="confirm")
    assert result.match_state_changed
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == expected
    assert not flow.jobs.rows


def test_incoming_proposal_can_be_declined_without_starting_search(flow):
    flow.matches.update_one({"_id": flow.old_id}, {"$set": {"from_user": "other", "to_user": "owner", "proposal_revision": 0}})
    preview = flow.send("不要這張", "dismiss_proposal")
    assert flow.choices.find_one({"_id": preview.choice_prompt["id"]})["arguments"] == {"decision": "declined"}
    flow.send(choice=preview.choice_prompt["id"], action="confirm")
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == "declined"
    assert not flow.jobs.rows


def test_second_confirmation_expiry_never_restores_old_proposal(flow):
    first = flow.send("重新配對")
    second = flow.send(choice=first.choice_prompt["id"], action="confirm")
    flow.choices.update_one({"_id": second.choice_prompt["id"]}, {"$set": {"expires_at": 1}})
    result = flow.send(choice=second.choice_prompt["id"], action="confirm")
    assert result.choice_prompt is None
    assert not flow.jobs.rows
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == "declined"


def test_failed_search_enqueue_keeps_old_proposal_ended(flow, monkeypatch):
    first = flow.send("重新配對")
    second = flow.send(choice=first.choice_prompt["id"], action="confirm")
    monkeypatch.setattr(write_executors, "start_match_search", Mock(side_effect=RuntimeError("isolated failure")))
    result = flow.send(choice=second.choice_prompt["id"], action="confirm")
    assert not result.match_state_changed
    assert not flow.jobs.rows
    assert flow.matches.find_one({"_id": flow.old_id})["status"] == "declined"
    assert "不能安全" in result.reply


def test_search_created_between_steps_is_not_duplicated(flow):
    first = flow.send("重新配對")
    second = flow.send(choice=first.choice_prompt["id"], action="confirm")
    flow.jobs.insert_one({"user_id": "owner", "active_user_id": "owner", "job_id": "other-search",
                         "status": "running", "created_at": 300, "idempotency_key": "other-request"})
    flow.send(choice=second.choice_prompt["id"], action="confirm")
    assert len(flow.jobs.rows) == 1
    assert flow.jobs.rows[0]["job_id"] == "other-search"
