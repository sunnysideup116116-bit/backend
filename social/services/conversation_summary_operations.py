"""Durable summary maintenance and explicit operator rollout approval.

Public reads never approve rollout or enqueue work. No raw messages, summaries,
credentials or model prompts are stored in jobs or approval records.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import inspect
import os
import threading
import time
import uuid

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from database import db
from services import conversation_compaction_service as c
from services.ai_service import _resolve_chat_model
from services.codex_chat_provider import selected_provider
from services.ai_room_service import get_room

JOBS = db['conversation_summary_jobs']
ROLLOUTS = db['conversation_summary_rollouts']
_stop = threading.Event()
_thread = None
_lock = threading.Lock()


@lru_cache(maxsize=1)
def algorithm_fingerprint():
    functions = [c._reusable_message_text, c._complete_message_prefix, c._prompt_messages,
                 c._generate_summary, c._evaluate_summary, c._evaluation_projection, c.ConversationSummaryV1]
    return hashlib.sha256('\n'.join(inspect.getsource(f) for f in functions).encode()).hexdigest()


def rollout_decision():
    """None preserves legacy readiness behavior; explicit pause overrides it."""
    record = ROLLOUTS.find_one({'_id': c.COMPACTION_POLICY_VERSION})
    if not record:
        return None
    return bool(record.get('state') == 'approved'
                and record.get('algorithm_sha256') == algorithm_fingerprint()
                and record.get('provider') == selected_provider()
                and record.get('model') == _resolve_chat_model())


def rollout_status():
    record = ROLLOUTS.find_one({'_id': c.COMPACTION_POLICY_VERSION}) or {}
    metrics = c.conversation_compaction_rollout_readiness()
    reasons = metrics.get('reason_codes') or []
    health = ('unavailable' if metrics['status'] == 'storage_unavailable' else
              'insufficient_data' if 'insufficient_samples' in reasons else
              'degraded' if any(r != 'stale_metrics' for r in reasons) else
              'stale' if reasons else 'healthy')
    return {'policy': c.COMPACTION_POLICY_VERSION, 'approval_state': record.get('state', 'unapproved'),
            'approval_valid': rollout_decision() is True, 'health': health,
            'approval_basis': record.get('basis'), 'approved_at': record.get('approved_at'),
            'metrics': metrics}


def monitor_rollout():
    """Pause on sufficient real evidence of poor quality; inactivity is not failure."""
    status = rollout_status()
    if status['approval_state'] == 'approved' and status['health'] == 'degraded':
        ROLLOUTS.update_one({'_id': c.COMPACTION_POLICY_VERSION, 'state': 'approved'},
            {'$set': {'state': 'paused', 'pause_reason': 'quality_regression', 'updated_at': time.time()}})
    return status


def approve_benchmark(report, digest, *, apply=True):
    """Operator-only CLI entry; validate evidence, not just a caller's success flag."""
    rows = report.get('cases') or []
    if (report.get('version') != 'summary-benchmark-v1' or report.get('synthetic') is not True
            or report.get('production_writes') is not False
            or report.get('policy') != c.COMPACTION_POLICY_VERSION
            or report.get('model') != _resolve_chat_model()
            or report.get('provider') != selected_provider()
            or report.get('algorithm_sha256') != algorithm_fingerprint()
            or not report.get('finished_at') or not 0 <= time.time() - report['finished_at'] <= 7*86400
            or len(rows) < 50 or len({r.get('name') for r in rows}) != len(rows)
            or len({r.get('family') for r in rows}) < 10):
        raise ValueError('benchmark_identity_or_coverage_invalid')
    passes = sum(r.get('passed') is True and r.get('content_pass') is True
                 and (r.get('evaluation') or {}).get('status') == 'pass' for r in rows)
    unavailable = sum(not r.get('evaluation') for r in rows)
    reviews = sum((r.get('evaluation') or {}).get('status') == 'review' for r in rows)
    critical = any(r.get('leaked') or ((r.get('evaluation') or {}).get('status') == 'pass'
                                      and r.get('content_pass') is not True) for r in rows)
    if passes/len(rows) < .95 or unavailable/len(rows) > .02 or reviews/len(rows) > .05 or critical:
        raise ValueError('benchmark_quality_not_ready')
    if not apply:
        return {'validated': True, 'sample_count': len(rows), 'pass_count': passes}
    now = time.time()
    ROLLOUTS.update_one({'_id': c.COMPACTION_POLICY_VERSION}, {'$set': {
        'state': 'approved', 'basis': 'operator_reviewed_synthetic_benchmark',
        'algorithm_sha256': report['algorithm_sha256'], 'model': report['model'], 'provider': report['provider'],
        'report_sha256': digest, 'sample_count': len(rows), 'pass_count': passes,
        'approved_at': now, 'updated_at': now}}, upsert=True)


def pause_rollout():
    ROLLOUTS.update_one({'_id': c.COMPACTION_POLICY_VERSION}, {'$set': {
        'state': 'paused', 'pause_reason': 'operator', 'updated_at': time.time()}}, upsert=True)


def _key(owner, room):
    return hashlib.sha256(f'{c.COMPACTION_POLICY_VERSION}:{owner}:{room}'.encode()).hexdigest()


def _scope(owner, room):
    if not owner or not room or room.endswith('::match_hub') or '::proposal::' in room:
        return False
    if room == c._public_room_id(owner):
        return True
    # get_room verifies the embedded owner; storage errors must remain retryable.
    return get_room(room, owner) is not None


def summary_status(owner, room):
    if not _scope(owner, room):
        raise PermissionError('summary_room_not_owned')
    record = c._load_current_compaction(owner, room)
    valid = c._validated_recursive_baseline(record, owner, room)
    has_content = bool(valid and any(valid.summary.model_dump().values()))
    query = {'room_id': room, **c._message_query_after(valid.model_dump() if valid else None)}
    pending = c.messages_coll.count_documents(query, maxTimeMS=5000)
    job = JOBS.find_one({'_id': _key(owner, room)}) or {}
    job_state = job.get('state', 'idle')
    enabled = c.conversation_context_enabled_for_user(owner)
    if job_state in {'queued', 'running', 'retry_wait', 'blocked', 'failed'}:
        state = job_state
    elif pending > c.COMPACTION_SOFT_MESSAGE_LIMIT:
        state = 'rebuild_needed'
    else:
        state = 'ready' if has_content else 'not_needed'
    return {'policy': c.COMPACTION_POLICY_VERSION, 'state': state, 'rebuild_state': job_state,
            'summary_available': has_content, 'injection_enabled': bool(enabled and has_content and c.conversation_compaction_mode() == 'shadow'),
            'pending_message_count': pending, 'processed_batches': job.get('batches', 0),
            'last_result_code': job.get('result_code'), 'updated_at': job.get('updated_at')}


def enqueue_rebuild(owner, room, *, retry=False):
    if not _scope(owner, room):
        raise PermissionError('summary_room_not_owned')
    if c.conversation_compaction_mode() != 'shadow':
        return {'status': 'disabled', 'queued': False}
    key = _key(owner, room)
    existing = JOBS.find_one({'_id': key}) or {}
    active = {'queued', 'running', 'retry_wait'}
    if existing.get('state') in active or (not retry and existing.get('state') in {'blocked', 'failed'}):
        return {'status': existing['state'], 'queued': existing['state'] in active}
    selected = c._select_compaction_batch(owner, room)
    if selected.get('status') not in {'ready', 'source_over_budget'}:
        return {'status': selected.get('status', 'unavailable'), 'queued': False}
    state = 'blocked' if selected['status'] == 'source_over_budget' else 'queued'
    guard = {'_id': key, 'state': {'$nin': list(active)}}
    try:
        JOBS.update_one(guard, {'$set': {'owner': owner, 'room': room,
            'policy': c.COMPACTION_POLICY_VERSION, 'state': state, 'batches': 0, 'attempts': 0,
            'next_attempt_at': time.time(), 'updated_at': time.time(),
            'result_code': selected['status']}, '$unset': {'token': '', 'lease_until': ''}}, upsert=True)
    except DuplicateKeyError:
        return {'status': 'already_queued', 'queued': True}
    return {'status': state, 'queued': state == 'queued'}


def run_rebuild_once():
    if c.conversation_compaction_mode() != 'shadow':
        return {'status': 'disabled'}
    now = time.time()
    token = uuid.uuid4().hex
    job = JOBS.find_one_and_update({'policy': c.COMPACTION_POLICY_VERSION, '$or': [
        {'state': {'$in': ['queued', 'retry_wait']}, 'next_attempt_at': {'$lte': now}},
        {'state': 'running', 'lease_until': {'$lte': now}}]},
        {'$set': {'state': 'running', 'token': token, 'lease_until': now+300, 'updated_at': now}},
        sort=[('updated_at', 1)], return_document=ReturnDocument.AFTER)
    if not job:
        return {'status': 'idle'}
    guard = {'_id': job['_id'], 'token': token}
    renewal_stop = threading.Event()
    def renew():
        while not renewal_stop.wait(30):
            try:
                JOBS.update_one(guard, {'$set': {'lease_until': time.time()+300}})
            except Exception:
                return
    renewal = threading.Thread(target=renew, daemon=True)
    renewal.start()
    try:
        if not _scope(job['owner'], job['room']):
            result = {'status': 'invalid_scope'}
        else:
            # Preserve the existing profile-coverage-before-compaction order.
            tasks = []
            class Collector:
                def add_task(self, function, *args, **kwargs):
                    tasks.append((function, args, kwargs))
            queued = c.queue_conversation_compaction_shadow(Collector(), job['owner'], job['room'])
            result = {'status': queued['status']}
            for function, args, kwargs in tasks:
                returned = function(*args, **kwargs)
                if function is c.run_conversation_compaction_shadow:
                    result = returned
    except Exception:
        result = {'status': 'worker_unavailable'}
    finally:
        renewal_stop.set()
        renewal.join(timeout=1)
    code = str(result.get('status') or 'worker_unavailable')
    batches = int(job.get('batches', 0)) + int(code in {'stored', 'unchanged'})
    attempts = int(job.get('attempts', 0)) + 1
    if code in {'stored', 'unchanged', 'stale', 'source_changed'}:
        state = 'queued' if batches < 50 else 'blocked'
        attempts = 0 if code in {'stored', 'unchanged'} else attempts
        if attempts >= 3:
            state = 'failed'
        if batches >= 50:
            code = 'batch_budget_reached'
    elif code == 'below_threshold':
        state = 'complete'
    elif code in {'source_over_budget', 'invalid_scope', 'disabled'}:
        state = 'blocked'
    else:
        state = 'retry_wait' if attempts < 3 else 'failed'
    JOBS.update_one(guard, {'$set': {'state': state, 'batches': batches, 'attempts': attempts,
        'result_code': code, 'next_attempt_at': time.time() + (30*attempts if state == 'retry_wait' else 2),
        'updated_at': time.time()}, '$unset': {'token': '', 'lease_until': ''}})
    return {'status': state, 'result_code': code}


def _loop():
    while not _stop.wait(10):
        try:
            monitor_rollout()
            run_rebuild_once()
        except Exception:
            # No raw exception or owner data in diagnostics.
            print('[SUMMARY_WORKER] storage_or_provider_unavailable', flush=True)


def start_summary_worker():
    global _thread
    if os.getenv('AYUE_SUMMARY_REBUILD_WORKER_ENABLED', 'on').lower() not in {'on', 'true', '1'}:
        return
    with _lock:
        if _thread and _thread.is_alive():
            return
        try:
            JOBS.create_index([('policy', 1), ('state', 1), ('next_attempt_at', 1)])
        except Exception:
            # Keep process startup available; the consumer retries storage later.
            print('[SUMMARY_WORKER] index_unavailable', flush=True)
        _stop.clear()
        _thread = threading.Thread(target=_loop, name='summary-rebuild', daemon=True)
        _thread.start()


def stop_summary_worker():
    _stop.set()
    if _thread:
        _thread.join(timeout=3)
