import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading

import mongomock
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app_voice_assistant.contextual import safe_result
from app_voice_assistant.contracts import deterministic_proposal
from app_voice_assistant.duplex_runtime import run_duplex_session
from app_voice_assistant.memory import VoiceMemoryError
from app_voice_assistant.router import AppVoiceRuntime, create_router
from app_voice_assistant.settings import AppVoiceSettings
from app_voice_assistant.status_contract import public_quota, public_cooldown, status_result, provider_error_code
from app_voice_assistant.status_service import VoiceStatusService
from app_voice_assistant.task_service import VoiceTaskService
from app_voice_assistant.tests.test_duplex_runtime import FakeLive, FakeWebSocket, FakeLimiter, FakeProvider, wait_until


def quota(remaining=100, *, unlimited=False):
    return {'remaining_tokens': remaining, 'remaining_percent': remaining / 10,
            'infinity': unlimited, 'next_refill_at': '2026-09-22T00:00:00+08:00'}


@pytest.mark.parametrize('remaining,unlimited,exhausted', [(0, False, True), (0, True, False), (1, False, False), (100, False, False)])
def test_quota_projection_is_shared_and_exhaustion_is_not_rounded_percent(remaining, unlimited, exhausted):
    result = public_quota(quota(remaining, unlimited=unlimited))
    assert result['exhausted'] is exhausted
    assert result['shared'] is True
    assert result['unlimited'] is unlimited
    assert 'remaining_tokens' not in result and 'used_tokens' not in result
    assert public_quota(result)['exhausted'] is exhausted


def test_cooldown_projection_uses_authoritative_deadline_and_ceil():
    now = datetime.now(timezone.utc)
    result = public_cooldown({'state': 'active', 'server_time': now.isoformat(),
                              'until': (now + timedelta(milliseconds=1500)).isoformat(),
                              'remaining_seconds': 9999})
    assert result['remaining_seconds'] == 2
    assert public_cooldown({'state': 'active'})['state'] == 'unknown'
    assert public_cooldown(None)['remaining_seconds'] is None


def test_safe_result_preserves_public_status_but_drops_risk_internals_and_ids():
    now = datetime.now(timezone.utc)
    result = safe_result({'result_version': 1, 'success': False, 'error_code': 'risk_cooldown',
        'data': {'quota': quota(0), 'cooldown': {'state': 'active', 'server_time': now.isoformat(),
          'until': (now + timedelta(seconds=65)).isoformat()},
          'delivery': {'state': 'delivered', 'sender_message': '請稍微休息', 'risk_available': True,
                       'receiver_directive': {'secret': True}, 'risk_state': {'score': .9}},
          'local_contact_id': 'private-id', 'raw_risk': {'score': .9}}})
    assert result['error_code'] == 'risk_cooldown'
    assert result['data']['delivery']['state'] == 'delivered'
    assert result['data']['cooldown']['remaining_seconds'] == 65
    assert set(result['data']) == {'quota', 'cooldown', 'delivery'}
    assert 'receiver_directive' not in result['data']['delivery']


def test_status_endpoints_require_verified_owner_but_do_not_require_quota():
    seen = []
    def identity(header):
        if header != 'Bearer test-jwt': raise VoiceMemoryError('appwrite_jwt_required')
        return 'verified-owner'
    def chat(owner, contact, attempt):
        seen.append((owner, contact, attempt))
        if contact != 'accepted-contact': raise HTTPException(403, detail='contact_unavailable')
        return status_result(cooldown={'state': 'unknown'}, delivery={'state': 'delivered'})
    runtime = AppVoiceRuntime(settings=AppVoiceSettings.from_env({}), keys=[],
        identity_authenticator=identity,
        status_service=VoiceStatusService(quota_reader=lambda owner: quota(0), chat_reader=chat))
    app = FastAPI(); app.include_router(create_router(runtime))
    with TestClient(app) as client:
        assert client.get('/api/app-voice/status/quota').status_code == 401
        headers = {'Authorization': 'Bearer test-jwt'}
        result = client.get('/api/app-voice/status/quota', headers=headers)
        assert result.status_code == 200
        assert result.json()['data']['quota']['exhausted'] is True
        assert client.get('/api/app-voice/status/chat', params={'contact_id': 'stranger'}, headers=headers).status_code == 403
        result = client.get('/api/app-voice/status/chat', params={
            'contact_id': 'accepted-contact', 'client_message_id': 'voice-attempt', 'user_id': 'attacker'}, headers=headers)
        assert result.json()['data']['delivery']['state'] == 'delivered'
        assert seen[-1] == ('verified-owner', 'accepted-contact', 'voice-attempt')


def test_status_reads_are_rate_limited():
    service = VoiceStatusService()
    for _ in range(60): service.allow_read('owner')
    with pytest.raises(HTTPException) as failure: service.allow_read('owner')
    assert failure.value.status_code == 429
    service.allow_read('other-owner')


def test_concurrent_quota_queries_share_one_owner_read():
    calls = []
    release = threading.Event()
    def reader(owner):
        calls.append(owner)
        release.wait(2)
        return quota()
    async def scenario():
        service = VoiceStatusService(quota_reader=reader)
        first = asyncio.create_task(service.quota('owner'))
        second = asyncio.create_task(service.quota('owner'))
        await wait_until(lambda: bool(calls))
        release.set()
        result = await asyncio.gather(first, second)
        assert result[0] == result[1]
    asyncio.run(scenario())
    assert calls == ['owner']


def test_zero_quota_never_constructs_or_connects_provider():
    class Provider:
        def create_duplex_session(self, **kwargs): raise AssertionError('must not use a model')
    async def scenario():
        events = []
        async def emit(event): events.append(event)
        async def read(): return public_quota(quota(0))
        await run_duplex_session(FakeWebSocket(), provider=Provider(), limiter=FakeLimiter(),
            identity='owner', initial_context={}, max_session_seconds=2,
            send_event=emit, quota_reader=read)
        assert events[-1]['code'] == 'agent_quota_exhausted'
    asyncio.run(scenario())


def test_live_exhaustion_closes_provider_and_does_not_reconnect():
    class Socket(FakeWebSocket):
        async def close(self, code=1000):
            await self.incoming.put({'type': 'websocket.disconnect'})
    async def scenario():
        live, socket, events = FakeLive(), Socket(), []
        reads = 0
        async def read():
            nonlocal reads
            reads += 1
            return public_quota(quota(100 if reads == 1 else 0))
        async def emit(event): events.append(event)
        await run_duplex_session(socket, provider=FakeProvider(live), limiter=FakeLimiter(),
            identity='owner', initial_context={}, max_session_seconds=2, send_event=emit,
            quota_reader=read, quota_poll_seconds=.01)
        assert live.closed and live.reconnect_count == 0
        assert any(e.get('code') == 'agent_quota_exhausted' for e in events)
    asyncio.run(scenario())


def test_quota_pauses_only_unissued_work_and_keeps_completed_results():
    from app_voice_assistant.capability_proxy import action_metadata
    db = mongomock.MongoClient().db
    service = VoiceTaskService(db.batches, db.tasks, enabled=True)
    created = service.create_batch(user_id='owner', session_id='session', operations=[
        {'operation_key': name, 'depends_on': [], 'arguments': {}, 'action': action_metadata('contacts.query')}
        for name in ['done', 'issued', 'pending']])
    service.update(created.tasks[0]['task_id'], status='completed', result_summary='已完成')
    service.update(created.tasks[1]['task_id'], status='waiting_device')
    paused = service.pause_for_quota('owner')
    assert len(paused) == 1 and paused[0]['status'] == 'waiting_input'
    assert service.get_task('owner', created.tasks[0]['task_id'])['status'] == 'completed'
    assert service.get_task('owner', created.tasks[1]['task_id'])['status'] == 'waiting_device'


@pytest.mark.parametrize('text,intent', [('我還剩多少額度', 'quota.query'), ('語音和配對共用嗎', 'quota.query'),
    ('冷卻還有多久', 'chat.status.query'), ('剛剛那則有送出嗎', 'chat.status.query')])
def test_state_questions_route_without_generating_business_actions(text, intent):
    assert deterministic_proposal(text, context={}).intent == intent


def test_provider_limit_is_distinct_from_account_quota():
    error = RuntimeError('provider failed'); error.__cause__ = RuntimeError('429 RESOURCE_EXHAUSTED')
    assert provider_error_code(error) == 'provider_rate_limited'


@pytest.mark.parametrize('mode', ['legacy', 'template', 'proxy'])
def test_advertised_tool_count_matches_actual_live_contract(mode):
    from app_voice_assistant.duplex_session import _live_tools
    from app_voice_assistant.tests.test_template_dispatcher import FakeTypes
    runtime = AppVoiceRuntime(settings=AppVoiceSettings.from_env({'VOICE_APP_TOOL_ROUTING_MODE': mode}), keys=[])
    router = create_router(runtime)
    endpoint = next(route.endpoint for route in router.routes if route.path.endswith('/capability'))
    assert asyncio.run(endpoint())['max_model_tools'] == sum(
        len(tool.function_declarations) for tool in _live_tools(FakeTypes, mode))


def test_exhausted_live_accepts_inflight_receipt_without_resuming_model():
    import json
    from app_voice_assistant.capability_proxy import action_metadata
    async def scenario():
        live, socket, events = FakeLive(), FakeWebSocket(), []
        db = mongomock.MongoClient().db
        service = VoiceTaskService(db.batches, db.tasks, enabled=True)
        created = service.create_batch(user_id='owner', session_id='earlier', operations=[{
            'operation_key': 'contacts', 'depends_on': [], 'arguments': {},
            'action': action_metadata('contacts.query')}])
        service.set_action(created.tasks[0]['task_id'], action_id='previous-read',
                           status='waiting_device', stage='waiting_device')
        reads = 0
        async def read():
            nonlocal reads
            reads += 1
            return public_quota(quota(100 if reads == 1 else 0))
        async def emit(event): events.append(event)
        running = asyncio.create_task(run_duplex_session(socket, provider=FakeProvider(live),
            limiter=FakeLimiter(), identity='owner', user_id='owner', task_service=service,
            initial_context={'permissions': {'chat_list': True}, 'feature_status': {'operational_status': True}},
            max_session_seconds=5, send_event=emit, quota_reader=read, quota_poll_seconds=.03))
        try:
            await wait_until(lambda: any(e['type'] == 'action_proposal' for e in events))
            proposal = next(e for e in events if e['type'] == 'action_proposal')
            await wait_until(lambda: live.closed)
            text_count = len(live.text)
            await socket.incoming.put({'type': 'websocket.receive', 'bytes': b'\x00\x00'})
            await socket.incoming.put({'type': 'websocket.receive', 'text': json.dumps({
                'type': 'action_result', 'action_id': proposal['action_id'], 'success': True,
                'message': '讀取已完成'})})
            await wait_until(lambda: service.get_task('owner', created.tasks[0]['task_id'])['status'] == 'completed')
            assert not live.audio and live.reconnect_count == 0
            assert len(live.text) == text_count
        finally:
            await socket.incoming.put({'type': 'websocket.disconnect'})
            await running
    asyncio.run(scenario())
