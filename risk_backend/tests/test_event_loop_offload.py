"""Runtime checks that synchronous model clients do not serialize the event loop."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks

from app.models.schemas import RiskDetectionRequest, RiskState


def _slow_result(value, delay=0.12):
    time.sleep(delay)
    return value


def _run_four(factory):
    async def run():
        started = time.perf_counter()
        results = await asyncio.gather(*(factory(index) for index in range(4)))
        return time.perf_counter() - started, results

    elapsed, results = asyncio.run(run())
    assert elapsed < 0.35, f"four calls were serialized: {elapsed:.3f}s"
    return results


@pytest.mark.parametrize("provider", ["openai_moderation", "llm_classifier"])
def test_guardrail_sync_providers_run_off_event_loop(provider):
    from app.core.guardrail_engine import GuardrailEngine

    engine = GuardrailEngine.__new__(GuardrailEngine)
    engine.HARD_BLOCK_RECORDS = []
    engine._provider = provider
    engine._classifier_adapter = object() if provider == "llm_classifier" else None
    result = {
        "is_blocked": False,
        "reason": "Passed",
        "classifier_flagged": False,
    }
    engine._check_via_openai_moderation = lambda _: _slow_result(dict(result))
    engine._check_via_classifier = lambda _: _slow_result(dict(result))

    responses = _run_four(lambda index: engine.check(f"message-{index}"))
    assert all(response["is_blocked"] is False for response in responses)


def test_summary_llm_sync_adapter_runs_off_event_loop(monkeypatch):
    from app.core.llm_adapters import get_summary_adapter
    from app.core.nlp_engine import NLPEngine

    adapter = MagicMock()
    adapter.generate.side_effect = lambda *_, **__: _slow_result(
        json.dumps({"summary": "ok"})
    )
    monkeypatch.setattr(
        "app.core.llm_adapters.get_summary_adapter",
        lambda: adapter,
    )
    assert get_summary_adapter is not None
    engine = NLPEngine.__new__(NLPEngine)

    responses = _run_four(
        lambda index: engine.get_raw_llm_response(f"prompt-{index}", "test")
    )
    assert all(json.loads(response)["summary"] == "ok" for response in responses)


def test_background_judge_sync_adapter_runs_off_event_loop(monkeypatch):
    from app.services.background_judge_service import BackgroundJudgeService

    monkeypatch.setenv("BACKGROUND_JUDGE_ENABLED", "true")
    adapter = MagicMock()
    adapter.generate.side_effect = lambda *_, **__: _slow_result(
        '{"judgment":"healthy","reasoning":"test"}'
    )
    chat_log = MagicMock()
    chat_log.save_guardrail_context_review = AsyncMock(return_value=True)
    service = BackgroundJudgeService(
        chat_log_service=chat_log,
        adapter=adapter,
        model="test",
    )

    _run_four(
        lambda index: service.review_guardrail_context(
            conversation_id="conversation",
            sender_id="alice",
            msg_id=f"message-{index}",
            current_message="hello",
            recent_messages=[],
            flagged_words=["flag"],
            classifier_flag={"flagged": False},
        )
    )
    assert chat_log.save_guardrail_context_review.await_count == 4


def test_detect_requests_do_not_serialize_sync_nlp(monkeypatch):
    from app.api import risk_detection as api

    monkeypatch.setattr(
        api.guardrail_engine,
        "check",
        AsyncMock(
            return_value={
                "is_blocked": False,
                "reason": "Passed",
                "flagged_words": [],
                "classifier_flagged": False,
                "degraded": False,
            }
        ),
    )
    monkeypatch.setattr(
        api.chat_log_service,
        "log_message",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        api.chat_log_service.rel_service,
        "get_memory_context",
        AsyncMock(return_value={"metrics": {}, "summary": None}),
    )
    monkeypatch.setattr(
        api.state_machine,
        "get_user_state",
        AsyncMock(return_value=(RiskState(), None)),
    )
    monkeypatch.setattr(
        api.chat_log_service,
        "get_recent_messages",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        api.chat_log_service,
        "get_recent_behavior_messages",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        api.rule_engine,
        "calculate",
        MagicMock(return_value={"delta": RiskState(), "triggered_rules": []}),
    )
    monkeypatch.setattr(
        api.nlp_engine,
        "analyze",
        lambda *_, **__: _slow_result(
            {
                "delta": RiskState(),
                "confidence": 1.0,
                "reasoning": "safe",
                "detected_features": [],
            }
        ),
    )
    monkeypatch.setattr(api.fusion, "fuse", MagicMock(return_value=RiskState()))
    monkeypatch.setattr(
        api.scenario_risk_layer,
        "evaluate",
        MagicMock(return_value=(RiskState(), [])),
    )
    monkeypatch.setattr(
        api.fusion,
        "apply_scenario_bonus",
        MagicMock(return_value=RiskState()),
    )
    monkeypatch.setattr(
        api.state_machine,
        "update",
        AsyncMock(return_value=(RiskState(), "safe")),
    )
    api.state_machine.last_diagnostic = {
        "reason": "normal",
        "composite_score": 0.0,
        "max_score": 0.0,
        "spread_score": 0.0,
        "trend_score": 0.0,
    }
    monkeypatch.setattr(
        api.intervention_engine,
        "execute",
        AsyncMock(
            return_value={
                "sanction_exempted": False,
                "sender_directive": {"action": "none", "content": None},
                "receiver_directive": {"action": "none", "content": None},
            }
        ),
    )
    monkeypatch.setattr(
        api.background_judge_service,
        "should_review",
        MagicMock(return_value=False),
    )

    responses = _run_four(
        lambda index: api.detect_risk(
            RiskDetectionRequest(
                conversation_id=f"conversation-{index}",
                current_message="hello",
                sender_id=f"sender-{index}",
                receiver_id=f"receiver-{index}",
            ),
            BackgroundTasks(),
        )
    )
    assert all(response.risk_level == "safe" for response in responses)
