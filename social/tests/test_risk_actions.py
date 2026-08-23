"""Pair-chat risk feedback / appeal proxies and blocked-notice behavior."""

from unittest.mock import MagicMock, patch

import pytest
import requests


@pytest.fixture
def risk_client(monkeypatch):
    monkeypatch.setattr("routers.risk_actions._RISK_SERVICE_URL", "http://risk.test")
    monkeypatch.setattr("routers.risk_actions._RISK_TIMEOUT", 1.0)
    return requests


def test_feedback_proxy_forwards_detail(risk_client):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return MagicMock(status_code=200, json=lambda: {"status": "ok"})

    with patch.object(risk_client, "post", side_effect=fake_post):
        from routers.risk_actions import submit_risk_feedback
        result = submit_risk_feedback(
            MagicMock(
                triggered_by_msg_id="msg-1",
                role="receiver",
                feedback="uncomfortable",
                detail="他連續傳了很多訊息，讓我有點壓力。",
                model_dump=lambda exclude_none: {
                    "triggered_by_msg_id": "msg-1",
                    "role": "receiver",
                    "feedback": "uncomfortable",
                    "detail": "他連續傳了很多訊息，讓我有點壓力。",
                },
            )
        )

    assert result == {"status": "ok"}
    assert captured["url"].endswith("/api/v1/risk/feedback")
    assert captured["json"]["detail"] == "他連續傳了很多訊息，讓我有點壓力。"


def test_appeal_proxy_forwards_text(risk_client):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return MagicMock(status_code=200, json=lambda: {"status": "ok"})

    with patch.object(risk_client, "post", side_effect=fake_post):
        from routers.risk_actions import submit_sender_appeal
        result = submit_sender_appeal(
            MagicMock(
                triggered_by_msg_id="msg-1",
                sender_id="user-1",
                appeal_text="那句話是誤會，我沒有那個意思。",
                model_dump=lambda: {
                    "triggered_by_msg_id": "msg-1",
                    "sender_id": "user-1",
                    "appeal_text": "那句話是誤會，我沒有那個意思。",
                },
            )
        )

    assert result == {"status": "ok"}
    assert captured["url"].endswith("/api/v1/risk/appeal")


def test_risk_gate_records_triggered_by_msg_id_from_command():
    from services.risk_policy_service import PairMessageRiskGate

    gate = PairMessageRiskGate(
        transport=lambda payload: {
            "risk_level": "warning",
            "intervention_command": {"triggered_by_msg_id": "real-msg-42"},
        }
    )
    decision = gate.evaluate(
        conversation_id="a_b",
        sender_id="a",
        receiver_id="b",
        content="嗨",
        idempotency_key="k1",
    )
    projection = decision.public_projection()

    assert decision.level == "warning"
    assert decision.delivery == "delivered"
    assert decision.triggered_by_msg_id == "real-msg-42"
    assert projection["triggered_by_msg_id"] == "real-msg-42"


def test_risk_gate_projection_omits_trigger_id_when_absent():
    from services.risk_policy_service import PairMessageRiskGate

    gate = PairMessageRiskGate(
        transport=lambda payload: {"risk_level": "safe"}
    )
    decision = gate.evaluate(
        conversation_id="a_b",
        sender_id="a",
        receiver_id="b",
        content="嗨",
        idempotency_key="k2",
    )
    projection = decision.public_projection()

    assert "triggered_by_msg_id" not in projection
    assert "sender_directive" not in projection
    assert "receiver_directive" not in projection


def test_risk_gate_passes_through_directives():
    from services.risk_policy_service import PairMessageRiskGate

    gate = PairMessageRiskGate(
        transport=lambda payload: {
            "risk_level": "restricted",
            "intervention_command": {
                "triggered_by_msg_id": "real-msg-7",
                "sender_directive": {
                    "action": "show_modal_warning",
                    "cooldown_seconds": 60,
                    "require_acknowledgment": True,
                    "mascot": "danger",
                    "allow_report_text": True,
                    "display_throttle_seconds": 120,
                    "content": {"title": "請暫停一下", "body": "系統偵測到…", "primary_risk_type": "coercion"},
                },
                "receiver_directive": {
                    "action": "show_safety_info_card",
                    "show_options": True,
                    "show_feedback_buttons": True,
                    "allow_report_text": True,
                    "mascot": "heart",
                    "display_throttle_seconds": 120,
                    "content": {"body": "已加強保護這段對話", "primary_risk_type": "any"},
                    "unexpected_field_should_drop": "x",
                },
            },
        }
    )
    decision = gate.evaluate(
        conversation_id="a_b",
        sender_id="a",
        receiver_id="b",
        content="哪裡",
        idempotency_key="k-dir",
    )
    projection = decision.public_projection()

    assert projection["triggered_by_msg_id"] == "real-msg-7"
    sender = projection["sender_directive"]
    assert sender["action"] == "show_modal_warning"
    assert sender["cooldown_seconds"] == 60
    assert sender["require_acknowledgment"] is True
    assert sender["mascot"] == "danger"
    assert sender["content"]["title"] == "請暫停一下"
    receiver = projection["receiver_directive"]
    assert receiver["action"] == "show_safety_info_card"
    assert receiver["show_options"] is True
    assert "unexpected_field_should_drop" not in receiver


def test_risk_gate_suppressed_directive_kept_for_frontend():
    from services.risk_policy_service import PairMessageRiskGate

    gate = PairMessageRiskGate(
        transport=lambda payload: {
            "risk_level": "warning",
            "intervention_command": {
                "triggered_by_msg_id": "real-msg-8",
                "sender_directive": {
                    "action": "suppressed",
                    "throttled": {"reason": "display_throttle", "window_seconds": 300},
                },
                "receiver_directive": {"action": "none", "content": None},
            },
        }
    )
    decision = gate.evaluate(
        conversation_id="a_b",
        sender_id="a",
        receiver_id="b",
        content="嗨",
        idempotency_key="k-thr",
    )
    projection = decision.public_projection()

    sender = projection["sender_directive"]
    assert sender["action"] == "suppressed"
    assert sender["throttled"]["reason"] == "display_throttle"
    # action == "none" 的 directive 不投影給前端
    assert "receiver_directive" not in projection


def test_risk_gate_projection_marks_sanction_exempted():
    from services.risk_policy_service import PairMessageRiskGate

    gate = PairMessageRiskGate(
        transport=lambda payload: {
            "risk_level": "blocked",
            "intervention_command": {
                "triggered_by_msg_id": "real-msg-9",
                "sender_directive": {
                    "action": "show_reflection_banner",
                    "sanction_exempted": True,
                    "content": {"body": "這段對話仍在觀察中", "primary_risk_type": "any"},
                },
                "receiver_directive": {
                    "action": "show_safety_info_card",
                    "sanction_exempted": True,
                    "content": {"body": "這段對話仍在加強保護中", "primary_risk_type": "any"},
                },
                "admin_directive": None,
            },
        }
    )
    decision = gate.evaluate(
        conversation_id="a_b",
        sender_id="a",
        receiver_id="b",
        content="嗨",
        idempotency_key="k-ex",
    )
    projection = decision.public_projection()

    assert projection.get("sanction_exempted") is True
    assert projection["sender_directive"]["sanction_exempted"] is True


def test_risk_gate_omits_trigger_id_for_unavailable():
    from services.risk_policy_service import PairMessageRiskGate

    gate = PairMessageRiskGate(
        transport=lambda payload: {"risk_level": "unavailable"}
    )
    decision = gate.evaluate(
        conversation_id="a_b",
        sender_id="a",
        receiver_id="b",
        content="嗨",
        idempotency_key="k3",
    )
    assert decision.level == "unavailable"
    assert "triggered_by_msg_id" not in decision.public_projection()


def test_blocked_message_writes_receiver_notice_card():
    from routers import public_chat
    from models import DirectChatRequest

    req = DirectChatRequest(
        user_id="a", contact_id="b", message="會被攔截", client_message_id="att-1",
    )
    risk_decision = MagicMock(
        may_persist=False,
        public_projection=lambda: {
            "level": "blocked",
            "ui_priority": "risk",
            "delivery": "blocked",
            "triggered_by_msg_id": "real-msg-9",
        },
    )
    with patch.object(public_chat, "find_accepted_match", return_value={"_id": "m1"}), \
         patch.object(public_chat.pair_message_risk_gate, "evaluate", return_value=risk_decision), \
         patch.object(public_chat, "save_system_message_once") as save_system, \
         patch("routers.public_chat.generate_room_id", return_value="a_b"):
        result = public_chat.direct_chat(req, MagicMock())

    assert result["is_blocked"] is True
    assert result["risk_assessment"]["triggered_by_msg_id"] == "real-msg-9"
    save_system.assert_called_once()
    kwargs = save_system.call_args
    assert kwargs.args[0] == "a_b"
    assert "系統攔截" in kwargs.args[1]
    assert kwargs.kwargs["message_type"] == "system"
    assert kwargs.kwargs["metadata"]["event_type"] == "blocked_notice"
    assert kwargs.kwargs["metadata"]["risk"]["level"] == "blocked"
