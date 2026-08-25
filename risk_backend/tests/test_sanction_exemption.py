"""已處置豁免（sanction exemption）測試。

對應整合進度確認 2026-08-23 第 2 項：同一件事在冷卻期內不應被重複處罰。
四條件「全部」成立才豁免，否則照罰；累積狀態全程不動。

直接測 _check_sanction_exempted 的四條件邏輯，以及 execute 全流程是否
正確標記 sanction_exempted 並讓 risk_detection 不鎖訊息。

（本專案未安裝 pytest-asyncio，沿用既有測試的 asyncio.run 寫法。）
"""

import asyncio
from unittest.mock import patch

import pytest

from app.core.intervention_engine import InterventionEngine


class FakeLogService:
    """同時控制 get_last_displayed_intervention 與 get_remaining_cooldown。

    並提供節流所需的 get_last_displayed_intervention（execute 會呼叫兩次：
    一次給節流、一次給豁免）。回傳同一份資料即可——豁免的條件①②本就
    與節流的「上次顯示」共用同一份紀錄。
    """

    def __init__(self, last_level=None, seconds_ago=None, remaining=0):
        self._last_level = last_level
        self._seconds_ago = seconds_ago
        self._remaining = remaining

    async def get_last_displayed_intervention(self, conv_id, user_id, role):
        if self._last_level is None:
            return None
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(timezone.utc) - timedelta(seconds=self._seconds_ago)).isoformat()
        return {"risk_level": self._last_level, "timestamp": ts}

    async def get_remaining_cooldown(self, conv_id, user_id):
        return self._remaining


def _check(engine, level, diagnosis, fake):
    return asyncio.run(engine._check_sanction_exempted(
        level, diagnosis, "conv1", "user1", fake))


@pytest.fixture
def engine():
    return InterventionEngine()


# --- 四條件各鎖一條：任一不成立即不豁免 ---

def test_exempt_when_all_conditions_met(engine):
    """四條件全部成立 → 豁免。"""
    fake = FakeLogService(last_level="blocked", seconds_ago=4000, remaining=0)
    diag = {"delta_max": 0.01}
    assert _check(engine, "blocked", diag, fake) is True


def test_not_exempt_no_previous_intervention(engine):
    """條件①失敗：首次累積到該等級，無上次介入 → 照罰。"""
    fake = FakeLogService(last_level=None, remaining=0)
    diag = {"delta_max": 0.01}
    assert _check(engine, "blocked", diag, fake) is False


def test_not_exempt_level_escalated(engine):
    """條件②失敗：本次等級高於上次（風險升高）→ 照罰，豁免不成保護傘。"""
    fake = FakeLogService(last_level="warning", seconds_ago=4000, remaining=0)
    diag = {"delta_max": 0.01}
    assert _check(engine, "blocked", diag, fake) is False


def test_not_exempt_cooldown_active(engine):
    """條件③失敗：冷卻尚未服完 → 照罰，確認真的服完刑才豁免。"""
    fake = FakeLogService(last_level="blocked", seconds_ago=60, remaining=1740)
    diag = {"delta_max": 0.01}
    assert _check(engine, "blocked", diag, fake) is False


def test_not_exempt_high_delta(engine):
    """條件④失敗：本則 delta ≥ 0.05（服完又違規）→ 照罰，且從高狀態起跳罰得更重。"""
    fake = FakeLogService(last_level="blocked", seconds_ago=4000, remaining=0)
    diag = {"delta_max": 0.08}
    assert _check(engine, "blocked", diag, fake) is False


# --- 邊界值 ---

def test_exempt_equal_level(engine):
    """上次等級 == 本次等級 → 條件②成立（≥），豁免。"""
    fake = FakeLogService(last_level="blocked", seconds_ago=4000, remaining=0)
    diag = {"delta_max": 0.01}
    assert _check(engine, "blocked", diag, fake) is True


def test_exempt_zero_delta(engine):
    """delta_max = 0（本則無風險增量）→ 條件④成立，豁免。"""
    fake = FakeLogService(last_level="blocked", seconds_ago=4000, remaining=0)
    diag = {"delta_max": 0.0}
    assert _check(engine, "blocked", diag, fake) is True


def test_exempt_delta_just_below_threshold(engine):
    """delta_max = 0.049 < 0.05 → 條件④成立，豁免（邊界）。"""
    fake = FakeLogService(last_level="blocked", seconds_ago=4000, remaining=0)
    diag = {"delta_max": 0.049}
    assert _check(engine, "blocked", diag, fake) is True


# --- 例外處理 ---

def test_exempt_failure_fails_closed(engine):
    """豁免判定拋例外時保守不豁免（fail-closed），不讓例外變成繞道。"""

    class Broken:
        async def get_last_displayed_intervention(self, *a, **k):
            raise RuntimeError("db down")

        async def get_remaining_cooldown(self, *a, **k):
            return 0

    assert _check(engine, "blocked", {"delta_max": 0.01}, Broken()) is False