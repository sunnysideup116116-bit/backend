from types import SimpleNamespace
from unittest.mock import MagicMock
import mongomock
import pytest
from services import assessment_session_service as assessment
from services.ayue_agent.pi import public_turn
from services.ayue_agent.shared.confirmation import ConfirmationManager
from services.ayue_agent.shared.contact_selections import ContactSelectionManager
from services.ayue_agent.shared.operation_batches import OperationBatchManager


@pytest.mark.parametrize('action', ['confirm', 'cancel'])
@pytest.mark.parametrize('resume', [False, True])
def test_completed_assessment_has_buttons_and_resolves(monkeypatch, action, resume):
    db = mongomock.MongoClient().test
    monkeypatch.setattr(assessment, 'profiles_coll', db.profiles)
    monkeypatch.setattr(public_turn, 'messages_coll', db.messages)
    draft = {'O': 8, 'C': 6, 'E': 7, 'A': 8, 'N': 4, 'summary': '好奇且外向'}
    db.profiles.insert_one({'user_id': 'owner', 'big_five': {'summary': '舊資料'}})
    assessment.start_assessment_session('owner', 'big_five', idempotency_key='start')
    monkeypatch.setattr(assessment, 'analyze_big_five', MagicMock(return_value={
        'reply': '分析完成', 'big_five': draft, 'is_complete': True,
    }))
    ctx = SimpleNamespace(user_id='owner', room_id='room',
        user_profile=db.profiles.find_one({'user_id': 'owner'}),
        message='我喜歡認識新朋友', message_id='answer', assessment_action=None,
        choice_action=None, choice_id=None)
    result = public_turn._handle_assessment(ctx, 'run1')
    assert result.assessment_state == 'awaiting_commit'
    if resume:
        ctx.user_profile = db.profiles.find_one({'user_id': 'owner'})
        result = public_turn._handle_assessment(ctx, 'run1')
    confirmations = ConfirmationManager(db.confirmations)
    selections = ContactSelectionManager(db.selections)
    batches = OperationBatchManager(db.batches)
    result = public_turn._bind_interactions(result, ctx=ctx, run_id='run1',
        confirmations=confirmations, selections=selections, batches=batches)
    assert '好奇且外向' in result.reply
    assert result.choice_prompt['state'] == 'pending'
    assert sum(b['type'] == 'confirmation' for b in result.interaction_blocks_v1) == 1
    record = db.confirmations.find_one({'_id': result.choice_prompt['id']})
    assert record['status'] == 'prepared'
    assert record['payload']['revision'] == 1
    assert db.profiles.find_one({'user_id': 'owner'})['big_five'] == {'summary': '舊資料'}
    assert confirmations.mark_presented(user_id='owner', origin_run_id='run1',
        message_id='assistant-message', persisted_content=result.reply,
        interaction_blocks_v1=result.interaction_blocks_v1)
    ctx.choice_id, ctx.choice_action = result.choice_prompt['id'], action
    resolved = public_turn._handle_choice(ctx, None, 'run2', confirmations, selections, batches)
    assert resolved.choice_resolution['state'] == ('confirmed' if action == 'confirm' else 'cancelled')
    profile = db.profiles.find_one({'user_id': 'owner'})
    assert profile['big_five'] == (draft if action == 'confirm' else {'summary': '舊資料'})
    assert profile['agentic_assessment_session']['status'] == ('completed' if action == 'confirm' else 'cancelled')
    again = public_turn._handle_choice(ctx, None, 'run3', confirmations, selections, batches)
    assert again.conversation_intent == 'confirmation_missing'
    assert db.profiles.find_one({'user_id': 'owner'}) == profile
