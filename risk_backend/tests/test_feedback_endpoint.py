"""Feedback endpoint service tests (pure mock, no Appwrite)."""

import asyncio
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


def test_update_feedback_success(chat_service):
    """Normal path: find row, update it, return True."""
    mock_doc = MagicMock()
    mock_doc.id = "log_123"
    mock_response = MagicMock()
    mock_response.documents = [mock_doc]
    chat_service.db.list_documents.return_value = mock_response
    chat_service.db.update_document.return_value = {"$id": "log_123"}

    ok = asyncio.run(chat_service.update_intervention_feedback(
        msg_id="msg_abc", role="receiver", feedback="comfortable"
    ))

    assert ok is True
    call_args = chat_service.db.update_document.call_args
    assert call_args[0][2] == "log_123"
    assert call_args[0][3] == {"receiver_feedback": "comfortable"}


def test_update_feedback_with_detail_writes_detail_column(chat_service):
    """Detail text is persisted to the role-specific detail column."""
    mock_doc = MagicMock()
    mock_doc.id = "log_123"
    mock_response = MagicMock()
    mock_response.documents = [mock_doc]
    chat_service.db.list_documents.return_value = mock_response

    ok = asyncio.run(chat_service.update_intervention_feedback(
        msg_id="msg_abc", role="receiver", feedback="uncomfortable",
        detail="他連續傳了很多訊息，讓我有點壓力。",
    ))

    assert ok is True
    call_args = chat_service.db.update_document.call_args
    assert call_args[0][3] == {
        "receiver_feedback": "uncomfortable",
        "receiver_feedback_detail": "他連續傳了很多訊息，讓我有點壓力。",
    }


def test_update_feedback_blank_detail_skips_detail_column(chat_service):
    """Whitespace-only detail must not create an empty detail column."""
    mock_doc = MagicMock()
    mock_doc.id = "log_123"
    mock_response = MagicMock()
    mock_response.documents = [mock_doc]
    chat_service.db.list_documents.return_value = mock_response

    ok = asyncio.run(chat_service.update_intervention_feedback(
        msg_id="msg_abc", role="sender", feedback="comfortable", detail="   ",
    ))

    assert ok is True
    call_args = chat_service.db.update_document.call_args
    assert call_args[0][3] == {"sender_feedback": "comfortable"}


def test_update_feedback_invalid_role(chat_service):
    """Invalid role should return False without querying DB."""
    ok = asyncio.run(chat_service.update_intervention_feedback(
        msg_id="msg_abc", role="admin", feedback="comfortable"
    ))

    assert ok is False
    chat_service.db.list_documents.assert_not_called()


def test_update_feedback_invalid_value(chat_service):
    """Invalid feedback value should return False."""
    ok = asyncio.run(chat_service.update_intervention_feedback(
        msg_id="msg_abc", role="receiver", feedback="happy"
    ))

    assert ok is False


def test_update_feedback_msg_not_found(chat_service):
    """Missing intervention log should return False."""
    mock_response = MagicMock()
    mock_response.documents = []
    chat_service.db.list_documents.return_value = mock_response

    ok = asyncio.run(chat_service.update_intervention_feedback(
        msg_id="msg_xyz", role="receiver", feedback="uncomfortable"
    ))

    assert ok is False
    chat_service.db.update_document.assert_not_called()
