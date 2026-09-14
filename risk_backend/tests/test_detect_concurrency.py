"""API ordering and dependency failure contracts, using only in-memory fakes."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api import risk_detection as api
from app.models.schemas import RiskDetectionRequest, RiskState
from app.services.kb_service import KBUnavailableError


@pytest.fixture
def detect_fakes(monkeypatch):
    events = []
    checked = []
    monkeypatch.setattr(api.guardrail_engine, "check", AsyncMock(return_value={
        "is_blocked": False, "flagged_words": [], "classifier_flagged": False,
    }))
    monkeypatch.setattr(api.chat_log_service, "log_message", AsyncMock(return_value=True))
    monkeypatch.setattr(api.chat_log_service.rel_service, "get_memory_context", AsyncMock(return_value={"metrics": None, "summary": None}))
    monkeypatch.setattr(api.state_machine, "get_user_state", AsyncMock(return_value=(RiskState(), None)))
    monkeypatch.setattr(api.chat_log_service, "get_recent_messages", AsyncMock(return_value=[]))
    monkeypatch.setattr(api.chat_log_service, "get_recent_behavior_messages", AsyncMock(return_value=[]))
    monkeypatch.setattr(api.rule_engine, "calculate", lambda *args: {"delta": RiskState(), "triggered_rules": []})
    monkeypatch.setattr(api.nlp_engine, "analyze", lambda *args, **kwargs: {"delta": RiskState(), "confidence": 1.0, "reasoning": "test"})
    monkeypatch.setattr(api.fusion, "fuse", lambda *args, **kwargs: RiskState())
    monkeypatch.setattr(api.scenario_risk_layer, "evaluate", lambda *args, **kwargs: (RiskState(), []))
    monkeypatch.setattr(api.fusion, "apply_scenario_bonus", lambda *args: RiskState())
    monkeypatch.setattr(api.state_machine, "update", AsyncMock(return_value=(RiskState(), "observation")))
    monkeypatch.setattr(api.background_judge_service, "should_review", lambda *args: False)

    async def intervene(**kwargs):
        # The next message must see the first intervention persisted even though
        # FastAPI's response background tasks have not been run.
        checked.append(list(events))
        return {"sender_directive": {"action": "banner", "content": None},
                "receiver_directive": {"action": "none", "content": None}}

    async def log(*args, **kwargs):
        await asyncio.sleep(0.01)
        events.append("intervention-saved")
        return True

    async def status(*args, **kwargs):
        await asyncio.sleep(0.01)
        events.append("status-saved")
        return True

    monkeypatch.setattr(api.intervention_engine, "execute", intervene)
    monkeypatch.setattr(api.chat_log_service, "log_intervention", log)
    monkeypatch.setattr(api.chat_log_service, "update_message_status", status)
    return events, checked


def request():
    return RiskDetectionRequest(conversation_id="same", sender_id="alice", receiver_id="bob", current_message="hello")


def test_detect_commits_intervention_and_delivery_before_next_message(detect_fakes):
    events, checked = detect_fakes

    async def run():
        tasks = [BackgroundTasks(), BackgroundTasks()]
        results = await asyncio.gather(*(api.detect_risk(request(), background) for background in tasks))
        assert all(result.risk_level == "observation" for result in results)
        assert checked == [[], ["intervention-saved", "status-saved"]]
        assert events == ["intervention-saved", "status-saved"] * 2
        for background in tasks:
            assert all(task.func not in (api.chat_log_service.log_intervention, api.chat_log_service.update_message_status) for task in background.tasks)

    asyncio.run(run())


def test_detect_returns_503_when_rules_cannot_be_refreshed(detect_fakes, monkeypatch):
    def unavailable(*args):
        raise KBUnavailableError("mock unavailable")

    monkeypatch.setattr(api.rule_engine, "calculate", unavailable)
    with pytest.raises(HTTPException) as failure:
        asyncio.run(api.detect_risk(request(), BackgroundTasks()))
    assert failure.value.status_code == 503
    assert detect_fakes[0] == [], "a failed KB read must not deliver a safe decision"
