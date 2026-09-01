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


# --- SPREAD_MODE 開關（校準期暫時設施，見 risk_state.compute_spread 說明）---

@pytest.fixture
def spread(monkeypatch):
    """取得 compute_spread（本檔的 import 一律延後到 fixture 內，見上方 machine）。"""
    monkeypatch.setenv("APPWRITE_ENDPOINT", "http://test/v1")
    monkeypatch.setenv("APPWRITE_PROJECT_ID", "test-proj")
    monkeypatch.setenv("APPWRITE_API_KEY", "test-key")
    monkeypatch.setenv("APPWRITE_DB_ID", "test-db")
    from app.core.risk_state import RiskStateMachine
    return RiskStateMachine.compute_spread


def test_spread_count_is_binary_above_floor(spread):
    """原始設計：只要超過 noise_floor 就算一票，不論強弱。"""
    assert spread([1.0, 0.20, 0.15, 0.12, 0.20], 'count', 0.10) == pytest.approx(1.0)
    assert spread([1.0, 0.0, 0.0, 0.0, 0.0], 'count', 0.10) == pytest.approx(0.2)


def test_spread_effdim_matches_dimension_count_when_equal(spread):
    """有效維度數的定義性質：均等時等於維度數、集中時等於 1。"""
    assert spread([0.6] * 5, 'effdim') == pytest.approx(1.0)          # 5 個有效維度 / 5
    assert spread([1.0, 0, 0, 0, 0], 'effdim') == pytest.approx(0.2)  # 1 個 / 5


def test_spread_effdim_discounts_weak_residue(spread):
    """核心差異：一個強維度 + 四個殘影，不應等同五個真實訊號。

    此為 B6 前導評估 M06 的實況（參考答案 warning，系統判 blocked）。
    """
    residue = [1.0, 0.20, 0.15, 0.12, 0.20]
    assert spread(residue, 'count', 0.10) == pytest.approx(1.0)
    assert spread(residue, 'effdim') == pytest.approx(0.4991, abs=1e-3)
    assert spread(residue, 'effdim') < spread(residue, 'count', 0.10)


def test_spread_unknown_mode_falls_back_to_count(spread):
    """設定錯字不應改變行為，一律退回原始定義。"""
    assert spread([0.5, 0.5, 0, 0, 0], 'typo_mode', 0.10) == pytest.approx(0.4)


def test_spread_all_zero_is_safe(spread):
    assert spread([0.0] * 5, 'effdim') == 0.0
    assert spread([0.0] * 5, 'count', 0.10) == 0.0


# --- effdim 先套 noise_floor（整合進度確認 2026-08-23 第 1 項）---
# 參與比是尺度不變的：所有維度等比衰退時 spread 完全不動，於是 composite 被鎖在
# W_SPREAD × spread 的地板上，任何曾經多維分散的對話永遠回不到 safe。
# 修正：effdim 分支先以 noise_floor 過濾掉衰退殘影，再算參與比。

def test_spread_effdim_applies_noise_floor(spread):
    """effdim 應先濾掉低於 noise_floor 的維度，只算活躍訊號。

    [0.70, 0.30, 0.20, 0.10, 0.05] 全部 ×0.9 五次後，殘影會被 noise_floor
    清掉，只留真正活躍的訊號——避免衰退殘影把 spread 鎖在地板上。
    """
    # 全部 0.01（等於沒風險）不過 noise_floor=0.05 → active 空 → spread=0
    assert spread([0.01] * 5, 'effdim', 0.05) == 0.0
    # 只有一個 0.30 過門檻 → spread = 0.2（集中）
    assert spread([0.30, 0.01, 0.01, 0.01, 0.01], 'effdim', 0.05) == pytest.approx(0.2)
    # 兩個過門檻 → 參與比對應 2 個有效維度
    assert spread([0.30, 0.25, 0.01, 0.01, 0.01], 'effdim', 0.05) == pytest.approx(0.4, abs=0.01)


def test_spread_effdim_noise_floor_clears_decay_residue(spread):
    """五個維度全部只有 0.01 時，未套 noise_floor 的 spread=1.000 會判 warning；
    套了之後 spread=0，等同無風險——修正後同一組值在 count 模式下也是 safe。
    """
    tiny = [0.01] * 5
    # 不過門檻（noise_floor=0）時，參與比=1.000，會拿滿 W_SPREAD 權重
    assert spread(tiny, 'effdim', 0.0) == pytest.approx(1.0)
    # 過門檻後殘影被清掉
    assert spread(tiny, 'effdim', 0.05) == 0.0


def test_spread_effdim_preserves_real_signal(spread):
    """真實訊號幾乎不變：0.576 → 0.537，只有衰退殘影被清掉，不傷真實分散。"""
    real = [0.70, 0.30, 0.20, 0.10, 0.05]
    # 不過門檻（全部都算）
    without_floor = spread(real, 'effdim', 0.0)
    # 過 0.05 門檻（0.05 不 > 0.05，被濾掉）→ 只剩 4 個維度
    with_floor = spread(real, 'effdim', 0.05)
    # 真實訊號的 spread 只微降，不會被錯誤清零
    assert with_floor > 0
    assert abs(without_floor - with_floor) < 0.05  # 變動在 5% 以內
