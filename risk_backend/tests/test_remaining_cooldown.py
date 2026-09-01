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


def test_blocked_30_minute_boundary_uses_a_fake_clock(chat_service, monkeypatch):
    fixed_now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr("app.services.chat_log_service.datetime", FixedDateTime)
    active = MagicMock()
    active.data = {
        "cooldown_seconds": 1800,
        "timestamp": (fixed_now - timedelta(seconds=1799)).isoformat(),
    }
    expired = MagicMock()
    expired.data = {
        "cooldown_seconds": 1800,
        "timestamp": (fixed_now - timedelta(seconds=1800)).isoformat(),
    }

    response = MagicMock()
    response.documents = [active]
    chat_service.db.list_documents.return_value = response
    assert asyncio.run(chat_service.get_remaining_cooldown("c1", "s1")) == 1

    response.documents = [expired]
    assert asyncio.run(chat_service.get_remaining_cooldown("c1", "s1")) == 0
