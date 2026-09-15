"""Regression for real system-event IDs interrupting recursive compaction."""
import json
from unittest.mock import Mock

import mongomock
import pytest
from bson import ObjectId
from pymongo.errors import AutoReconnect

from services import conversation_compaction_service as c
from services import conversation_summary_operations as ops
from services.ayue_agent.context import _message_is_after_watermark
from services.message_use_service import metadata_for_use

OWNER = 'owner'
ROOM = 'ai_assistant_owner'
SYSTEM = 'system-event:' + 'a' * 64


@pytest.fixture
def store(monkeypatch):
    db = mongomock.MongoClient().mixed_ids
    for name, collection in [('messages_coll', db.messages), ('CONVERSATION_COMPACTIONS', db.summaries),
                             ('CONVERSATION_COMPACTION_RUNS', db.runs)]:
        monkeypatch.setattr(c, name, collection)
    monkeypatch.setattr(ops, 'JOBS', db.jobs)
    monkeypatch.setattr(ops, '_scope', lambda owner, room: owner == OWNER and room == ROOM)
    monkeypatch.setenv('AYUE_CONVERSATION_COMPACTION_MODE', 'shadow')
    monkeypatch.setenv('AYUE_CONVERSATION_CONTEXT_MODE', 'on')
    monkeypatch.setenv('AYUE_CONVERSATION_CONTEXT_USER_ALLOWLIST', OWNER)
    monkeypatch.setattr(c, 'queue_profile_coverage', lambda *a, **k: {'status': 'ok', 'requeued_count': 0})
    monkeypatch.setattr(c, '_generate_summary', lambda *a, **k: c.ConversationSummaryV1(active_topics=['fiction']))
    monkeypatch.setattr(c, '_evaluate_summary', lambda *a, **k: c.ConversationCompactionEvaluationDecisionV1(
        retention=c.ContinuityRetentionV1(**{field: True for field in c.SUMMARY_FIELDS}),
        unsupported_content=False, role_confusion=False, canonical_state_leak=False, confidence=.95))
    return db


def add(db, key=None, at=1., ordinary=True):
    key = ObjectId() if key is None else key
    doc = {'_id': key, 'room_id': ROOM, 'sender_id': 'ai_assistant' if isinstance(key, str) else OWNER,
           'content': 'SYSTEM_PRIVATE_STATE' if isinstance(key, str) else 'fiction', 'timestamp': at,
           'metadata': {'message_use': metadata_for_use('ordinary')} if ordinary else {}}
    db.messages.insert_one(doc)
    return doc


@pytest.mark.parametrize('position', [0, 5, 10])
def test_system_event_at_any_batch_position_loads_and_advances(store, position):
    docs = [add(store, SYSTEM if i == position else None, at=float(i)) for i in range(31)]
    selected = c._select_compaction_batch(OWNER, ROOM)
    assert selected['status'] == 'ready'
    loaded = c._load_exact_batch(OWNER, ROOM, selected['message_ids'])
    assert [m['_id'] for m in loaded] == [m['_id'] for m in docs[:11]]
    assert 'SYSTEM_PRIVATE_STATE' not in json.dumps(c._prompt_messages(OWNER, loaded))
    result = c.run_conversation_compaction_shadow(OWNER, ROOM, selected['message_ids'], 0, None)
    assert result['status'] == 'stored'
    saved = c._load_current_compaction(OWNER, ROOM)
    assert saved['covered_through_message_id'] == str(docs[10]['_id'])
    assert c._validated_recursive_baseline(saved, OWNER, ROOM) is not None
    assert c.load_validated_conversation_continuity(OWNER, ROOM) is not None
    assert store.messages.count_documents({'room_id': ROOM, **c._message_query_after(saved)}) == 20
    assert c._select_compaction_batch(OWNER, ROOM)['status'] == 'below_threshold'


def test_excluded_system_only_batch_never_calls_model_and_keeps_progress(store, monkeypatch):
    docs = [add(store, 'system-event:' + f'{i:064x}', at=float(i), ordinary=False) for i in range(11)]
    monkeypatch.setattr(c, '_generate_summary', lambda *a, **k: pytest.fail('system events are not evidence'))
    result = c.run_conversation_compaction_shadow(OWNER, ROOM, [m['_id'] for m in docs], 0, None)
    assert result['status'] == 'stored'
    saved = c._load_current_compaction(OWNER, ROOM)
    assert not any(saved['summary'].values())
    assert c._validated_recursive_baseline(saved, OWNER, ROOM) is not None


@pytest.mark.parametrize('anchor_index', [0, 1, 2, 3])
def test_mixed_same_timestamp_watermark_matches_mongo_sort_and_context(store, anchor_index):
    ids = ['system-event:' + 'a' * 64, 'system-event:' + 'b' * 64,
           ObjectId('100000000000000000000000'), ObjectId('200000000000000000000000')]
    for key in reversed(ids): add(store, key, at=10.)
    add(store, at=11.)
    watermark = {'covered_through_timestamp': 10., 'covered_through_message_id': str(ids[anchor_index])}
    ordered = list(store.messages.find().sort([('timestamp', 1), ('_id', 1)]))
    assert [m['_id'] for m in ordered[:4]] == ids
    actual = list(store.messages.find(c._message_query_after(watermark)).sort([('timestamp', 1), ('_id', 1)]))
    expected = ordered[anchor_index + 1:]
    assert [m['_id'] for m in actual] == [m['_id'] for m in expected]
    assert [m['_id'] for m in ordered if _message_is_after_watermark(m, watermark)] == [m['_id'] for m in expected]


def test_missing_cross_room_and_wrong_sender_fail_closed(store):
    doc = add(store, SYSTEM)
    assert c._load_exact_batch(OWNER, ROOM + '_other', [SYSTEM]) == []
    assert c._load_exact_batch(OWNER, ROOM, [SYSTEM, str(ObjectId())]) == []
    store.messages.update_one({'_id': SYSTEM}, {'$set': {'sender_id': 'other'}})
    assert c._load_exact_batch(OWNER, ROOM, [SYSTEM]) == []


def test_invalid_id_is_not_misreported_as_storage_failure(store, monkeypatch):
    assert c.run_conversation_compaction_shadow(OWNER, ROOM, ['unsupported-format'], 0, None)['status'] == 'source_invalid_id'
    monkeypatch.setattr(c.messages_coll, 'find', Mock(side_effect=AutoReconnect('test')))
    assert c.run_conversation_compaction_shadow(OWNER, ROOM, [str(ObjectId())], 0, None)['status'] == 'source_unavailable'


def test_failed_mixed_job_recovers_and_future_turns_keep_compacting(store):
    for i in range(45): add(store, SYSTEM if i == 5 else None, at=float(i))
    assert ops.enqueue_rebuild(OWNER, ROOM)['queued']
    store.jobs.update_one({}, {'$set': {'state': 'failed', 'result_code': 'source_unavailable', 'attempts': 3}})
    assert ops.enqueue_rebuild(OWNER, ROOM, retry=True)['queued']
    for _ in range(6):
        store.jobs.update_one({}, {'$set': {'next_attempt_at': 0}})
        result = ops.run_rebuild_once()
        if result['status'] == 'complete': break
    assert result['status'] == 'complete'
    revision = store.summaries.find_one({})['revision']
    for i in range(45, 66): add(store, at=float(i))
    assert ops.enqueue_rebuild(OWNER, ROOM)['queued']
    assert ops.run_rebuild_once()['status'] == 'queued'
    assert store.summaries.find_one({})['revision'] > revision
    assert ops.summary_status(OWNER, ROOM)['injection_enabled']


def test_worker_blocks_unsupported_id_without_three_futile_retries(store):
    for i in range(31): add(store, 'unsupported-format' if i == 5 else None, at=float(i))
    assert ops.enqueue_rebuild(OWNER, ROOM)['queued']
    assert ops.run_rebuild_once() == {'status': 'blocked', 'result_code': 'source_invalid_id'}
    assert store.jobs.find_one({})['attempts'] == 1
