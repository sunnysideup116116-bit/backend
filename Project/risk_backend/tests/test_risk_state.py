"""RiskStateMachine.update() core behavior tests.

Covers:
- initial accumulation (no prior, no history)
- message decay vs time decay
- critical_override (max >= T_CRITICAL -> blocked)
- composite formula (W_MAX x max + W_SPREAD x spread + trend_adj)
- cumulative state clipping
- negative short-message-buffer delta cannot make cumulative state negative
- asymmetric damping when trend is decreasing
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.schemas import RiskState


STATE_CONFIG = {
    "decay_factor": 0.9,
    "thresholds": {
        "T_SAFE": 0.10,
        "T_OBSERVATION": 0.20,
        "T_WARNING": 0.45,
        "T_RESTRICTED": 0.65,
        "T_CRITICAL": 0.85,
    },
    "weights": {
        "time_decay": {
            "unit": "hour",
            "tiers": [
                {"min_h": 0, "max_h": 1, "lambda": 0.057},
                {"min_h": 1, "max_h": 24, "lambda": 0.028},
                {"min_h": 24, "max_h": 72, "lambda": 0.014},
                {"min_h": 72, "max_h": 168, "lambda": 0.009},
                {"min_h": 168, "max_h": 9999, "lambda": 0.693},
            ],
        },
        "decision_logic": {
            "W_MAX": 0.60,
            "W_SPREAD": 0.30,
            "W_TREND": 0.10,
            "NOISE_FLOOR": 0.10,
            "DECREASE_DAMPING": 0.03,
        },
    },
}


@pytest.fixture
def machine(monkeypatch):
    """Create RiskStateMachine and replace ChatLogService with a mock."""
    monkeypatch.setenv("APPWRITE_ENDPOINT", "http://test/v1")
    monkeypatch.setenv("APPWRITE_PROJECT_ID", "test-proj")
    monkeypatch.setenv("APPWRITE_API_KEY", "test-key")
    monkeypatch.setenv("APPWRITE_DB_ID", "test-db")

    from app.core.risk_state import RiskStateMachine

    m = RiskStateMachine()
    m.chat_log_service = MagicMock()
    m.chat_log_service.save_risk_state_history = AsyncMock(return_value=None)
    return m


def _setup_mocks(machine, prior_state, last_ts_str=None, history=None):
    """Quickly set up the three ChatLogService method return values."""
    machine.chat_log_service.get_latest_risk_state_with_time = AsyncMock(
        return_value=(prior_state, last_ts_str)
    )
    machine.chat_log_service.get_recent_risk_state_history = AsyncMock(
        return_value=history or []
    )
    machine.chat_log_service.get_recent_feedbacks = AsyncMock(return_value=[])
    machine.chat_log_service.get_recent_guardrail_context_reviews = AsyncMock(return_value=[])


def _run(machine, delta):
    """asyncio.run wrapper for update()."""
    return asyncio.run(machine.update("conv1", "user1", "msg1", delta))


def test_no_history_zero_state(machine):
    _setup_mocks(machine, prior_state=RiskState())
    with patch("app.core.risk_state.KBService.get_fusion_config", return_value=STATE_CONFIG):
        new_state, _ = _run(machine, RiskState(harassment=0.3))

    assert new_state.harassment == pytest.approx(0.3)


def test_msg_decay_only(machine):
    now_iso = datetime.now(timezone.utc).isoformat()
    _setup_mocks(machine, prior_state=RiskState(harassment=0.5), last_ts_str=now_iso)

    with patch("app.core.risk_state.KBService.get_fusion_config", return_value=STATE_CONFIG):
        new_state, _ = _run(machine, RiskState())

    assert new_state.harassment == pytest.approx(0.45, abs=0.01)


def test_time_decay_12_hours(machine):
    twelve_h_ago = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    _setup_mocks(
        machine, prior_state=RiskState(harassment=0.5), last_ts_str=twelve_h_ago
    )

    with patch("app.core.risk_state.KBService.get_fusion_config", return_value=STATE_CONFIG):
        new_state, _ = _run(machine, RiskState())

    assert new_state.harassment == pytest.approx(0.322, abs=0.02)


def test_critical_override_at_threshold(machine):
    _setup_mocks(machine, prior_state=RiskState())

    with patch("app.core.risk_state.KBService.get_fusion_config", return_value=STATE_CONFIG):
        new_state, level = _run(machine, RiskState(sexual_boundary=0.9))

    assert level == "blocked"
    assert machine.last_diagnostic["reason"] == "critical_override"
    assert new_state.sexual_boundary == pytest.approx(0.9)


def test_composite_formula(machine):
    _setup_mocks(machine, prior_state=RiskState())

    with patch("app.core.risk_state.KBService.get_fusion_config", return_value=STATE_CONFIG):
        _run(machine, RiskState(harassment=0.3, manipulation=0.2))

    diag = machine.last_diagnostic
    assert diag["max_score"] == pytest.approx(0.3)
    assert diag["spread_score"] == pytest.approx(0.4)
    assert diag["composite_score"] == pytest.approx(0.30, abs=0.001)


def test_cumulative_clipped_at_one(machine):
    now_iso = datetime.now(timezone.utc).isoformat()
    _setup_mocks(
        machine, prior_state=RiskState(harassment=0.95), last_ts_str=now_iso
    )

    with patch("app.core.risk_state.KBService.get_fusion_config", return_value=STATE_CONFIG):
        new_state, _ = _run(machine, RiskState(harassment=0.5))

    assert new_state.harassment == pytest.approx(1.0)


def test_negative_delta_clipped_at_zero(machine):
    now_iso = datetime.now(timezone.utc).isoformat()
    _setup_mocks(
        machine, prior_state=RiskState(harassment=0.1), last_ts_str=now_iso
    )

    with patch("app.core.risk_state.KBService.get_fusion_config", return_value=STATE_CONFIG):
        new_state, _ = _run(machine, RiskState(harassment=-0.3))

    assert new_state.harassment == pytest.approx(0.0)


def test_trend_decreasing_uses_damping(machine):
    now_iso = datetime.now(timezone.utc).isoformat()
    _setup_mocks(
        machine,
        prior_state=RiskState(harassment=0.5),
        last_ts_str=now_iso,
        history=[RiskState(harassment=0.5)],
    )

    with patch("app.core.risk_state.KBService.get_fusion_config", return_value=STATE_CONFIG):
        _run(machine, RiskState())

    diag = machine.last_diagnostic
    assert diag["trend_score"] == pytest.approx(-0.05, abs=0.01)
    assert diag["composite_score"] == pytest.approx(0.3285, abs=0.01)


def test_no_feedback_neutral_signal(machine):
    _setup_mocks(machine, prior_state=RiskState(harassment=0.5))

    with patch("app.core.risk_state.KBService.get_fusion_config", return_value=STATE_CONFIG):
        _run(machine, RiskState())

    assert machine.last_diagnostic["feedback_signal"] == "neutral"


def test_two_comfortable_triggers_trust(machine):
    now_iso = datetime.now(timezone.utc).isoformat()
    _setup_mocks(
        machine, prior_state=RiskState(harassment=0.5), last_ts_str=now_iso
    )
    machine.chat_log_service.get_recent_feedbacks = AsyncMock(
        return_value=["comfortable", "comfortable", "comfortable"]
    )

    with patch("app.core.risk_state.KBService.get_fusion_config", return_value=STATE_CONFIG):
        new_state, _ = _run(machine, RiskState())

    assert machine.last_diagnostic["feedback_signal"] == "trust"
    assert new_state.harassment == pytest.approx(0.315, abs=0.01)


def test_one_uncomfortable_triggers_alert(machine):
    _setup_mocks(machine, prior_state=RiskState())
    machine.chat_log_service.get_recent_feedbacks = AsyncMock(
        return_value=["uncomfortable"]
    )

    with patch("app.core.risk_state.KBService.get_fusion_config", return_value=STATE_CONFIG):
        new_state, _ = _run(machine, RiskState(harassment=0.2))

    assert machine.last_diagnostic["feedback_signal"] == "alert"
    assert new_state.harassment == pytest.approx(0.3)


def test_alert_wins_over_trust(machine):
    _setup_mocks(machine, prior_state=RiskState())
    machine.chat_log_service.get_recent_feedbacks = AsyncMock(
        return_value=["comfortable", "comfortable", "uncomfortable"]
    )

    with patch("app.core.risk_state.KBService.get_fusion_config", return_value=STATE_CONFIG):
        new_state, _ = _run(machine, RiskState(harassment=0.2))

    assert machine.last_diagnostic["feedback_signal"] == "alert"
    assert new_state.harassment == pytest.approx(0.3)


def test_one_comfortable_not_enough(machine):
    now_iso = datetime.now(timezone.utc).isoformat()
    _setup_mocks(
        machine, prior_state=RiskState(harassment=0.5), last_ts_str=now_iso
    )
    machine.chat_log_service.get_recent_feedbacks = AsyncMock(
        return_value=["comfortable"]
    )

    with patch("app.core.risk_state.KBService.get_fusion_config", return_value=STATE_CONFIG):
        new_state, _ = _run(machine, RiskState())

    assert machine.last_diagnostic["feedback_signal"] == "neutral"
    assert new_state.harassment == pytest.approx(0.45, abs=0.01)


def test_alert_with_zero_delta_no_boost(machine):
    _setup_mocks(machine, prior_state=RiskState(harassment=0.2))
    machine.chat_log_service.get_recent_feedbacks = AsyncMock(
        return_value=["uncomfortable"]
    )

    with patch("app.core.risk_state.KBService.get_fusion_config", return_value=STATE_CONFIG):
        new_state, _ = _run(machine, RiskState())

    assert machine.last_diagnostic["feedback_signal"] == "alert"
    assert new_state.harassment == pytest.approx(0.18, abs=0.02)


def test_background_concerning_triggers_alert(machine):
    _setup_mocks(machine, prior_state=RiskState())
    machine.chat_log_service.get_recent_guardrail_context_reviews = AsyncMock(
        return_value=["concerning"]
    )

    with patch("app.core.risk_state.KBService.get_fusion_config", return_value=STATE_CONFIG):
        new_state, _ = _run(machine, RiskState(harassment=0.2))

    assert machine.last_diagnostic["feedback_signal"] == "alert"
    assert new_state.harassment == pytest.approx(0.3)


def test_two_background_healthy_triggers_trust(machine):
    now_iso = datetime.now(timezone.utc).isoformat()
    _setup_mocks(machine, prior_state=RiskState(harassment=0.5), last_ts_str=now_iso)
    machine.chat_log_service.get_recent_guardrail_context_reviews = AsyncMock(
        return_value=["healthy", "healthy"]
    )

    with patch("app.core.risk_state.KBService.get_fusion_config", return_value=STATE_CONFIG):
        new_state, _ = _run(machine, RiskState())

    assert machine.last_diagnostic["feedback_signal"] == "trust"
    assert new_state.harassment == pytest.approx(0.315, abs=0.01)


def test_background_concerning_wins_over_feedback_trust(machine):
    _setup_mocks(machine, prior_state=RiskState())
    machine.chat_log_service.get_recent_feedbacks = AsyncMock(
        return_value=["comfortable", "comfortable"]
    )
    machine.chat_log_service.get_recent_guardrail_context_reviews = AsyncMock(
        return_value=["healthy", "concerning"]
    )

    with patch("app.core.risk_state.KBService.get_fusion_config", return_value=STATE_CONFIG):
        new_state, _ = _run(machine, RiskState(harassment=0.2))

    assert machine.last_diagnostic["feedback_signal"] == "alert"
    assert new_state.harassment == pytest.approx(0.3)
