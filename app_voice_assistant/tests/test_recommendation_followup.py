import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app_voice_assistant.contracts import VoiceProposal
from app_voice_assistant.contextual import safe_result
from app_voice_assistant.duplex_session import AppVoiceDuplexSession
from app_voice_assistant.duplex_runtime import run_duplex_session
from app_voice_assistant.recommendations import resolve_calendar_recommendation
from app_voice_assistant.settings import AppVoiceSettings
from app_voice_assistant.tests.test_duplex_runtime import (
    FakeLive, FakeProvider, FakeLimiter, FakeWebSocket, message, wait_until, _append,
)

PLACES = [{'name': '台東山海咖啡', 'category': 'cafe', 'address_summary': '台東市山海路一號'}]


def test_generic_cafe_resolves_to_recommendation_but_multiple_cafes_need_selection():
    proposal = VoiceProposal('calendar.create', {'title': '咖啡廳'}, '', 0)
    resolved, question = resolve_calendar_recommendation(proposal, PLACES)
    assert question is None
    assert resolved.arguments == {'title': '台東山海咖啡', 'location': '台東市山海路一號'}
    resolved, question = resolve_calendar_recommendation(proposal, PLACES + [
        {'name': '另一家咖啡', 'category': 'cafe', 'address_summary': '另一地址'},
    ])
    assert resolved is None and '哪一家' in question


def test_explicit_different_venue_is_not_replaced():
    proposal = VoiceProposal('calendar.create', {'title': '別的咖啡廳'}, '', 0)
    resolved, question = resolve_calendar_recommendation(proposal, PLACES)
    assert resolved == proposal and question is None


def test_generic_location_or_known_city_still_resolves_the_real_cafe():
    for location in ('咖啡廳', '台東市'):
        proposal = VoiceProposal('calendar.create', {'title': '咖啡廳', 'location': location}, '', 0)
        resolved, question = resolve_calendar_recommendation(proposal, PLACES)
        assert question is None
        assert resolved.arguments == {'title': '台東山海咖啡', 'location': '台東市山海路一號'}


def test_places_survive_result_filter_and_long_live_transport():
    result = safe_result({'success': True, 'result_version': 1, 'message': '前文' * 1800,
                          'data': {'recommendations': PLACES}})
    assert result['data']['recommendations'] == PLACES
    async def scenario():
        live = AppVoiceDuplexSession(AppVoiceSettings.from_env({}), SimpleNamespace())
        live._session = SimpleNamespace(send_realtime_input=AsyncMock())
        payload = json.dumps(result, ensure_ascii=False)
        await live.send_text(payload)
        assert live._session.send_realtime_input.call_args.kwargs['text'] == payload
    asyncio.run(scenario())


def test_unspoken_recommendation_survives_page_change_and_enriches_calendar_confirmation():
    async def scenario():
        live, socket, events = FakeLive(), FakeWebSocket(), []
        permissions = {'public_ayue': True, 'places': True, 'calendar_read': True, 'calendar_write': True}
        task = asyncio.create_task(run_duplex_session(
            socket, provider=FakeProvider(live), limiter=FakeLimiter(), identity='test',
            initial_context={'permissions': permissions}, max_session_seconds=30,
            send_event=lambda event: _append(events, event),
        ))
        try:
            await live.incoming.put(message(tool_calls=[SimpleNamespace(id='places', name='ask_public_ayue',
                args={'domain': 'places', 'question': '台東有什麼好玩的'})]))
            await wait_until(lambda: any(e.get('action_id') == 'places' for e in events))
            await socket.incoming.put({'type': 'websocket.receive', 'text': json.dumps({
                'type': 'action_result', 'action_id': 'places', 'result_version': 1, 'success': True,
                'message': '先介紹其他景點。' * 300 + '最後還有台東山海咖啡。',
                'data': {'recommendations': PLACES},
            })})
            await live.incoming.put(message(content=SimpleNamespace(interim_input_transcription=None,
                input_transcription=None, output_transcription=None, interrupted=False, model_turn=None, turn_complete=True)))
            await wait_until(lambda: any('最後還有台東山海咖啡' in item for item in live.text))
            await socket.incoming.put({'type': 'websocket.receive', 'text': json.dumps({
                'type': 'context_changed', 'context': {'scope': 'calendar', 'permissions': permissions},
            })})
            await live.incoming.put(message(tool_calls=[SimpleNamespace(id='calendar', name='create_calendar_event',
                args={'title': '咖啡廳', 'date': '2026-09-21', 'start_time': '10:00', 'end_time': '11:00'})]))
            await wait_until(lambda: any(e.get('intent') == 'calendar.create' for e in events))
            confirmation = next(e for e in events if e.get('intent') == 'calendar.create')
            assert confirmation['type'] == 'confirmation_required'
            assert confirmation['arguments']['title'] == '台東山海咖啡'
            assert confirmation['arguments']['location'] == '台東市山海路一號'
        finally:
            await socket.incoming.put({'type': 'websocket.disconnect'})
            await task
    asyncio.run(scenario())
