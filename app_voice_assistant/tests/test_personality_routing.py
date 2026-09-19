import asyncio
import json
from types import SimpleNamespace

import pytest

from app_voice_assistant.contracts import deterministic_proposal, context_allows_proposal
from app_voice_assistant.duplex_runtime import run_duplex_session
from app_voice_assistant.template_dispatcher import (
    authoritative_template_proposal, template_tool_call_for_proposal,
    template_proposal_from_function,
)
from app_voice_assistant.tests.test_duplex_runtime import (
    FakeLive, FakeWebSocket, FakeProvider, FakeLimiter, message, wait_until, _append,
)


@pytest.mark.parametrize('spoken', [
    '我希望可以更認識我', '我希望可以開始性格探索', '你可以再更認識我嗎？',
    '我想讓你更了解我', '你能不能更深入了解我？', '讓你多認識我',
    '開始個性探索',
])
def test_personality_request_round_trips_through_template(spoken):
    proposal = authoritative_template_proposal(spoken, context={'revision': 3})
    assert proposal is not None
    assert proposal.intent == 'personality.explore'
    assert spoken in proposal.arguments['message']
    assert '探索' in proposal.arguments['message']
    name, args = template_tool_call_for_proposal(proposal)
    assert args['domain'] == 'personality'
    restored = template_proposal_from_function(SimpleNamespace(name=name, args=args), revision=3)
    assert restored.intent == proposal.intent
    assert restored.arguments == proposal.arguments
    assert not context_allows_proposal({'permissions': {'public_ayue': True}}, restored)
    assert context_allows_proposal({'permissions': {'public_ayue': True, 'memory_read': True}}, restored)


@pytest.mark.parametrize('spoken', [
    '你對我了解多少', '描述我', '我想更認識他', '你可以幫我認識新朋友嗎',
    '不要再更認識我', '我不想開始性格探索', '停止性格探索', '取消性格探索',
])
def test_other_requests_do_not_start_personality_exploration(spoken):
    proposal = deterministic_proposal(spoken, context={})
    assert proposal is None or proposal.intent != 'personality.explore'


def test_template_personality_fallback_and_follow_up():
    async def scenario():
        live, socket, events = FakeLive(), FakeWebSocket(), []
        task = asyncio.create_task(run_duplex_session(
            socket, provider=FakeProvider(live), limiter=FakeLimiter(), identity='test',
            user_id='u1', voice_session_id='s1',
            initial_context={'scope': 'global', 'revision': 2,
                             'permissions': {'public_ayue': True, 'memory_read': True}},
            max_session_seconds=30, send_event=lambda event: _append(events, event),
            routing_mode='template',
        ))
        try:
            await live.incoming.put(message(content=SimpleNamespace(
                interim_input_transcription=None,
                input_transcription=SimpleNamespace(text='你可以再更認識我嗎？', finished=True),
                interrupted=False, output_transcription=None, model_turn=None, turn_complete=False,
            )))
            await wait_until(lambda: any(e.get('type') == 'action_proposal' for e in events))
            start = next(e for e in events if e.get('type') == 'action_proposal')
            assert start['intent'] == 'personality.explore'
            assert '開始性格探索' in start['arguments']['message']
            await socket.incoming.put({'type': 'websocket.receive', 'text': json.dumps({
                'type': 'action_result', 'action_id': start['action_id'], 'success': True,
                'message': '在人多的場合，你通常會怎麼做？',
            })})
            await live.incoming.put(message(content=SimpleNamespace(
                interim_input_transcription=None, input_transcription=None,
                interrupted=False, output_transcription=None, model_turn=None, turn_complete=True,
            )))
            await live.incoming.put(message(voice_activity=SimpleNamespace(
                voice_activity_type='ACTIVITY_START',
            )))
            await live.incoming.put(message(content=SimpleNamespace(
                interim_input_transcription=None,
                input_transcription=SimpleNamespace(text='我在人多的場合會先觀察', finished=True),
                interrupted=False, output_transcription=None, model_turn=None, turn_complete=False,
            )))
            await live.incoming.put(message(tool_calls=[SimpleNamespace(
                id='answer', name='ask_app_ayue',
                args={'domain': 'personality', 'question': '我在人多的場合會先觀察'},
            )]))
            await wait_until(lambda: any(e.get('action_id') == 'answer' for e in events))
            answer = next(e for e in events if e.get('action_id') == 'answer')
            assert answer['intent'] == 'personality.explore'
            assert answer['arguments'] == {'message': '我在人多的場合會先觀察'}
        finally:
            await socket.incoming.put({'type': 'websocket.disconnect'})
            await task
    asyncio.run(scenario())
