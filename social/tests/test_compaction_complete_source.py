import json
from types import SimpleNamespace

import mongomock
import pytest

from services import conversation_compaction_service as c
from services.message_use_service import metadata_for_use


@pytest.fixture
def store(monkeypatch):
    db = mongomock.MongoClient()['compaction_complete_source']
    monkeypatch.setattr(c, 'messages_coll', db.messages)
    monkeypatch.setattr(c, 'CONVERSATION_COMPACTIONS', db.summaries)
    monkeypatch.setattr(c, 'CONVERSATION_COMPACTION_RUNS', db.runs)
    monkeypatch.setenv('AYUE_CONVERSATION_COMPACTION_MODE', 'shadow')
    monkeypatch.setenv('AYUE_CONVERSATION_CONTEXT_MODE', 'on')
    monkeypatch.setenv('AYUE_CONVERSATION_CONTEXT_USER_ALLOWLIST', 'owner')
    return db


def message(store, text, use='ordinary'):
    doc = {'room_id': 'ai_assistant_owner', 'sender_id': 'owner', 'content': text,
           'timestamp': float(store.messages.count_documents({}) + 1),
           'metadata': {'message_use': metadata_for_use(use)}}
    doc['_id'] = store.messages.insert_one(doc).inserted_id
    return doc


def run(messages, revision=0, source_hash=None):
    return c.run_conversation_compaction_shadow('owner', 'ai_assistant_owner',
        [str(m['_id']) for m in messages], revision, source_hash)


def approve(*args, **kwargs):
    return c.ConversationCompactionEvaluationDecisionV1(
        retention=c.ContinuityRetentionV1(**{field: True for field in c.SUMMARY_FIELDS}),
        unsupported_content=False, role_confusion=False, canonical_state_leak=False, confidence=.95)


@pytest.mark.parametrize('position', ['head', 'middle', 'tail'])
def test_complete_correction_reaches_both_model_prompts(store, monkeypatch, position):
    correction = '更正：角色已改成白鷺。'
    parts = {'head': correction + '景' * 1473,
             'middle': '景' * 950 + correction + '景' * 500,
             'tail': '景' * 1473 + correction}
    msgs = [message(store, parts[position])]
    prompts = []
    def provider(prompt, **kwargs):
        prompts.append(prompt)
        payload = (approve().model_dump() if 'shadow evaluator' in prompt else
                   c.ConversationSummaryV1(known_continuity=['角色已改成白鷺']).model_dump())
        return SimpleNamespace(content=json.dumps(payload))
    monkeypatch.setattr(c, 'generate_chat_completion', provider)
    assert run(msgs)['status'] == 'stored'
    assert len(prompts) == 2 and all(correction in p for p in prompts)


def test_batch_selection_uses_complete_contiguous_prefix(store):
    docs = [message(store, '甲' * 5000), message(store, '乙' * 5000)]
    docs += [message(store, '短句') for _ in range(29)]
    selected = c._select_compaction_batch('owner', 'ai_assistant_owner')
    assert selected['message_ids'] == [str(docs[0]['_id'])]


def test_worker_rechecks_budget_and_does_not_cover_unprocessed_tail(store, monkeypatch):
    docs = [message(store, '甲' * 5000), message(store, '乙' * 5000)]
    monkeypatch.setattr(c, '_generate_summary', lambda *a, **k: c.ConversationSummaryV1(active_topics=['測試']))
    monkeypatch.setattr(c, '_evaluate_summary', approve)
    assert run(docs)['covered_message_count'] == 1
    saved = store.summaries.find_one({})
    assert saved['covered_through_message_id'] == str(docs[0]['_id'])
    assert list(store.messages.find(c._message_query_after(saved)))[0]['_id'] == docs[1]['_id']


def test_single_oversized_message_is_deferred_without_model_or_watermark(store, monkeypatch):
    docs = [message(store, '甲' * 9001)]
    monkeypatch.setattr(c, 'generate_chat_completion', lambda *a, **k: pytest.fail('unexpected model call'))
    assert run(docs)['status'] == 'source_over_budget'
    assert store.summaries.count_documents({}) == 0
    for _ in range(30):
        message(store, '短句')
    assert c._select_compaction_batch('owner', 'ai_assistant_owner')['status'] == 'source_over_budget'


def test_prompt_rejects_overflow_instead_of_silently_truncating(store):
    with pytest.raises(ValueError, match='source_over_budget'):
        c._prompt_messages('owner', [message(store, '甲' * 9001)])


def test_exact_budget_and_owner_raw_content(store):
    doc = message(store, '顯示用文字')
    doc['metadata']['owner_raw_content'] = '甲' * 8998 + '更正'
    assert c._prompt_messages('owner', [doc])[0]['content'].endswith('更正')
    assert len(c._prompt_messages('owner', [doc])[0]['content']) == 9000


def test_empty_summary_is_retried_and_never_evaluated_or_stored(store, monkeypatch):
    calls = []
    def empty(prompt, **kwargs):
        calls.append(prompt)
        return SimpleNamespace(content=json.dumps(c.ConversationSummaryV1().model_dump()))
    monkeypatch.setattr(c, 'generate_chat_completion', empty)
    monkeypatch.setattr(c, '_evaluate_summary', lambda *a, **k: pytest.fail('empty summary must not reach evaluator'))
    result = run([message(store, '故事主角改為白鷺')])
    assert result == {'status': 'generation_failed', 'result_code': 'empty_summary'}
    assert len(calls) == 2 and store.summaries.count_documents({}) == 0
    assert store.runs.find_one({})['observability']['generation_result_code'] == 'empty_summary'


def test_failed_next_batch_preserves_previous_summary_and_watermark(store, monkeypatch):
    def good(prompt, **kwargs):
        return SimpleNamespace(content=json.dumps(c.ConversationSummaryV1(active_topics=['原有故事']).model_dump()))
    monkeypatch.setattr(c, 'generate_chat_completion', good)
    monkeypatch.setattr(c, '_evaluate_summary', approve)
    assert run([message(store, '原有故事')])['status'] == 'stored'
    previous = store.summaries.find_one({})
    monkeypatch.setattr(c, 'generate_chat_completion', lambda *a, **k: SimpleNamespace(content=json.dumps(c.ConversationSummaryV1().model_dump())))
    assert run([message(store, '更正故事')], previous['revision'], previous['source_hash'])['status'] == 'generation_failed'
    assert store.summaries.find_one({}) == previous


def test_excluded_only_batch_still_advances_without_generation(store, monkeypatch):
    monkeypatch.setattr(c, 'generate_chat_completion', lambda *a, **k: pytest.fail('excluded content must not reach model'))
    docs = [message(store, '私人資料' * 5000, 'no_memory')]
    assert run(docs)['status'] == 'stored'
    assert store.summaries.find_one({})['observability']['input_char_count'] == 0


def test_empty_generation_can_recover_once(store, monkeypatch):
    outputs = iter([c.ConversationSummaryV1(), c.ConversationSummaryV1(known_continuity=['角色為白鷺'])])
    monkeypatch.setattr(c, 'generate_chat_completion', lambda *a, **k: SimpleNamespace(content=json.dumps(next(outputs).model_dump())))
    monkeypatch.setattr(c, '_evaluate_summary', approve)
    assert run([message(store, '角色為白鷺')])['status'] == 'stored'
    saved = store.summaries.find_one({})
    assert saved['observability']['generation_attempt_count'] == 2
    assert saved['observability']['generation_result_code'] == 'success_after_retry'


def test_old_policy_summary_is_not_injected(store, monkeypatch):
    monkeypatch.setattr(c, '_generate_summary', lambda *a, **k: c.ConversationSummaryV1(known_continuity=['角色為白鷺']))
    monkeypatch.setattr(c, '_evaluate_summary', approve)
    assert run([message(store, '角色為白鷺')])['status'] == 'stored'
    assert c.load_validated_conversation_continuity('owner', 'ai_assistant_owner')
    store.summaries.update_one({}, {'$set': {'observability.policy_version': 'conversation_compaction_policy_v4'}})
    assert c.load_validated_conversation_continuity('owner', 'ai_assistant_owner') is None
