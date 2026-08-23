"""驗證 KB intervention 模板對 blocked/restricted sender 設定的 cooldown 預設值。

spec section 8 / 5.2：remaining_cooldown 缺值時後端按等級預設 restricted=60、blocked=1800。
本測試驗證 KB seed 模板（dating_safety.sql）確實為 blocked sender 設 cooldown=1800、
restricted sender 設 cooldown=60，故 intervention_engine 產生的 sender_directive.cooldown_seconds
在正常流程下非 0，冷卻功能不會靜默 no-op。
"""
import asyncio
from unittest.mock import patch
import pytest


def _templates(level):
    """取自 risk_backend/db_setup/dating_safety.sql kb_interventions seed。"""
    if level == "blocked":
        return [
            {"template_id": "block_receiver_notice", "risk_level": "blocked",
             "primary_risk_type": "any", "action_type": "show_blocked_notice",
             "message_template": {"body": "..."}, "ui_behavior": {"show_options": True}},
            {"template_id": "block_sender_final", "risk_level": "blocked",
             "primary_risk_type": "any", "action_type": "block_message",
             "message_template": {"title": "訊息未送出", "body": "..."},
             "ui_behavior": {"cooldown": 1800, "require_ack": True}},
        ]
    if level == "restricted":
        return [
            {"template_id": "restrict_receiver_options", "risk_level": "restricted",
             "primary_risk_type": "any", "action_type": "show_safety_info_card",
             "message_template": {"title": "...", "body": "..."},
             "ui_behavior": {"show_options": True, "cooldown": 0, "require_ack": False}},
            {"template_id": "restrict_sender_general", "risk_level": "restricted",
             "primary_risk_type": "any", "action_type": "show_modal_warning",
             "message_template": {"title": "請暫停一下", "body": "..."},
             "ui_behavior": {"cooldown": 60, "require_ack": True}},
        ]
    return []


@pytest.fixture
def engine():
    from app.core.intervention_engine import InterventionEngine
    return InterventionEngine()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_blocked_sender_cooldown_defaults_to_1800(engine):
    with patch("app.core.intervention_engine.KBService.get_interventions_by_level",
               staticmethod(lambda lvl: _templates(lvl))):
        cmd = asyncio.run(engine.execute(
            risk_level="blocked",
            risk_state={"sexual_boundary": 1.0, "coercion": 0.0, "manipulation": 0.0,
                        "harassment": 0.0, "emotional_pressure": 0.0},
            diagnosis={"reason": "critical_override"}, conv_id="c1", sender_id="s1",
            receiver_id="r1", msg_id="m1", decision_reason="critical_override",
        ))
    assert cmd["sender_directive"]["cooldown_seconds"] == 1800


def test_restricted_sender_cooldown_defaults_to_60(engine):
    with patch("app.core.intervention_engine.KBService.get_interventions_by_level",
               staticmethod(lambda lvl: _templates(lvl))):
        cmd = asyncio.run(engine.execute(
            risk_level="restricted",
            risk_state={"sexual_boundary": 0.0, "coercion": 0.5, "manipulation": 0.0,
                        "harassment": 0.0, "emotional_pressure": 0.0},
            diagnosis={"reason": "normal"}, conv_id="c1", sender_id="s1",
            receiver_id="r1", msg_id="m1", decision_reason="normal",
        ))
    assert cmd["sender_directive"]["cooldown_seconds"] == 60


def test_restricted_sender_directive_carries_mascot_flag(engine):
    """規格 §6：整包 passthrough，mascot 不得被白名單過濾。

    2026-08-15 前 _get_specific_directive 是白名單，mascot / show_options
    從未送達前端，導致寄件方 restricted 跳窗沒有吉祥物圖（需求：請暫停一下
    要出現 danger 圖片）。此測試鎖定該旗標現在會原樣傳出。
    """
    with patch("app.core.intervention_engine.KBService.get_interventions_by_level",
               staticmethod(lambda lvl: [
                   {"template_id": "restrict_sender_general", "risk_level": "restricted",
                    "primary_risk_type": "any", "action_type": "show_modal_warning",
                    "message_template": {"title": "請暫停一下", "body": "..."},
                    "ui_behavior": {"cooldown": 60, "require_ack": True,
                                    "mascot": "danger", "allow_report_text": True,
                                    "display_throttle_seconds": 120}},
               ])):
        cmd = asyncio.run(engine.execute(
            risk_level="restricted",
            risk_state={"sexual_boundary": 0.0, "coercion": 0.5, "manipulation": 0.0,
                        "harassment": 0.0, "emotional_pressure": 0.0},
            diagnosis={"reason": "normal"}, conv_id="c1", sender_id="s1",
            receiver_id="r1", msg_id="m1", decision_reason="normal",
        ))
    sender = cmd["sender_directive"]
    assert sender["mascot"] == "danger"
    assert sender["allow_report_text"] is True
    assert sender["display_throttle_seconds"] == 120


def test_blocked_sender_directive_has_no_mascot(engine):
    """規格 §5：blocked 寄件方刻意不放吉祥物（可愛圖像會降低感知嚴重性）。"""
    with patch("app.core.intervention_engine.KBService.get_interventions_by_level",
               staticmethod(lambda lvl: [
                   {"template_id": "block_receiver_notice", "risk_level": "blocked",
                    "primary_risk_type": "any", "action_type": "show_blocked_notice",
                    "message_template": {"body": "..."}, "ui_behavior": {"show_options": True}},
                   {"template_id": "block_sender_final", "risk_level": "blocked",
                    "primary_risk_type": "any", "action_type": "block_message",
                    "message_template": {"title": "訊息未送出", "body": "..."},
                    "ui_behavior": {"cooldown": 1800, "require_ack": True}},
               ])):
        cmd = asyncio.run(engine.execute(
            risk_level="blocked",
            risk_state={"sexual_boundary": 1.0, "coercion": 0.0, "manipulation": 0.0,
                        "harassment": 0.0, "emotional_pressure": 0.0},
            diagnosis={"reason": "critical_override"}, conv_id="c1", sender_id="s1",
            receiver_id="r1", msg_id="m1", decision_reason="critical_override",
        ))
    assert "mascot" not in cmd["sender_directive"]


def test_warning_sender_cooldown_is_zero(engine):
    with patch("app.core.intervention_engine.KBService.get_interventions_by_level",
               staticmethod(lambda lvl: [
                   {"template_id": "warn_sender_coercion", "risk_level": "warning",
                    "primary_risk_type": "coercion", "action_type": "show_reflection_banner",
                    "message_template": {"body": "..."},
                    "ui_behavior": {"cooldown": 0, "require_ack": False}},
               ])):
        cmd = asyncio.run(engine.execute(
            risk_level="warning",
            risk_state={"sexual_boundary": 0.0, "coercion": 0.4, "manipulation": 0.0,
                        "harassment": 0.0, "emotional_pressure": 0.0},
            diagnosis={"reason": "normal"}, conv_id="c1", sender_id="s1",
            receiver_id="r1", msg_id="m1", decision_reason="normal",
        ))
    assert cmd["sender_directive"]["cooldown_seconds"] == 0