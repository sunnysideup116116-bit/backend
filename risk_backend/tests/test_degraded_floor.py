"""Display-floor behavior when semantic risk analysis is degraded."""

import asyncio
from unittest.mock import patch

from app.models.schemas import RiskState


CONFIG = {
    "decay_factor": 0.7,
    "weights": {"decision_logic": {
        "W_MAX": 0.60,
        "W_SPREAD": 0.30,
        "W_TREND": 0.10,
        "NOISE_FLOOR": 0.10,
        "DECREASE_DAMPING": 0.03,
        "SPREAD_MODE": "effdim",
    }},
}


class FakeLog:
    def __init__(self):
        self.saved_state = None
        self.saved_level = None

    async def get_latest_risk_state_with_time(self, *_):
        return RiskState(), None

    async def get_recent_risk_state_history(self, *_, **__):
        return []

    async def get_recent_feedbacks(self, *_, **__):
        return []

    async def get_recent_guardrail_context_reviews(self, *_, **__):
        return []

    async def save_risk_state_history(
        self, _conversation, _user, _message, state, level, _delta, **_,
    ):
        self.saved_state = state
        self.saved_level = level


def _run(delta: RiskState, degraded: bool):
    from app.core.risk_state import RiskStateMachine

    machine = RiskStateMachine()
    log = FakeLog()
    machine.chat_log_service = log
    with patch("app.core.risk_state.KBService.get_fusion_config", return_value=CONFIG):
        state, level = asyncio.run(machine.update(
            "conversation", "user", "message", delta,
            degraded_with_flags=degraded,
        ))
    return state, level, machine.last_diagnostic, log


def test_floor_lifts_safe_to_observation():
    _, level, diagnostic, _ = _run(RiskState(), True)
    assert level == "observation"
    assert diagnostic["reason"] == "degraded_floor"
    assert diagnostic["composite_score"] < 0.10


def test_floor_does_not_touch_risk_state():
    state, _, _, log = _run(RiskState(), True)
    assert state.model_dump() == RiskState().model_dump()
    assert log.saved_state.model_dump() == RiskState().model_dump()


def test_floor_never_lowers_an_existing_judgement():
    _, level, diagnostic, _ = _run(RiskState(harassment=0.55), True)
    assert level in {"warning", "restricted", "blocked"}
    assert diagnostic["reason"] != "degraded_floor"


def test_no_floor_when_not_degraded():
    _, level, diagnostic, _ = _run(RiskState(), False)
    assert level == "safe"
    assert diagnostic["reason"] != "degraded_floor"


def test_floor_is_idempotent_across_calls():
    assert [_run(RiskState(), True)[1] for _ in range(5)] == ["observation"] * 5
