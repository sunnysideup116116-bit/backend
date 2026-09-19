import copy
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_quota import service as module
from agent_quota.service import QuotaService, QuotaError, StoreError, key, task_scope, tracked_ollama


class MemoryStore:
    def __init__(self):
        self.rows = {('agent_quota_settings', 'default'): {'initial_tokens': 100000},
                     ('agent_quota_codes', key('Sunnyfan116')): {'code': 'Sunnyfan116', 'enabled': True}}
        self.lock = threading.RLock()
        self.unavailable = False

    def get(self, collection, document, tx=None):
        if self.unavailable:
            raise StoreError(503)
        return copy.deepcopy(self.rows.get((collection, document)))

    def create(self, collection, document, data, tx=None):
        with self.lock:
            if (collection, document) in self.rows:
                raise StoreError(409)
            self.rows[collection, document] = dict(data)
            return dict(data)

    def update(self, collection, document, data, tx=None):
        self.rows[collection, document].update(data)

    @contextmanager
    def transaction(self):
        with self.lock:
            backup = copy.deepcopy(self.rows)
            try:
                yield 'tx'
            except BaseException:
                self.rows = backup
                raise


@pytest.fixture
def quota(tmp_path, monkeypatch):
    q = QuotaService(MemoryStore(), tmp_path / 'outbox.sqlite')
    monkeypatch.setattr(module, 'service', q)
    return q


def test_defaults_independent_manual_fields_and_percent(quota):
    assert quota.status('old')['remaining_tokens'] == 100000
    quota.store.update('agent_quota_settings', 'default', {'initial_tokens': 200000})
    assert quota.status('new')['remaining_tokens'] == 200000
    assert quota.status('old')['remaining_tokens'] == 100000
    quota.store.update('agent_quotas', 'old', {'max_tokens': 200000, 'remaining_tokens': 20000})
    assert quota.status('old')['remaining_percent'] == 10
    quota.store.update('agent_quotas', 'old', {'max_tokens': 0})
    assert quota.status('old')['remaining_percent'] == 0


def test_overrun_no_debt_cumulative_usage_and_replay(quota):
    quota.account('owner')
    quota.store.update('agent_quotas', 'owner', {'remaining_tokens': 100})
    quota.record('owner', 'matching', 'task', 'call', 600, 400)
    assert quota.status('owner')['remaining_tokens'] == 0
    with pytest.raises(QuotaError, match='額度不足'):
        quota.check('owner')
    quota.store.update('agent_quotas', 'owner', {'remaining_tokens': 10000})
    quota.record('owner', 'matching', 'task', 'call', 600, 400)
    assert quota.status('owner')['remaining_tokens'] == 10000
    assert quota.status('owner')['used_tokens'] == 1000


def test_concurrent_calls_and_duplicate_snapshots(quota):
    quota.account('owner')
    def work(i):
        quota.record('owner', ('matching', 'private', 'voice')[i % 3], 'task', str(i), 10, 5)
        quota.record('owner', ('matching', 'private', 'voice')[i % 3], 'task', str(i), 10, 5)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(30)))
    status = quota.status('owner')
    assert status['used_tokens'] == 450
    assert status['remaining_tokens'] == 99550
    assert [status[f + '_tokens'] for f in module.FEATURES] == [150, 150, 150]


def test_cumulative_partial_failure_out_of_order(quota):
    quota.record('owner', 'private', 'task', 'call', 100, 10)
    quota.record('owner', 'private', 'task', 'call', 100, 40)
    quota.record('owner', 'private', 'task', 'call', 100, 5)
    assert quota.status('owner')['used_tokens'] == 140


def test_permanent_code_once_case_sensitive_and_revocation(quota):
    assert quota.redeem('owner', 'sunnyfan116') == 'invalid_code'
    assert quota.redeem('owner', 'Sunnyfan116') == 'redeemed'
    quota.record('owner', 'voice', 'session', 'call', 200000, 50)
    assert quota.status('owner')['remaining_tokens'] == 100000
    assert quota.status('owner')['used_tokens'] == 200050
    quota.store.update('agent_quotas', 'owner', {'infinity': False})
    assert quota.redeem('owner', 'Sunnyfan116') == 'redeemed'
    assert quota.status('owner')['infinity'] is True
    assert quota.status('owner')['remaining_tokens'] == 100000
    assert quota.status('owner')['used_tokens'] == 200050
    assert sum(c == 'agent_quota_redemptions' for c, _ in quota.store.rows) == 1
    quota.store.update('agent_quota_codes', key('Sunnyfan116'), {'enabled': False})
    assert quota.redeem('other', 'Sunnyfan116') == 'invalid_code'
    quota.store.update('agent_quotas', 'owner', {'infinity': False})
    assert quota.redeem('owner', 'Sunnyfan116') == 'invalid_code'
    assert quota.status('owner')['infinity'] is False


def test_outage_durable_replay_and_fail_closed(quota):
    quota.account('owner')
    quota.store.unavailable = True
    quota.record('owner', 'voice', 'task', 'call', 30, 20)
    with pytest.raises(QuotaError):
        quota.check('owner')
    recovered = QuotaService(quota.store, quota.outbox)
    quota.store.unavailable = False
    assert recovered.status('owner')['used_tokens'] == 50
    assert recovered.status('owner')['used_tokens'] == 50


def test_scope_exclusions_nested_and_cancelled_stream(quota):
    # No foreground scope: risk/embedding/background do not acquire a charge.
    module.record_usage('background', 999, 999)
    assert not any(c == 'agent_quota_usage' for c, _ in quota.store.rows)
    with task_scope('owner', 'voice', 'session'):
        tracked_ollama({'prompt_eval_count': 10, 'eval_count': 5})
        with task_scope('owner', 'private', 'child'):
            stream = tracked_ollama(iter([{'prompt_eval_count': 20, 'eval_count': 7}]))
            next(stream)
            stream.close()
        tracked_ollama({'prompt_eval_count': 2, 'eval_count': 1})
    status = quota.status('owner')
    assert status['voice_tokens'] == 18 and status['private_tokens'] == 27
    assert module.SCOPE.get() is None


def test_admitted_task_continues_at_zero_next_task_blocked(quota):
    quota.account('owner')
    quota.store.update('agent_quotas', 'owner', {'remaining_tokens': 1})
    quota.check('owner')
    with task_scope('owner', 'voice', 'session'):
        module.record_usage('turn1', 100, 100)
        module.record_usage('turn2', 100, 100)
        with pytest.raises(QuotaError):
            quota.check('owner')  # Child operation is a new task.
    assert quota.status('owner')['used_tokens'] == 400


def test_owner_auth_and_api_errors(quota, monkeypatch):
    from agent_quota import api
    from services.appwrite_identity_service import AppwriteIdentityError
    monkeypatch.setattr(api, 'service', quota)
    def authenticate(value):
        if value != 'Bearer good':
            raise AppwriteIdentityError('appwrite_jwt_required', 401)
        return 'owner'
    monkeypatch.setattr(api, 'authenticate_owner', authenticate)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    assert client.get('/api/agent-quota').status_code == 401
    response = client.get('/api/agent-quota?user_id=other', headers={'Authorization': 'Bearer good'})
    assert response.status_code == 200
    assert quota.store.get('agent_quotas', 'other') is None
    quota.store.unavailable = True
    assert client.get('/api/agent-quota', headers={'Authorization': 'Bearer good'}).status_code == 503


def test_http_admission_stops_before_stream_and_preserves_worker_scope(quota, monkeypatch):
    import json
    from contextvars import copy_context
    from fastapi import Request
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel
    from agent_quota import api
    monkeypatch.setattr(api, 'service', quota)
    monkeypatch.setattr(api, 'authenticate_owner', lambda _authorization: 'owner')
    class Body(BaseModel):
        user_id: str
        contact_id: str = 'ai_assistant'
    @api.budgeted('matching')
    def chat(req: Body, request: Request):
        result = []
        def worker():
            module.record_usage('call', 100, 50)
            result.append(module.SCOPE.get()[1])
            with module.unmetered():
                module.record_usage('background', 5000, 5000)
        thread = threading.Thread(target=copy_context().run, args=(worker,))
        thread.start()
        thread.join()
        return {'feature': result[0]}
    app = FastAPI()
    app.post('/chat')(chat)
    client = TestClient(app)
    quota.account('owner')
    quota.store.update('agent_quotas', 'owner', {'remaining_tokens': 1})
    assert client.post('/chat', json={'user_id': 'other'}).status_code == 403
    assert client.post('/chat', json={'user_id': 'owner'}).json() == {'feature': 'matching'}
    response = client.post('/chat', json={'user_id': 'owner'})
    assert response.status_code == 403
    assert response.json()['detail']['code'] == 'agent_quota_exhausted'
    assert quota.status('owner')['used_tokens'] == 150


def test_voice_session_is_gated_once_and_auth_is_bound(quota, monkeypatch):
    import asyncio
    from dataclasses import replace
    from agent_quota import api
    from app_voice_assistant.router import AppVoiceRuntime, AppVoiceSessionRequest, create_router
    from app_voice_assistant.settings import AppVoiceSettings
    monkeypatch.setattr(api, 'service', quota)
    settings = AppVoiceSettings.from_env({'VOICE_APP_ENABLED': 'on', 'VOICE_APP_DEMO_ONLY': 'off'})
    runtime = AppVoiceRuntime(settings=settings, keys=[], defer_task_service=True,
                              identity_authenticator=lambda _authorization: 'owner')
    route = next(r.endpoint for r in create_router(runtime).routes if r.path.endswith('/session'))
    req = AppVoiceSessionRequest(user_id='owner', installation_id='installation-123456',
          consent_version=settings.consent_version, consent_accepted_at='2026-09-19T12:00:00Z', client_protocol_version=4)
    request = SimpleNamespace(headers={'authorization': 'Bearer good'}, client=SimpleNamespace(host='127.0.0.1'))
    issued = asyncio.run(route(req, request))
    assert issued['ticket']
    quota.store.update('agent_quotas', 'owner', {'remaining_tokens': 0})
    # Admission ticket remains valid; ongoing turns never re-check balance.
    assert runtime.tickets.consume(issued['ticket']).user_id == 'owner'
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as failure:
        asyncio.run(route(req, request))
    assert failure.value.detail['code'] == 'agent_quota_exhausted'


def test_internal_context_cannot_be_forged(quota, monkeypatch):
    import asyncio
    from agent_quota import internal
    monkeypatch.setattr(internal, 'secret', lambda: b'quota-test-secret')
    observed = []
    async def app(scope, receive, send):
        observed.append(module.SCOPE.get())
    middleware = internal.MatchmakerQuotaMiddleware(app)
    async def receive():
        return {'type': 'http.request', 'body': b''}
    messages = []
    async def send(value):
        messages.append(value)
    with task_scope('owner', 'matching', 'task'):
        headers = internal.signed_headers()
    async def invoke(value):
        await middleware({'type': 'http', 'path': '/api/match',
                          'headers': [(b'x-agent-quota', value.encode())]}, receive, send)
    asyncio.run(invoke(headers['X-Agent-Quota']))
    assert observed == [('owner', 'matching', 'task')]
    asyncio.run(invoke(headers['X-Agent-Quota'] + 'tampered'))
    assert len(observed) == 1
    assert messages[-2]['status'] == 403


def test_gemini_text_audio_and_repeated_live_snapshot(quota):
    with task_scope('owner', 'voice', 'session'):
        module.record_gemini(SimpleNamespace(usage_metadata=SimpleNamespace(
            prompt_token_count=100, candidates_token_count=20, thoughts_token_count=5)), 'text')
        live = SimpleNamespace(usage_metadata=SimpleNamespace(
            prompt_token_count=300, response_token_count=80))
        module.record_gemini(live, 'live-turn')
        module.record_gemini(live, 'live-turn')
    assert quota.status('owner')['voice_tokens'] == 505


def test_stream_callback_failure_still_records_received_usage(quota):
    with task_scope('owner', 'private'):
        stream = tracked_ollama(iter([{'prompt_eval_count': 40, 'eval_count': 10}]))
        with pytest.raises(RuntimeError):
            for _ in stream:
                raise RuntimeError('client disconnected')
        stream.close()
    assert quota.status('owner')['private_tokens'] == 50


def test_two_service_instances_share_outbox_and_owner_lock(quota):
    other = QuotaService(quota.store, quota.outbox)
    quota.account('owner')
    def work(i):
        client = quota if i % 2 else other
        client.record('owner', 'voice', 'session', str(i), 10, 10)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(work, range(12)))
    assert quota.status('owner')['used_tokens'] == 240


def test_match_queue_uses_foreground_scope_not_client_source(quota, monkeypatch):
    from unittest.mock import MagicMock
    from services import match_search_job_service as jobs
    collection = MagicMock()
    collection.find_one.return_value = None
    profiles = MagicMock()
    profiles.find_one.return_value = {'current_context_revision': 1}
    monkeypatch.setattr(jobs, 'MATCH_SEARCH_JOBS', collection)
    monkeypatch.setattr(jobs, 'profiles_coll', profiles)
    monkeypatch.setattr(jobs, '_has_live_match', lambda _owner: False)
    monkeypatch.setattr(jobs, 'quota_service', quota)
    with task_scope('owner', 'matching', 'foreground'):
        jobs.enqueue_match_search('owner', source='automatic', idempotency_key='foreground')
    assert collection.insert_one.call_args.args[0]['quota_billable'] is True
    jobs.enqueue_match_search('owner', source='automatic', idempotency_key='background')
    assert collection.insert_one.call_args.args[0]['quota_billable'] is False


def _daily_account(quota, *, now, remaining=10000):
    quota.store.update('agent_quota_settings', 'default',
                       {'initial_tokens': 150000, 'daily_refill_tokens': 40000})
    quota.status('owner', now=now)
    quota.store.update('agent_quotas', 'owner', {'remaining_tokens': remaining})


def test_daily_refill_uses_taipei_midnights_not_24_hour_duration(quota):
    from datetime import datetime, timezone
    # UTC 15:59 = Taipei 23:59; two minutes later is the next local day.
    before = datetime(2026, 9, 19, 15, 59, tzinfo=timezone.utc)
    after = datetime(2026, 9, 19, 16, 1, tzinfo=timezone.utc)
    _daily_account(quota, now=before)
    first = quota.status('owner', now=after)
    assert first['remaining_tokens'] == 50000
    assert first['remaining_percent'] == pytest.approx(100 / 3)
    assert first['last_refill_at'] == after.isoformat()
    assert first['next_refill_at'] == '2026-09-21T00:00:00+08:00'
    assert first['refill_timezone'] == 'Asia/Taipei'
    assert first['refill_time'] == '00:00'
    assert quota.status('owner', now=after)['remaining_tokens'] == 50000


def test_two_missed_days_credit_80k_and_long_absence_caps_at_150k(quota):
    from datetime import datetime, timedelta, timezone
    start = datetime(2026, 9, 19, 0, tzinfo=timezone.utc)
    _daily_account(quota, now=start)
    assert quota.status('owner', now=start + timedelta(days=2))['remaining_tokens'] == 90000
    full = quota.status('owner', now=start + timedelta(days=100))
    assert full['remaining_tokens'] == 150000 and full['remaining_percent'] == 100
    assert full['used_tokens'] == 0


def test_full_days_not_banked_and_manual_maximum_still_respected(quota):
    from datetime import datetime, timedelta, timezone
    start = datetime(2026, 9, 19, 0, tzinfo=timezone.utc)
    _daily_account(quota, now=start, remaining=150000)
    today = start + timedelta(days=3)
    assert quota.status('owner', now=today)['remaining_tokens'] == 150000
    quota.record('owner', 'matching', 'task', 'spent', 100000, 0)
    assert quota.status('owner', now=today)['remaining_tokens'] == 50000
    assert quota.status('owner', now=today + timedelta(days=1))['remaining_tokens'] == 90000
    quota.store.update('agent_quotas', 'owner', {'max_tokens': 110000})
    assert quota.status('owner', now=today + timedelta(days=2))['remaining_tokens'] == 110000


def test_parallel_login_only_refills_once(quota):
    from datetime import datetime, timedelta, timezone
    start = datetime(2026, 9, 19, 0, tzinfo=timezone.utc)
    _daily_account(quota, now=start, remaining=0)
    today = start + timedelta(days=2)
    other = QuotaService(quota.store, quota.outbox)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda i: (quota if i % 2 else other).status('owner', now=today), range(16)))
    assert all(row['remaining_tokens'] == 80000 for row in rows)


def test_legacy_upgrade_once_preserves_usage_and_infinity(quota):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 9, 19, 0, tzinfo=timezone.utc)
    quota.store.create('agent_quotas', 'owner', {
        'user_id': 'owner', 'max_tokens': 100000, 'remaining_tokens': 20000,
        'infinity': True, 'used_tokens': 80000, 'matching_tokens': 50000,
        'private_tokens': 20000, 'voice_tokens': 10000, 'revision': '',
    })
    first = quota.status('owner', now=now)
    assert first['max_tokens'] == 150000 and first['remaining_tokens'] == 70000
    assert first['used_tokens'] == 80000 and first['infinity'] is True
    assert quota.status('owner', now=now + timedelta(days=3))['remaining_tokens'] == 70000
    quota.store.update('agent_quotas', 'owner', {'infinity': False})
    assert quota.status('owner', now=now + timedelta(days=3))['remaining_tokens'] == 70000
    assert quota.status('owner', now=now + timedelta(days=4))['remaining_tokens'] == 110000


def test_clock_rollback_does_not_move_marker_or_double_credit(quota):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 9, 19, 0, tzinfo=timezone.utc)
    _daily_account(quota, now=now)
    first = quota.status('owner', now=now + timedelta(days=1))
    old = quota.status('owner', now=now)
    assert old['remaining_tokens'] == first['remaining_tokens'] == 50000
    assert old['last_refill_at'] == first['last_refill_at']
    assert quota.status('owner', now=now + timedelta(days=1))['remaining_tokens'] == 50000


def test_refill_failure_rolls_back_balance_and_marker_then_retries(quota, monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 9, 19, 0, tzinfo=timezone.utc)
    _daily_account(quota, now=now)
    original = quota.store.update
    def failing(collection, document, data, tx=None):
        original(collection, document, data, tx)
        if 'last_refill_at' in data:
            raise StoreError(503)
    monkeypatch.setattr(quota.store, 'update', failing)
    with pytest.raises(QuotaError):
        quota.status('owner', now=now + timedelta(days=1))
    row = quota.store.get('agent_quotas', 'owner')
    assert row['remaining_tokens'] == 10000 and row['last_refill_at'] == now.isoformat()
    monkeypatch.setattr(quota.store, 'update', original)
    assert quota.status('owner', now=now + timedelta(days=1))['remaining_tokens'] == 50000


def test_only_sunnyfan_code_can_be_reused(quota):
    other_code = 'AnotherCode'
    quota.store.create('agent_quota_codes', key(other_code), {'code': other_code, 'enabled': True})
    assert quota.redeem('owner', other_code) == 'redeemed'
    quota.store.update('agent_quotas', 'owner', {'infinity': False})
    assert quota.redeem('owner', other_code) == 'already_redeemed'
    assert quota.status('owner')['infinity'] is False
    assert quota.redeem('owner', 'Sunnyfan116') == 'redeemed'
    assert quota.redeem('owner', 'Sunnyfan116') == 'redeemed'
    assert quota.status('owner')['infinity'] is True
    assert sum(c == 'agent_quota_redemptions' for c, _ in quota.store.rows) == 2


def test_parallel_sunnyfan_redemptions_keep_one_audit_record(quota):
    quota.account('owner')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: quota.redeem('owner', 'Sunnyfan116'), range(8)))
    assert results == ['redeemed'] * 8
    assert sum(c == 'agent_quota_redemptions' for c, _ in quota.store.rows) == 1
    assert quota.status('owner')['infinity'] is True
