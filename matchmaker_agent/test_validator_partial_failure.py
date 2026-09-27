"""Deterministic validator -> retrieval coverage; no providers or databases."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

import related_interest_validator as validator
from related_interest_retrieval import retrieve
from related_interest_contract import INDEX_NAME, embedding_fingerprint, validated_evidence
from concept_identity import canonicalize_concept
from agent_api import RelatedInterestCandidateRequest

MODEL = 'models/gemini-embedding-2'
VECTOR = [1.0] + [0.0]*767


def completion(*relations, model='deepseek-test', usage=None):
    return SimpleNamespace(model=model, usage=usage, choices=[SimpleNamespace(finish_reason='stop',
        message=SimpleNamespace(content=json.dumps({'results': [
            {'id': str(i), 'relation': r} for i, r in enumerate(relations)]})))])


class Result(list):
    def single(self):
        return self[0] if self else None


@pytest.fixture(params=['canary', 'enabled_accounts'])
def runtime(request, monkeypatch, tmp_path):
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ENABLED', 'on')
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MODE', 'active')
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ROLLOUT_MODE', request.param)
    monkeypatch.setenv('MATCH_RELATED_INTEREST_CANARY_USER_IDS', '["owner","person"]')
    path = tmp_path/'kill'
    monkeypatch.setenv('MATCH_RELATED_INTEREST_KILL_SWITCH_FILE', str(path))
    return path


def run_retrieval(monkeypatch, runtime, outcomes, *, count=8, now=None):
    now = now if now is not None else [0.0]
    query = canonicalize_concept('Watching football')
    identities = [canonicalize_concept(f'Playing sport {i}') for i in range(count)]
    by_key = {c.key: c for c in identities}
    fp = embedding_fingerprint(MODEL)
    expanded, timeouts = [], []

    def record(c):
        return {**c.as_dict(), 'vector': VECTOR, 'fingerprint': fp,
            'source_hash': c.semantic_input_hash, 'provenance': 'a'*64, 'similarity': .91}

    class Session:
        def run(self, statement, **params):
            text = str(statement)
            assert not any(word in text for word in ('MERGE ', ' SET ', 'DELETE ', 'CREATE '))
            if 'SHOW VECTOR INDEXES' in text:
                return Result([{'state': 'ONLINE', 'labelsOrTypes': ['Concept'], 'properties': ['embedding_v2'],
                    'options': {'indexConfig': {'vector.dimensions': 768, 'vector.similarity_function': 'cosine'}}}])
            if 'SHOW INDEXES' in text:
                return Result([{'count': 1, 'user_count': 1}])
            if 'db.index.vector.queryNodes' in text:
                assert params['index_name'] == INDEX_NAME
                return Result([record(c) for c in identities])
            if 'UNWIND $concepts' in text:
                expanded.extend(params['concepts'])
                return Result([{'candidate_id': 'person', 'evidence': [
                    {'concept_key': c['concept_key']} for c in params['concepts']]}])
            if 'type(r) AS polarity' in text:
                return Result([{**record(by_key[params['key']]), 'polarity': 'PREFERS'}])
            raise AssertionError('unexpected graph query')

    pending = iter(outcomes)
    def complete(_client, *, timeout, **_request):
        timeouts.append(timeout)
        outcome = next(pending)
        if callable(outcome):
            return outcome()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(validator, '_completion', complete)
    req = RelatedInterestCandidateRequest(requester_user_id='owner', topic=query.semantic_text,
        embedding_model=MODEL, embedding_fingerprint=fp, query_embedding=VECTOR)
    checks = Mock(unavailable=False); checks.check.return_value = True
    result = retrieve(Session(), req, query, Mock(), 'deepseek-test', MODEL, clock=lambda: now[0],
        eligibility_factory=lambda *_a, **_kw: checks)
    return result, identities, expanded, timeouts


@pytest.mark.parametrize('error_batch', [0, 1, 3])
def test_six_reject_two_error_is_normal_no_match(monkeypatch, runtime, error_batch):
    outcomes = []
    for i in range(4):
        outcomes.extend([completion('invalid', 'invalid')]*2 if i == error_batch
                        else [completion('unrelated', 'unrelated')])
    result, _, expanded, timeouts = run_retrieval(monkeypatch, runtime, outcomes)
    assert result['status'] == 'success' and 'error_code' not in result
    assert result['candidate_count'] == 0 and result['candidates'] == []
    assert result['semantic_concepts_considered'] == [] and not expanded
    assert result['validator_counts']['rejected'] == result['validator_counts']['unrelated'] == 6
    assert result['validator_counts']['error'] == 2
    assert result['validator_counts']['retries'] == 1 and len(timeouts) == 5


def test_two_accept_two_error_only_accepted_concepts_expand(monkeypatch, runtime):
    result, concepts, expanded, _ = run_retrieval(monkeypatch, runtime, [
        completion('candidate_more_specific', 'role_mismatch'),
        completion('invalid', 'invalid'), completion('invalid', 'invalid')], count=4)
    allowed = {c.key for c in concepts[:2]}
    assert result['status'] == 'success'
    assert {c['concept_key'] for c in expanded} == allowed
    packets = result['candidates'][0]['evidence']
    assert {e['concept_key'] for e in packets} == allowed
    assert all(validated_evidence(e) for e in packets)
    assert {c.key for c in concepts[2:]}.isdisjoint(e['concept_key'] for e in packets)
    assert result['validator_counts']['accepted'] == result['validator_counts']['error'] == 2


def test_zero_valid_decisions_is_unavailable(monkeypatch, runtime):
    result, _, expanded, _ = run_retrieval(monkeypatch, runtime,
        [completion('invalid', 'invalid')]*4, count=4)
    assert result['error_code'] == 'semantic_validator_unavailable'
    assert result['validator_counts']['error'] == 4 and not expanded and result['candidates'] == []


def provider_error(code):
    request = httpx.Request('POST', 'http://provider.invalid/v1')
    return APIStatusError('SECRET provider body', response=httpx.Response(code, request=request), body={'private': 'SECRET'})


@pytest.mark.parametrize('prefix', ['unrelated', 'candidate_more_specific'])
@pytest.mark.parametrize('failure', [401, 403, 429, 503, 'connection', 'wrong_model'])
def test_persistent_systemic_failure_overrides_partial_decisions(monkeypatch, runtime, prefix, failure):
    error = (APIConnectionError(request=httpx.Request('POST', 'http://provider.invalid/v1')) if failure == 'connection'
        else completion('unrelated', 'unrelated', model='wrong-provider') if failure == 'wrong_model'
        else provider_error(failure))
    result, _, expanded, timeouts = run_retrieval(monkeypatch, runtime,
        [completion(prefix, prefix), error, error], count=4)
    assert result['error_code'] == 'semantic_validator_unavailable' and not expanded
    assert result['candidates'] == [] and result['validator_counts']['job_unavailable'] == 1
    assert len(timeouts) == 3 and 'SECRET' not in str(result)


def test_systemic_provider_unavailable_without_any_decision(monkeypatch, runtime):
    result, _, expanded, _ = run_retrieval(monkeypatch, runtime, [provider_error(503)]*2, count=4)
    assert result['error_code'] == 'semantic_validator_unavailable' and not expanded
    assert result['validator_counts']['error'] == 4


def test_existing_single_retry_can_recover_provider_failure(monkeypatch, runtime):
    result, _, expanded, timeouts = run_retrieval(monkeypatch, runtime,
        [provider_error(503), completion('unrelated', 'unrelated')], count=2)
    assert result['status'] == 'success' and not expanded
    assert result['validator_counts']['retries'] == 1 and timeouts == [6.0, 6.0]
    assert not result['validator_counts'].get('job_unavailable')


@pytest.mark.parametrize('prefix', ['unrelated', 'candidate_more_specific'])
def test_accounting_failure_stays_job_wide_after_valid_decisions(monkeypatch, runtime, prefix):
    from agent_quota import service
    recorder = Mock(side_effect=RuntimeError('SECRET storage failure'))
    monkeypatch.setattr(service, 'record_usage_deferred', recorder)
    result, _, expanded, timeouts = run_retrieval(monkeypatch, runtime, [completion(prefix, prefix),
        completion('unrelated', 'unrelated', usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20))], count=4)
    assert result['error_code'] == 'semantic_validator_unavailable' and not expanded
    assert result['validator_counts']['job_unavailable'] == 1
    assert len(timeouts) == 2 and recorder.call_count == 1  # No paid retry of failed accounting.
    assert 'SECRET' not in str(result)


@pytest.mark.parametrize('timeout_error', [TimeoutError, APITimeoutError])
def test_per_attempt_timeout_is_local_with_trusted_rejects(monkeypatch, runtime, timeout_error):
    error = timeout_error() if timeout_error is TimeoutError else timeout_error(request=httpx.Request('POST', 'http://provider.invalid/v1'))
    result, _, expanded, _ = run_retrieval(monkeypatch, runtime,
        [completion('unrelated', 'unrelated'), error, error], count=4)
    assert result['status'] == 'success' and not expanded
    assert result['validator_counts']['rejected'] == result['validator_counts']['error'] == 2


@pytest.mark.parametrize('elapsed,expected', [(18.0, 'success'), (27.0, 'error')])
def test_partial_deadline_result_must_still_fit_request_budget(monkeypatch, runtime, elapsed, expected):
    now = [0.0]
    def expires():
        now[0] = elapsed
        raise TimeoutError()
    result, _, expanded, timeouts = run_retrieval(monkeypatch, runtime,
        [completion('unrelated', 'unrelated')]*3+[expires], now=now)
    assert result['status'] == expected and not expanded
    assert result['validator_counts']['rejected'] == 6 and result['validator_counts']['error'] == 2
    assert len(timeouts) == 4 and result['validator_counts']['retries'] == 0
    if expected == 'error':assert result['error_code'] == 'semantic_validator_unavailable'


def test_deadline_without_trusted_result_and_late_accept_are_unavailable(monkeypatch, runtime):
    now = [0.0]
    def late_accept():
        now[0] = 18.0
        return completion('candidate_more_specific', 'role_mismatch')
    result, _, expanded, _ = run_retrieval(monkeypatch, runtime, [late_accept], count=4, now=now)
    assert result['error_code'] == 'semantic_validator_unavailable' and not expanded
    assert result['validator_counts']['accepted'] == 0 and result['validator_counts']['error'] == 4


def test_kill_after_valid_batch_still_drops_everything(monkeypatch, runtime):
    def kill():
        runtime.touch()
        return completion('candidate_more_specific', 'role_mismatch')
    result, _, expanded, timeouts = run_retrieval(monkeypatch, runtime, [kill], count=4)
    assert result['error_code'] == 'semantic_policy_disabled' and not expanded and len(timeouts) == 1


def test_no_ann_concepts_is_not_validator_unavailability(monkeypatch, runtime):
    result, _, expanded, timeouts = run_retrieval(monkeypatch, runtime, [], count=0)
    assert result['status'] == 'success' and not expanded and not timeouts
