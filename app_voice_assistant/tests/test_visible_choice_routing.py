import asyncio
from types import SimpleNamespace
import pytest
from app_voice_assistant.contracts import deterministic_proposal, visible_choice_action
from app_voice_assistant.duplex_runtime import run_duplex_session
from app_voice_assistant.tests.test_duplex_runtime import (
    FakeLive, FakeWebSocket, FakeProvider, FakeLimiter, message, wait_until, _append,
)

CONFIRMS = ['開始', '好，可以開始了', '嗯，那就開始吧', '麻煩你幫我開始',
            '可以套用', '幫我套用結果吧', '好，那我們繼續探索吧', '請直接開始性格探索']
CANCELS = ['那先取消吧', '麻煩你幫我取消', '不要開始', '先不要套用了']


@pytest.mark.parametrize('spoken,expected', [(s, 'confirm') for s in CONFIRMS] + [(s, 'cancel') for s in CANCELS])
def test_flexible_choice_wording_requires_a_pending_button(spoken, expected):
    assert visible_choice_action(spoken) == expected
    proposal = deterministic_proposal(spoken, context={'feature_status': {'visible_choice_pending': True}})
    assert proposal.intent == 'ui.choice.activate'
    assert proposal.arguments == {'action': expected}
    absent = deterministic_proposal(spoken, context={})
    assert absent is None or absent.intent != 'ui.choice.activate'


@pytest.mark.parametrize('spoken', ['開始前我想問一下', '可以取消嗎', '不要取消我想繼續聊', '我還沒確定', '幫我確認明天的行程'])
def test_other_sentences_do_not_press_buttons(spoken):
    assert visible_choice_action(spoken) is None


@pytest.mark.parametrize('spoken', ['開始', '好，可以開始了', '那先取消吧'])
def test_template_presses_button_even_without_model_tool_call(spoken):
    async def scenario():
        live, socket, events = FakeLive(), FakeWebSocket(), []
        task = asyncio.create_task(run_duplex_session(
            socket, provider=FakeProvider(live), limiter=FakeLimiter(), identity='test',
            initial_context={'scope': 'global', 'feature_status': {'visible_choice_pending': True}},
            max_session_seconds=30, send_event=lambda event: _append(events, event),
            routing_mode='template',
        ))
        try:
            await live.incoming.put(message(content=SimpleNamespace(
                interim_input_transcription=None,
                input_transcription=SimpleNamespace(text=spoken, finished=True),
                interrupted=False, output_transcription=None, model_turn=None, turn_complete=False,
            )))
            await wait_until(lambda: any(e.get('type') == 'action_proposal' for e in events))
            actions = [e for e in events if e.get('type') == 'action_proposal']
            assert len(actions) == 1
            assert actions[0]['intent'] == 'ui.choice.activate'
            assert actions[0]['arguments'] == {'action': visible_choice_action(spoken)}
        finally:
            await socket.incoming.put({'type': 'websocket.disconnect'})
            await task
    asyncio.run(scenario())
