import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def service():
    from app.services.chat_log_service import ChatLogService
    value = ChatLogService.__new__(ChatLogService)
    value.db_id = 'test'
    value.db = MagicMock()
    return value


@pytest.mark.parametrize('seconds,age,state,remaining', [
    (60, 20, 'active', 40), (60, 59.5, 'active', 1),
    (60, 60, 'clear', 0), (60, 70, 'clear', 0), (0, 0, 'clear', 0),
])
def test_cooldown_deadline_is_authoritative_at_boundaries(service, monkeypatch, seconds, age, state, remaining):
    import app.services.chat_log_service as module
    now = datetime(2026, 9, 21, 7, tzinfo=timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return now
    monkeypatch.setattr(module, 'datetime', Clock)
    service.db.list_documents.return_value = SimpleNamespace(documents=[{
        'cooldown_seconds': seconds, 'timestamp': (now - timedelta(seconds=age)).isoformat()}])
    result = asyncio.run(service.get_cooldown_status('conversation', 'owner'))
    assert result['state'] == state
    assert result['remaining_seconds'] == remaining
    assert result['server_time'] == now.isoformat()


def test_lookup_failure_is_unknown_not_zero(service):
    service.db.list_documents.side_effect = RuntimeError('storage unavailable')
    result = asyncio.run(service.get_cooldown_status('conversation', 'owner'))
    assert result['state'] == 'unknown'
    assert result['remaining_seconds'] is None
    assert result['until'] is None


def test_missing_timestamp_is_unknown_but_empty_history_is_clear(service):
    service.db.list_documents.return_value = SimpleNamespace(documents=[{'cooldown_seconds': 60}])
    assert asyncio.run(service.get_cooldown_status('conversation', 'owner'))['state'] == 'unknown'
    service.db.list_documents.return_value = SimpleNamespace(documents=[])
    assert asyncio.run(service.get_cooldown_status('conversation', 'owner'))['state'] == 'clear'
