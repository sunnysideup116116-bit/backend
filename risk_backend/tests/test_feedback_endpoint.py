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


# --- 收件方文字回報（/report 端點，整合進度確認 2026-08-23 第 4 項）---
# 與 /appeal 對稱但角色相反：一個是被警告者自辯、一個是被保護者陳述，
# 稽核意義相反，混用會使後台無從分辨。此內容不進入任何演算法。

def test_save_receiver_report_success(chat_service):
    """Normal path: find row, update receiver_report_text, return ok."""
    mock_doc = MagicMock()
    mock_doc.id = "log_123"
    mock_doc.data = {"receiver_id": "rx_456"}
    mock_response = MagicMock()
    mock_response.documents = [mock_doc]
    chat_service.db.list_documents.return_value = mock_response

    result = asyncio.run(chat_service.save_receiver_report(
        msg_id="msg_abc", receiver_id="rx_456", report_text="這則訊息讓我感到不舒服"
    ))

    assert result["ok"] is True
    assert result["error"] is None
    call_args = chat_service.db.update_document.call_args
    assert call_args[0][2] == "log_123"
    assert call_args[0][3] == {"receiver_report_text": "這則訊息讓我感到不舒服"}


def test_save_receiver_report_wrong_receiver_rejected(chat_service):
    """僅允許該則訊息的收件方本人回報——防冒名。"""
    mock_doc = MagicMock()
    mock_doc.id = "log_123"
    mock_doc.data = {"receiver_id": "rx_456"}
    mock_response = MagicMock()
    mock_response.documents = [mock_doc]
    chat_service.db.list_documents.return_value = mock_response

    result = asyncio.run(chat_service.save_receiver_report(
        msg_id="msg_abc", receiver_id="someone_else", report_text="偽造的回報"
    ))

    assert result["ok"] is False
    assert result["error"] == "receiver_mismatch"
    chat_service.db.update_document.assert_not_called()


def test_save_receiver_report_msg_not_found(chat_service):
    """找不到介入紀錄 → not_found，不嘗試寫入。"""
    mock_response = MagicMock()
    mock_response.documents = []
    chat_service.db.list_documents.return_value = mock_response

    result = asyncio.run(chat_service.save_receiver_report(
        msg_id="msg_xyz", receiver_id="rx_456", report_text="回報"
    ))

    assert result["ok"] is False
    assert result["error"] == "not_found"
    chat_service.db.update_document.assert_not_called()


def test_save_receiver_report_attribute_missing_returns_clear_error(chat_service):
    """Appwrite 尚未建立 receiver_report_text 屬性時回傳明確錯誤而非靜默失敗。"""
    mock_doc = MagicMock()
    mock_doc.id = "log_123"
    mock_doc.data = {"receiver_id": "rx_456"}
    mock_response = MagicMock()
    mock_response.documents = [mock_doc]
    chat_service.db.list_documents.return_value = mock_response
    chat_service.db.update_document.side_effect = Exception("Unknown attribute receiver_report_text")

    result = asyncio.run(chat_service.save_receiver_report(
        msg_id="msg_abc", receiver_id="rx_456", report_text="回報"
    ))

    assert result["ok"] is False
    assert result["error"] == "attribute_missing"
