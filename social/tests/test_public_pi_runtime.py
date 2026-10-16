"""Public Ayue always routes authenticated owners to Pi."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from routers import public_chat, system
from services.ayue_agent import public_runtime
from services.ayue_agent.pi.reply import failed_reply


def test_public_runtime_always_calls_pi(monkeypatch):
    expected = object()
    run_pi = MagicMock(return_value=expected)
    monkeypatch.setattr("services.ayue_agent.pi.public_turn.run_pi_public_turn", run_pi)
    context = SimpleNamespace(user_id="owner", external_calendar_authorized=True)

    assert public_runtime.run_public_agent_turn(context, debug_enabled=True) is expected
    run_pi.assert_called_once_with(context, debug_enabled=True)


def test_public_runtime_rejects_unverified_owner(monkeypatch):
    run_pi = MagicMock()
    monkeypatch.setattr("services.ayue_agent.pi.public_turn.run_pi_public_turn", run_pi)

    with pytest.raises(PermissionError, match="pi_authenticated_owner_required"):
        public_runtime.run_public_agent_turn(
            SimpleNamespace(user_id="owner", external_calendar_authorized=False),
        )
    run_pi.assert_not_called()


def test_http_gate_rejects_before_persisting_owner_message(monkeypatch):
    save = MagicMock()
    monkeypatch.setattr(public_chat, "save_message", save)

    with pytest.raises(HTTPException) as caught:
        public_chat._require_public_pi(SimpleNamespace(headers={}), False)
    assert caught.value.status_code == 401
    save.assert_not_called()


def test_http_gate_reports_missing_pi_without_fallback(monkeypatch):
    monkeypatch.setattr("services.ayue_agent.pi.settings.pi_available", lambda: False)

    with pytest.raises(HTTPException) as caught:
        public_chat._require_public_pi(SimpleNamespace(headers={}), True)
    assert caught.value.status_code == 503
    assert "沒有執行任何操作" in caught.value.detail


def test_experiment_runtime_api_is_removed():
    paths = {route.path for route in system.router.routes}
    assert "/api/settings/agent-runtime" not in paths


def test_relationship_tool_failure_does_not_present_contact_names_as_an_answer():
    observations = [
        {"tool": "relationship.list_accepted_contacts", "status": "ok", "result": {
            "contacts": [{"display_name": "小安"}],
        }},
        {"tool": "relationship.get_contact_evidence", "status": "failed",
         "error_code": "executor_args_invalid"},
        {"tool": "profile.get_self_summary", "status": "ok", "result": {
            "summary": "喜歡戶外活動",
        }},
    ]

    reply = failed_reply("pi_tool_schema_invalid", observations=observations)

    assert "後續查詢未能完成" in reply
    assert "重點是" not in reply
    assert "小安" not in reply
    assert "喜歡戶外活動" not in reply


def test_relationship_view_fallback_does_not_flatten_private_views():
    observations = [{"tool": "relationship.get_my_views", "status": "ok", "result": {
        "contacts": [{"display_name": "小安", "groups": [{
            "topic": "相處", "views": [{"text": "聊天有點冷"}],
        }]}],
    }}]

    reply = failed_reply("pi_reply_invalid", observations=observations)

    assert "沒能可靠完成回答" in reply
    assert "重點是" not in reply
    assert "小安" not in reply
    assert "聊天有點冷" not in reply
