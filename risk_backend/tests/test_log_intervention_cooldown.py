"""log_intervention 應儲存 cooldown_seconds（pure mock）。"""
import asyncio, json
from unittest.mock import MagicMock
import pytest

@pytest.fixture
def chat_service(monkeypatch):
    monkeypatch.setenv("APPWRITE_ENDPOINT", "http://test/v1")
    monkeypatch.setenv("APPWRITE_PROJECT_ID", "test-proj")
    monkeypatch.setenv("APPWRITE_API_KEY", "test-key")
    monkeypatch.setenv("APPWRITE_DB_ID", "test-db")
    from app.services.chat_log_service import ChatLogService
    svc = ChatLogService()
    svc.db = MagicMock()
    return svc

def _state():
    from app.models.schemas import RiskState
    return RiskState()

def test_log_intervention_stores_cooldown(chat_service):
    chat_service.db.create_document.return_value = {"$id": "log_1"}
    ok = asyncio.run(chat_service.log_intervention(
        conversation_id="c1", triggered_by_msg_id="m1", sender_id="s1", receiver_id="r1",
        risk_level="restricted", risk_state=_state(), diagnosis={"composite_score": 0.5,
        "max_score": 0.4, "spread_score": 0.5, "trend_score": 0.0},
        decision_reason="normal", primary_risk="coercion",
        sender_action="show_modal_warning", receiver_action="show_safety_info_card",
        cooldown_seconds=60,
    ))
    assert ok is True
    data = chat_service.db.create_document.call_args[0][3]
    assert data["cooldown_seconds"] == 60

def test_log_intervention_cooldown_defaults_zero(chat_service):
    chat_service.db.create_document.return_value = {"$id": "log_2"}
    asyncio.run(chat_service.log_intervention(
        conversation_id="c1", triggered_by_msg_id="m2", sender_id="s1", receiver_id="r1",
        risk_level="warning", risk_state=_state(), diagnosis={}, decision_reason="normal",
        primary_risk="emotional_pressure", sender_action="show_reflection_banner",
        receiver_action="none",
    ))
    data = chat_service.db.create_document.call_args[0][3]
    assert data["cooldown_seconds"] == 0


def test_receiver_feedback_statuses_are_scoped_and_bounded(chat_service):
    matching = MagicMock(data={
        "triggered_by_msg_id": "m1",
        "conversation_id": "c1",
        "receiver_id": "r1",
        "receiver_feedback": "comfortable",
    })
    wrong_receiver = MagicMock(data={
        "triggered_by_msg_id": "m2",
        "conversation_id": "c1",
        "receiver_id": "r2",
        "receiver_feedback": "uncomfortable",
    })
    pending = MagicMock(data={
        "triggered_by_msg_id": "m3",
        "conversation_id": "c1",
        "receiver_id": "r1",
        "receiver_feedback": None,
    })
    chat_service.db.list_documents.return_value = MagicMock(
        documents=[matching, wrong_receiver, pending]
    )

    result = asyncio.run(chat_service.get_receiver_feedback_statuses(
        "c1", "r1", ["m1", "m2", "m3", "m1"]
    ))

    assert result == {"m1": "comfortable"}
    queries = chat_service.db.list_documents.call_args.kwargs["queries"]
    assert any("m1" in query and "m3" in query for query in queries)
