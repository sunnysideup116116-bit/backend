"""get_remaining_cooldown 從 intervention_logs 算剩餘秒數（pure mock）。"""
import asyncio
from datetime import datetime, timedelta, timezone
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

def _doc(cooldown, seconds_ago):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    d = MagicMock()
    d.data = {"cooldown_seconds": cooldown, "timestamp": ts}
    return d

def test_remaining_cooldown_active(chat_service):
    resp = MagicMock(); resp.documents = [_doc(60, 20)]
    chat_service.db.list_documents.return_value = resp
    rem = asyncio.run(chat_service.get_remaining_cooldown("c1", "s1"))
    assert 30 <= rem <= 40  # 60 - ~20s，容忍計時誤差

def test_remaining_cooldown_expired(chat_service):
    resp = MagicMock(); resp.documents = [_doc(60, 120)]
    chat_service.db.list_documents.return_value = resp
    assert asyncio.run(chat_service.get_remaining_cooldown("c1", "s1")) == 0

def test_remaining_cooldown_no_log(chat_service):
    resp = MagicMock(); resp.documents = []
    chat_service.db.list_documents.return_value = resp
    assert asyncio.run(chat_service.get_remaining_cooldown("c1", "s1")) == 0