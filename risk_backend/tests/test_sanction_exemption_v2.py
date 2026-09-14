"""Sanction exemption boundary and state-notice behavior."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.core.intervention_engine import EXEMPT_DELTA_EPSILON, InterventionEngine


OPTIONS = [
    {"action": "block_user", "label": "封鎖"},
    {"action": "report_user", "label": "檢舉"},
    {"action": "leave_conversation", "label": "停止對話"},
]
BASE = [
    {
        "template_id": "block_sender_final",
        "primary_risk_type": "any",
        "action_type": "block_message",
        "message_template": {"title": "未送出", "body": "…"},
        "ui_behavior": {"cooldown": 1800, "require_ack": True},
    },
    {
        "template_id": "block_receiver_notice",
        "primary_risk_type": "any",
        "action_type": "show_blocked_notice",
        "message_template": {"body": "…"},
        "ui_behavior": {"show_options": True},
    },
]
STATE = [
    {
        "template_id": "sender_state_notice",
        "primary_risk_type": "any",
        "action_type": "show_reflection_banner",
        "message_template": {"body": "仍在觀察中"},
        "ui_behavior": {"display_throttle_seconds": 300},
    },
    {
        "template_id": "receiver_state_notice",
        "primary_risk_type": "any",
        "action_type": "show_safety_info_card",
        "message_template": {"body": "仍在加強保護中"},
        "ui_behavior": {
            "show_options": True,
            "show_feedback_buttons": False,
            "allow_report_text": True,
            "display_throttle_seconds": 300,
        },
        "action_options": OPTIONS,
    },
]


class Log:
    async def get_last_displayed_intervention(self, *_):
        return {
            "risk_level": "blocked",
            "timestamp": (datetime.now(timezone.utc) - timedelta(seconds=1800)).isoformat(),
        }

    async def get_remaining_cooldown(self, *_):
        return 0


def _run(delta=None):
    with patch(
        "app.core.intervention_engine.KBService.get_interventions_by_level",
        side_effect=lambda level: STATE if level == "exempt" else BASE,
    ):
        return asyncio.run(InterventionEngine().execute(
            risk_level="blocked",
            risk_state={
                "sexual_boundary": 0.9,
                "coercion": 0.0,
                "manipulation": 0.0,
                "harassment": 0.0,
                "emotional_pressure": 0.0,
            },
            diagnosis={},
            conv_id="conversation",
            sender_id="alice",
            receiver_id="bob",
            msg_id="message",
            decision_reason="normal",
            chat_log_service=Log(),
            message_delta=delta,
        ))


def test_minimum_nonzero_delta_is_exempt_and_releases_sanction():
    command = _run({"harassment": 0.055})
    assert EXEMPT_DELTA_EPSILON == 0.10
    assert command["sanction_exempted"] is True
    assert command["admin_directive"] is None
    assert command["sender_directive"]["action"] == "show_reflection_banner"
    assert command["sender_directive"]["cooldown_seconds"] == 0
    assert command["sender_directive"]["require_acknowledgment"] is False


def test_delta_above_epsilon_is_not_exempt():
    command = _run({"harassment": 0.11})
    assert command["sanction_exempted"] is False
    assert command["sender_directive"]["action"] == "block_message"


def test_exempt_receiver_keeps_actions_but_not_feedback_buttons():
    receiver = _run({"harassment": 0.055})["receiver_directive"]
    assert receiver["action_options"] == OPTIONS
    assert receiver["show_feedback_buttons"] is False
    assert receiver["display_throttle_seconds"] == 300
    assert receiver["target_user_id"] == "bob"


def test_missing_delta_keeps_legacy_sanction():
    command = _run()
    assert command["sanction_exempted"] is False
    assert command["sender_directive"]["cooldown_seconds"] == 1800


def test_missing_exempt_kb_uses_neutral_state_notice_not_original_sanction_copy():
    original = {
        "action": "show_blocked_notice",
        "cooldown_seconds": 1800,
        "require_acknowledgment": True,
        "content": {
            "body": "原本針對本則違規的封鎖文案",
            "primary_risk_type": "harassment",
        },
        "action_options": [{"action": "block_user", "label": "封鎖"}],
    }
    with patch(
        "app.core.intervention_engine.KBService.get_interventions_by_level",
        return_value=[],
    ):
        result = InterventionEngine()._apply_state_notice(
            original,
            "receiver",
            "blocked",
        )

    assert result["action"] == "show_safety_info_card"
    assert result["cooldown_seconds"] == 0
    assert result["require_acknowledgment"] is False
    assert result["sanction_exempted"] is True
    assert "原本" not in result["content"]["body"]
    assert result["action_options"] == [
        {"action": "block_user", "label": "封鎖"}
    ]
