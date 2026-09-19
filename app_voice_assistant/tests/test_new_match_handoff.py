import asyncio
from types import SimpleNamespace
import pytest
from app_voice_assistant.contracts import deterministic_proposal, requires_confirmation
from app_voice_assistant.duplex_runtime import run_duplex_session
from app_voice_assistant.tests.test_duplex_runtime import FakeLive, FakeWebSocket, FakeProvider, FakeLimiter, message, wait_until, _append


@pytest.mark.parametrize('spoken', ['幫我找新的人配對', '找新人配對', 'find a new match', 'please find me a new match'])
def test_new_match_hands_off_immediately_but_does_not_claim_search_started(spoken):
    proposal = deterministic_proposal(spoken, context={})
    assert proposal.intent == 'match.ayue_query'
    assert requires_confirmation(proposal) is False
    async def scenario():
        live, socket, events = FakeLive(), FakeWebSocket(), []
        task = asyncio.create_task(run_duplex_session(socket, provider=FakeProvider(live), limiter=FakeLimiter(),
            identity='test', initial_context={'permissions': {'match_ayue': True, 'match_read': True, 'match_actions': True}},
            routing_mode='template', max_session_seconds=30, send_event=lambda e: _append(events, e)))
        try:
            await live.incoming.put(message(content=SimpleNamespace(interim_input_transcription=None,
                input_transcription=SimpleNamespace(text=spoken, finished=True), interrupted=False,
                output_transcription=None, model_turn=None, turn_complete=False)))
            await wait_until(lambda: any(e.get('type') == 'action_proposal' for e in events))
            action = next(e for e in events if e.get('type') == 'action_proposal')
            assert action['intent'] == 'match.ayue_query'
            assert action['arguments']['question'] == spoken
            assert not any(e.get('type') == 'confirmation_required' for e in events)
        finally:
            await socket.incoming.put({'type': 'websocket.disconnect'})
            await task
    asyncio.run(scenario())


@pytest.mark.parametrize('spoken', ['不要幫我找新的配對', "don't find a new match"])
def test_rejection_is_not_classified_as_a_new_search(spoken):
    proposal = deterministic_proposal(spoken, context={})
    assert proposal is None or proposal.intent != 'match.ayue_query'


@pytest.mark.parametrize('spoken', ['start', 'go ahead'])
def test_english_start_activates_an_existing_choice_only(spoken):
    proposal = deterministic_proposal(spoken, context={'feature_status': {'visible_choice_pending': True}})
    assert proposal.intent == 'ui.choice.activate'
    assert proposal.arguments == {'action': 'confirm'}
    absent = deterministic_proposal(spoken, context={})
    assert absent is None or absent.intent != 'ui.choice.activate'
