"""Voice unit tests use synthetic identity/quota stores, never live accounts."""
import os
import sys
from pathlib import Path

import pytest

os.environ['AYUE_SKIP_DOTENV'] = '1'
os.environ['PYTHON_DOTENV_DISABLED'] = '1'
os.environ['VOICE_MEMORY_ENABLED'] = 'off'
os.environ['VOICE_APP_TASKS_ENABLED'] = 'off'
os.environ['APPWRITE_PROJECT_ID'] = ''
os.environ['APPWRITE_API_KEY'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'social'))


@pytest.fixture(autouse=True)
def synthetic_admission(monkeypatch, tmp_path):
    from agent_quota import service as quota_module
    from agent_quota.tests.test_quota import MemoryStore
    from importlib import import_module
    router = import_module('app_voice_assistant.router')
    quota = quota_module.QuotaService(MemoryStore(), tmp_path / 'quota.sqlite')
    monkeypatch.setattr(quota_module, 'service', quota)
    monkeypatch.setattr(router, 'require_quota', quota.check)
    # Legacy socket fixtures predate JWT admission. New owner-scoped endpoints
    # still exercise verify_owner with explicit injected JWT authenticators.
    monkeypatch.setattr(router, 'owner_from_request', lambda request: 'test-user-id')
