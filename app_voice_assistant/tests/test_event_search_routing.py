import asyncio
import json
from types import SimpleNamespace
import pytest
from app_voice_assistant.contracts import deterministic_proposal
from app_voice_assistant.duplex_runtime import run_duplex_session
from app_voice_assistant.tests.test_duplex_runtime import FakeLive, FakeWebSocket, FakeProvider, FakeLimiter, wait_until, message, _append


@pytest.mark.parametrize('query', [
    '幫我找9月20日在高雄有沒有活動', '我明天有空，幫我找高雄的活動',
    '幫我查週六台北的展覽', 'Find concerts in Taipei on September 20',
])
def test_external_events_delegate_to_public_ayue_from_calendar(query):
    result = deterministic_proposal(query, context={'scope': 'calendar'})
    assert result.intent == 'ayue.public_query'
    assert result.arguments == {'domain': 'web', 'question': query}


@pytest.mark.parametrize('query', ['我的行事曆明天有哪些活動', '取消2026-09-20的活動', '我明天有空嗎'])
def test_personal_calendar_stays_out_of_external_search(query):
    result = deterministic_proposal(query, context={'scope': 'calendar'})
    assert result is None or result.intent != 'ayue.public_query'


def test_ambiguous_day_needs_context_and_keeps_location():
    query = '幫我查那天在高雄有哪些活動'
    assert deterministic_proposal(query, context={}) is None
    result = deterministic_proposal(query, context={'_calendar_reference': '2026-09-20'})
    assert '2026-09-20' in result.arguments['question']
    assert '高雄' in result.arguments['question']


def test_calendar_to_external_events_works_without_model_function_call():
    async def scenario():
        live, socket, events = FakeLive(), FakeWebSocket(), []
        ctx = {'scope': 'calendar', 'permissions': {'calendar_read': True, 'public_ayue': True, 'web_search': True}}
        task = asyncio.create_task(run_duplex_session(socket, provider=FakeProvider(live),
            limiter=FakeLimiter(), identity='test', initial_context=ctx, routing_mode='template',
            max_session_seconds=30, send_event=lambda e: _append(events, e)))
        async def say(text):
            await live.incoming.put(message(voice_activity=SimpleNamespace(voice_activity_type='ACTIVITY_START')))
            await live.incoming.put(message(content=SimpleNamespace(
                interim_input_transcription=None, input_transcription=SimpleNamespace(text=text, finished=True),
                interrupted=False, output_transcription=None, model_turn=None, turn_complete=False)))
        try:
            await say('2026-09-20有空嗎')
            await wait_until(lambda: any(e.get('intent') == 'calendar.query' for e in events))
            calendar = next(e for e in events if e.get('intent') == 'calendar.query')
            await socket.incoming.put({'type': 'websocket.receive', 'text': json.dumps({
                'type': 'action_result', 'action_id': calendar['action_id'], 'success': True, 'message': '當天沒有行程'})})
            await live.incoming.put(message(content=SimpleNamespace(interim_input_transcription=None,
                input_transcription=None, interrupted=False, output_transcription=None, model_turn=None, turn_complete=True)))
            await socket.incoming.put({'type': 'websocket.receive', 'text': json.dumps({'type': 'context_changed', 'context': ctx})})
            await say('幫我找當天在高雄有沒有活動')
            await wait_until(lambda: any(e.get('intent') == 'ayue.public_query' for e in events))
            result = next(e for e in events if e.get('intent') == 'ayue.public_query')
            assert result['arguments']['domain'] == 'web'
            assert '2026-09-20' in result['arguments']['question']
            assert '高雄' in result['arguments']['question']
        finally:
            await socket.incoming.put({'type': 'websocket.disconnect'})
            await task
    asyncio.run(scenario())
