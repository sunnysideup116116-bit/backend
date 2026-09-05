"""Regression for provider result labels blocking the withdrawal confirmation."""

import json
from unittest.mock import Mock

import pytest

from services.ai_service import ToolCallResult
from services.ayue_agent.contracts import AgentTurnContext, PublicAgentTurnContext
from services.ayue_agent.v3 import planner, scheduler, synthesizer
from services.ayue_agent.v3 import match_runtime
from services.ayue_agent.v3.confirmation import ConfirmationManager
from services.ayue_agent.v3.sub_agents import base
from services.ayue_agent.v3.test_store import MemoryCollection


@pytest.mark.parametrize("label", ["match.proposal_cancelled", "task.finished"])
@pytest.mark.parametrize("button_action", ["confirm", "cancel"])
def test_withdrawal_repaired_plan_reaches_exact_room_confirmation(monkeypatch, label, button_action):
    ctx = AgentTurnContext(user_id="withdrawal-owner", room_id="withdrawal-room", message="我要撤回")
    turn = PublicAgentTurnContext(
        user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message,
        active_proposal={
            "status": "pending", "user_can_decide": False,
            "allowed_actions": ["cancelled"], "proposal_revision": 7,
            "counterparty": "測試對象",
        },
    )
    turn._active_proposal_authority = {
        "match_id": "bound-match", "expected_status": "pending",
        "proposal_namespace": "relationship_match",
    }
    confirmations = MemoryCollection()
    monkeypatch.setattr(scheduler, "_CONFIRMATIONS", confirmations)
    monkeypatch.setattr(scheduler, "build_public_agent_turn_context", lambda *_a, **_kw: turn)
    monkeypatch.setattr(match_runtime, "load_match_state", lambda _uid: {
        "active_proposal": {"_id": "bound-match", "status": "pending", "proposal_revision": 7},
        "stage": "waiting_other", "allowed_actions": ["cancelled"], "ambiguous": False,
        "search": {"status": "idle"}, "search_blocked": True,
    })
    monkeypatch.setattr(scheduler, "_persist_trace", lambda *_a, **_kw: None)
    monkeypatch.setattr(scheduler, "sync_choice_message_projection", lambda *_a, **_kw: None)
    provider = Mock(return_value=ToolCallResult(content="", tool_calls=[{
        "name": "decompose_tasks",
        "arguments": {"write_intent": "none", "tasks": [
            {"id": "m1", "agent": "match", "depends_on": [],
             "task_brief": "撤回目前提案", "match_intent": "dismiss_proposal", "outcome_contract": label},
            {"id": "s1", "agent": "synthesizer", "depends_on": ["m1"],
             "task_brief": "呈現 server 確認預覽"},
        ]},
    }]))
    monkeypatch.setattr(planner, "generate_chat_completion_with_tools", provider)
    match_provider = Mock(return_value=ToolCallResult(content="", tool_calls=[{
        "name": "match.decide_active_proposal", "arguments": {"decision": "cancelled"},
    }]))
    monkeypatch.setattr(base, "generate_chat_completion_with_tools", match_provider)
    preview = "要撤回目前提案嗎？確認後我才會送出。"
    monkeypatch.setattr(synthesizer, "synthesize", lambda *_a, **_kw: (
        preview, None, synthesizer.SynthesizerMetrics(),
    ))
    execute = Mock(return_value=(True, "已撤回此次提案。", None))
    monkeypatch.setattr(scheduler, "execute_write", execute)

    result = scheduler.run_public_agent_turn_v3(ctx)

    assert result.fallback_reason is None
    assert result.choice_prompt is not None
    assert result.match_state_changed is False
    provider.assert_called_once()
    match_provider.assert_not_called()
    execute.assert_not_called()
    record = confirmations.find({})[0]
    assert record["status"] == "prepared"
    assert record["interaction_mode"] == "bubble_buttons_v1"
    assert record["room_id"] == ctx.room_id
    assert record["arguments"] == {"decision": "cancelled"}
    assert record["payload"] == {
        "match_id": "bound-match", "expected_status": "pending",
        "proposal_namespace": "relationship_match", "proposal_revision": 7,
    }
    public_choice = json.dumps(result.choice_prompt)
    model_prompts = repr(provider.call_args) + repr(match_provider.call_args)
    assert "bound-match" not in public_choice + model_prompts
    assert "proposal_revision" not in public_choice

    manager = ConfirmationManager(confirmations)
    assert manager.mark_presented(
        user_id=ctx.user_id, origin_run_id=result.agent_run_id,
        message_id="saved-preview", persisted_content=result.reply,
    )
    button_ctx = ctx.model_copy(update={
        "message": "", "choice_id": result.choice_prompt["id"], "choice_action": button_action,
    })
    scheduler.run_public_agent_turn_v3(button_ctx.model_copy(update={"room_id": "another-room"}))
    execute.assert_not_called()
    resolved = scheduler.run_public_agent_turn_v3(button_ctx)
    scheduler.run_public_agent_turn_v3(button_ctx)
    if button_action == "confirm":
        execute.assert_called_once()
        assert execute.call_args.args[:2] == ("match.decide_active_proposal", {"decision": "cancelled"})
        assert execute.call_args.kwargs["payload"]["match_id"] == "bound-match"
        assert resolved.match_state_changed is True
    else:
        execute.assert_not_called()
        assert resolved.match_state_changed is False
