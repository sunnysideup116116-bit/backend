"""Regression for keeping Match proposal decisions on the Hub card surface."""

from unittest.mock import Mock

from services.ai_service import ToolCallResult
from services.ayue_agent.contracts import AgentTurnContext, PublicAgentTurnContext
from services.ayue_agent.v3 import match_runtime, planner, scheduler, synthesizer
from services.ayue_agent.v3.sub_agents import base
from services.ayue_agent.v3.test_store import MemoryCollection


def test_match_withdrawal_in_chat_redirects_without_confirmation(monkeypatch):
    ctx = AgentTurnContext(
        user_id="withdrawal-owner",
        room_id="withdrawal-room",
        message="我要撤回這張牽線卡",
    )
    turn = PublicAgentTurnContext(
        user_id=ctx.user_id,
        room_id=ctx.room_id,
        message=ctx.message,
        active_proposal={
            "status": "pending",
            "user_can_decide": False,
            "allowed_actions": ["cancelled"],
            "proposal_revision": 7,
            "counterparty": "測試對象",
        },
    )
    turn._active_proposal_authority = {
        "match_id": "bound-match",
        "expected_status": "pending",
        "proposal_namespace": "relationship_match",
    }
    confirmations = MemoryCollection()
    monkeypatch.setattr(scheduler, "_CONFIRMATIONS", confirmations)
    monkeypatch.setattr(
        scheduler,
        "build_public_agent_turn_context",
        lambda *_args, **_kwargs: turn,
    )
    monkeypatch.setattr(match_runtime, "load_match_state", lambda _user_id: {
        "active_proposal": {
            "_id": "bound-match",
            "status": "pending",
            "proposal_revision": 7,
        },
        "stage": "waiting_other",
        "allowed_actions": ["cancelled"],
        "ambiguous": False,
        "search": {"status": "idle"},
        "search_blocked": True,
    })
    monkeypatch.setattr(scheduler, "_persist_trace", lambda *_args, **_kwargs: None)
    provider = Mock(return_value=ToolCallResult(content="", tool_calls=[{
        "name": "decompose_tasks",
        "arguments": {
            "write_intent": "none",
            "tasks": [
                {
                    "id": "m1",
                    "agent": "match",
                    "depends_on": [],
                    "task_brief": "撤回目前人物牽線提案",
                    "match_intent": "dismiss_proposal",
                },
                {
                    "id": "s1",
                    "agent": "synthesizer",
                    "depends_on": ["m1"],
                    "task_brief": "引導使用者到 Hub 卡片操作",
                },
            ],
        },
    }]))
    monkeypatch.setattr(planner, "generate_chat_completion_with_tools", provider)
    match_provider = Mock()
    monkeypatch.setattr(base, "generate_chat_completion_with_tools", match_provider)
    hub_reply = "這是人物牽線提案，請到「阿月牽線」這張卡片撤回。"
    monkeypatch.setattr(synthesizer, "synthesize", lambda *_args, **_kwargs: (
        hub_reply,
        None,
        synthesizer.SynthesizerMetrics(),
    ))
    execute = Mock()
    monkeypatch.setattr(scheduler, "execute_write", execute)

    result = scheduler.run_public_agent_turn_v3(ctx)

    assert result.fallback_reason is None
    assert result.choice_prompt is None
    assert result.match_state_changed is False
    assert "阿月牽線" in result.reply
    provider.assert_called_once()
    match_provider.assert_not_called()
    execute.assert_not_called()
    assert confirmations.find({}) == []
