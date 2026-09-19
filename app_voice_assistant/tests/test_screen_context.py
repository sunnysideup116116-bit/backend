import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app_voice_assistant.screen_context import screen_context_payload, MAX_SCREEN_CONTEXT_CHARS
from app_voice_assistant.duplex_session import AppVoiceDuplexSession, _system_instruction
from app_voice_assistant.duplex_runtime import run_duplex_session
from app_voice_assistant.settings import AppVoiceSettings
from app_voice_assistant.tests.test_duplex_runtime import FakeLive, FakeWebSocket, FakeProvider, FakeLimiter, wait_until, message, _append


def context():
    return {'scope': 'ayue_public', 'permissions': {'screen_read': True, 'public_ayue': True},
        'screen': {'ready': True, 'surface_id': 'public',
            'controls': [{'kind': 'confirmation', 'title': '套用探索結果',
                'confirm_label': '確認', 'cancel_label': '取消', 'enabled': True, 'visible': True,
                'choice_id': 'must-stay-local'}],
            'composer': {'present': True, 'has_draft': True, 'text': 'private draft'},
            'content': {'kind': 'public_ayue_messages', 'content_permission': 'public_ayue',
                'items': [{'role': 'ayue', 'text': f'第{i}則結果'} for i in range(20)]}}}


def test_bounded_snapshot_uses_recent_messages_and_explicit_control_purpose():
    payload = screen_context_payload(context())
    data = json.loads(payload)['screen']
    assert [m['text'] for m in data['content']['items']] == [f'第{i}則結果' for i in range(16, 20)]
    assert data['content']['truncated'] is True
    assert data['controls'][0]['title'] == '套用探索結果'
    assert data['composer']['has_draft'] is True
    assert 'private draft' not in payload and 'must-stay-local' not in payload
    assert len(payload) <= MAX_SCREEN_CONTEXT_CHARS


def test_revoked_permissions_clear_content_and_controls():
    ctx = context()
    ctx['permissions']['public_ayue'] = False
    data = json.loads(screen_context_payload(ctx))
    assert data['screen']['content']['redacted'] is True
    assert data['screen']['content']['items'] == []
    ctx['permissions']['screen_read'] = False
    data = json.loads(screen_context_payload(ctx))
    assert data['screen_read_allowed'] is False
    assert data['screen']['controls'] == []
    assert data['screen']['composer'] == {}


def test_streaming_partial_text_does_not_flood_model_context():
    ctx = context()
    ctx['screen']['ready'] = False
    first = screen_context_payload(ctx)
    ctx['screen']['content']['items'][-1]['text'] = 'still streaming new tokens'
    assert screen_context_payload(ctx) == first
    ctx['screen']['ready'] = True
    assert 'still streaming new tokens' in screen_context_payload(ctx)


def test_oversized_screen_remains_valid_json_and_redacts_sensitive_text():
    ctx = context()
    ctx['screen']['content']['items'] = [{'text': '個性' * 1000 + ' secret@example.com'}] * 20
    payload = screen_context_payload(ctx)
    assert len(payload) <= MAX_SCREEN_CONTEXT_CHARS
    assert isinstance(json.loads(payload), dict)
    ctx['screen']['content']['items'] = [{'text': 'Email secret@example.com 電話0912345678 密碼: secret123'}]
    payload = screen_context_payload(ctx)
    assert 'secret@example.com' not in payload and '0912345678' not in payload
    assert 'secret123' not in payload


def test_live_screen_transport_does_not_request_an_assistant_turn():
    async def scenario():
        live = AppVoiceDuplexSession(AppVoiceSettings.from_env({}), SimpleNamespace())
        raw = SimpleNamespace(send_client_content=AsyncMock())
        live._session = raw
        await live.send_screen_context(screen_context_payload(context()))
        args = raw.send_client_content.call_args.kwargs
        assert args['turn_complete'] is False
        assert '[APP_SCREEN_STATE]' in args['turns'].parts[0].text
    asyncio.run(scenario())


def test_live_syncs_initial_changes_and_reconnect_without_duplicates():
    async def scenario():
        live, socket, events = FakeLive(), FakeWebSocket(), []
        task = asyncio.create_task(run_duplex_session(socket, provider=FakeProvider(live),
            limiter=FakeLimiter(), identity='test', initial_context=context(),
            max_session_seconds=30, send_event=lambda e: _append(events, e)))
        async def update(ctx):
            await socket.incoming.put({'type': 'websocket.receive', 'text': json.dumps({
                'type': 'context_changed', 'context': ctx})})
        try:
            await wait_until(lambda: len(getattr(live, 'screen_contexts', [])) == 1)
            await update(context())
            await socket.incoming.put({'type': 'websocket.receive', 'bytes': b'audio'})
            await wait_until(lambda: len(live.audio) == 1)
            assert len(live.screen_contexts) == 1
            changed = context()
            changed['screen']['controls'] = []
            changed['screen']['operation_in_progress'] = True
            await update(changed)
            await wait_until(lambda: len(live.screen_contexts) == 2)
            assert json.loads(live.screen_contexts[-1])['screen']['controls'] == []
            await live.incoming.put(message(go_away=SimpleNamespace(time_left=0)))
            await wait_until(lambda: len(live.screen_contexts) == 3)
            assert live.screen_contexts[-1] == live.screen_contexts[-2]
        finally:
            await socket.incoming.put({'type': 'websocket.disconnect'})
            await task
    asyncio.run(scenario())


def test_all_voice_modes_use_quiet_screen_updates():
    for mode in ('legacy', 'template', 'proxy'):
        prompt = _system_instruction({}, routing_mode=mode)
        assert '[APP_SCREEN_STATE]' in prompt
        assert '保持安靜' in prompt
        assert '動畫由 App' in prompt
