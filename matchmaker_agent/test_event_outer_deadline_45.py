"""Deterministic outer-deadline authority coverage; synthetic, no providers.

These tests pin the single Event outer-deadline authority to 45 s and replay the
Run #27 source-shaped d1387b80 flow (three sequential validator invocations with
observed durations and gaps). They prove:
  * the 38 s shape terminates on the outer deadline,
  * the 45 s budget lets the observed bounded flow finish,
  * an already-observed 6 s provider timeout stays a timeout,
  * an exhaustive provider failure still fails closed.
No test encodes an assumption that a timeout becomes a success.
"""
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from matchmaker_agent import event_v2_api
from matchmaker_agent.event_v2_api import EVENT_OUTER_DEADLINE_SECONDS, find_matches
from matchmaker_agent.event_v2_adapter import Adapter
from matchmaker_agent.event_v2_contract import EventUnavailable
from matchmaker_agent.related_interest_contract import RELATIONS
from matchmaker_agent.test_event_v2_adapter import (
    Graph, NOW, VECTOR, complete_decision_counts, enable_event_semantics, event, preference,
)

MODEL = 'deepseek-v4.1-flash:cloud'
EVENT_API_SOURCE = Path(event_v2_api.__file__)


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@pytest.fixture(autouse=True)
def threshold(monkeypatch):
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MIN_SIMILARITY', '.90')
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ENABLED', 'on')
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MODE', 'active')


def test_outer_deadline_authority_is_45_seconds():
    assert EVENT_OUTER_DEADLINE_SECONDS == 45.0
    source = EVENT_API_SOURCE.read_text(encoding='utf-8')
    assert 'deadline=time.monotonic()+EVENT_OUTER_DEADLINE_SECONDS' in source
    assert 'time.monotonic()+38' not in source


def test_outer_deadline_is_forwarded_to_the_adapter(monkeypatch):
    seen = {}

    @contextmanager
    def fake_session_for(_agent):
        yield object()

    class Inspect:
        def __init__(self, *_args, **kwargs):
            seen['deadline'] = kwargs['deadline']

        def select(self, *_args, **_kwargs):
            return []

        def telemetry(self):
            return {}

    monkeypatch.setattr(event_v2_api, 'session_for', fake_session_for)
    monkeypatch.setattr(event_v2_api, 'Adapter', Inspect)
    monkeypatch.setattr(event_v2_api.time, 'monotonic', lambda: 1000.0)
    find_matches(SimpleNamespace(client=None, model=MODEL), 'owner', [])
    assert seen['deadline'] == 1000.0 + 45.0


def _run27_observed_shape(outer):
    """Replay the observed d1387b80 invocation shape: request overhead 5.757 s,
    inv#1 3.912 s trusted rejects, inv#2 0.467 s gap + 12.016 s all-timeout,
    inv#3 1.142 s gap + min(18 s shared, outer remaining) exhausted."""
    clock = Clock()
    invocations = []

    def validate(_query, concepts, *_args, **_kwargs):
        index = len(invocations) + 1
        invocations.append(index)
        if index == 1:
            clock.advance(3.912)
            return [], complete_decision_counts('unrelated', len(concepts), accepted=0,
                rejected=len(concepts))
        if index == 2:
            clock.advance(0.467 + 12.016)
            return [], {'accepted': 0, 'rejected': 0, 'error': len(concepts),
                'attempts': 2, 'attempt_errors': 2, 'retries': 1}
        clock.advance(1.142)
        clock.advance(max(0.0, min(18.0, outer - clock())))
        return [], {'accepted': 0, 'rejected': 0, 'error': len(concepts),
            'attempts': 3, 'attempt_errors': 3, 'retries': 1}

    graph = Graph({'requester': [preference('打籃球')]}, [
        event(('三對三籃球',), eid='first'),
        event(('籃球訓練營',), eid='second'),
        event(('街頭籃球賽',), eid='third'),
    ])
    service = Adapter(graph, None, MODEL, deadline=outer, clock=clock, now=lambda: NOW,
        eligible=lambda _owner: True, allowed=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {s.source_hash: VECTOR[:] for s in signals},
        validate=validate)
    clock.advance(5.757)
    try:
        service.select('requester', [])
        outcome = 'completed'
    except EventUnavailable as exc:
        outcome = exc.code
    return outcome, clock(), invocations, service


def test_run27_shape_terminates_under_38s():
    outcome, elapsed, invocations, service = _run27_observed_shape(38.0)
    assert outcome == 'semantic_validator_unavailable'
    assert elapsed == 38.0
    assert len(invocations) == 3


def test_run27_shape_completes_under_45s():
    outcome, elapsed, invocations, service = _run27_observed_shape(45.0)
    assert outcome == 'completed'
    assert 41.0 <= elapsed <= 41.5
    assert len(invocations) == 3
    assert service.validator_counts['rejected'] > 0


def test_observed_six_second_timeout_stays_a_timeout(monkeypatch, tmp_path):
    enable_event_semantics(monkeypatch, tmp_path)
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MIN_SIMILARITY', '.90')
    clock = Clock()
    attempts = []

    def fixed_six_second_timeout(_client, *, timeout, model, **_request):
        attempts.append(timeout)
        clock.advance(timeout)
        raise TimeoutError('synthetic transport delay')

    monkeypatch.setattr('matchmaker_agent.related_interest_validator._completion',
        fixed_six_second_timeout)
    service = Adapter(Graph({'requester': [preference('打籃球')]},
        [event(('三對三籃球',))]), object(), MODEL, deadline=45.0, clock=clock,
        now=lambda: NOW, eligible=lambda _owner: True, allowed=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {s.source_hash: VECTOR[:] for s in signals},
        fallback_model='glm-5.3-flash:cloud')
    with pytest.raises(EventUnavailable, match='semantic_validator_unavailable'):
        service.relevance('requester')
    assert attempts and all(timeout == 6.0 for timeout in attempts)
    assert clock() > 0.0


def test_validator_shared_budget_remains_18s_under_45s_outer(monkeypatch, tmp_path):
    enable_event_semantics(monkeypatch, tmp_path)
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MIN_SIMILARITY', '.90')
    seen = []

    def validate(_query, concepts, *_args, **kwargs):
        seen.append(kwargs)
        return [], complete_decision_counts('unrelated', len(concepts), accepted=0,
            rejected=len(concepts))

    service = Adapter(Graph({'requester': [preference('打籃球')]}, [event(('籃球',))]),
        None, MODEL, deadline=45.0, clock=lambda: 0.0, now=lambda: NOW,
        eligible=lambda _owner: True, allowed=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {s.source_hash: VECTOR[:] for s in signals},
        validate=validate)
    service.relevance('requester')
    assert seen and seen[0]['deadline'] == 18.0


def test_genuine_full_provider_failure_still_fails_closed(monkeypatch, tmp_path):
    enable_event_semantics(monkeypatch, tmp_path)
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MIN_SIMILARITY', '.90')
    attempts = []

    def dead_provider(_client, *, timeout, model, **_request):
        attempts.append((timeout, model))
        raise ConnectionError('synthetic outage')

    monkeypatch.setattr('matchmaker_agent.related_interest_validator._completion', dead_provider)
    service = Adapter(Graph({'requester': [preference('打籃球')]}, [event(('籃球',))]),
        object(), MODEL, deadline=45.0, clock=Clock(), now=lambda: NOW,
        eligible=lambda _owner: True, allowed=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {s.source_hash: VECTOR[:] for s in signals},
        fallback_model='glm-5.3-flash:cloud')
    with pytest.raises(EventUnavailable, match='semantic_validator_unavailable'):
        service.relevance('requester')
    assert 1 <= len(attempts) <= 2
    assert all(timeout <= 6.0 for timeout, _ in attempts)
    assert len({model for _, model in attempts}) <= 2


def test_systemic_job_unavailable_stays_breaker_accountable(monkeypatch, tmp_path):
    """A provider 429/5xx exhaustion must still produce the terminal typed
    unavailable that the breaker counts - not a silent completion."""
    import httpx
    from openai import APIStatusError
    from matchmaker_agent.related_interest_contract import POLICY

    enable_event_semantics(monkeypatch, tmp_path)

    def unavailable_503(_client, *, timeout, model, **_request):
        raise APIStatusError('synthetic', response=httpx.Response(503,
            request=httpx.Request('POST', 'http://provider.invalid/v1')), body=None)

    monkeypatch.setattr('matchmaker_agent.related_interest_validator._completion', unavailable_503)
    service = Adapter(Graph({'requester': [preference('打籃球')]}, [event(('籃球',))]),
        object(), MODEL, deadline=45.0, clock=Clock(), now=lambda: NOW,
        eligible=lambda _owner: True, allowed=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {s.source_hash: VECTOR[:] for s in signals},
        fallback_model='glm-5.3-flash:cloud')
    with pytest.raises(EventUnavailable, match='semantic_validator_unavailable'):
        service.relevance('requester')


def test_partial_error_with_trusted_decision_does_not_fake_terminal_unavailable(monkeypatch, tmp_path):
    """Existing partial-failure semantics: one errored invocation alongside a
    trusted REJECT prefix must not be reported as a job-level outage."""
    enable_event_semantics(monkeypatch, tmp_path)
    calls = []

    def mixed(_query, concepts, *_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            return [], complete_decision_counts('unrelated', len(concepts), accepted=0,
                rejected=len(concepts))
        return [], {'accepted': 0, 'rejected': 0, 'error': len(concepts),
            'attempts': 2, 'attempt_errors': 2, 'retries': 1}

    service = Adapter(Graph({'requester': [preference('打籃球')]},
        [event(('三對三籃球',), eid='first'), event(('籃球訓練營',), eid='second')]),
        None, MODEL, deadline=45.0, clock=Clock(), now=lambda: NOW,
        eligible=lambda _owner: True, allowed=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {s.source_hash: VECTOR[:] for s in signals},
        validate=mixed)
    assert service.select('requester', []) == []
    assert len(calls) == 2


# Recorded Run #27 shapes (request-relative seconds; see the 2026-10-06 telemetry
# capture). Each invocation is (concepts, [ (kind, seconds_or_truncated_seconds) ])
# where kind is 'ok' (trusted decision) or 'timeout' (provider stall).
_RUN27_SHAPES = {
    'd1387b80': {
        'start': 5.757,
        'gaps': [0.467, 1.142],
        'invocations': [
            (3, [('ok', 2.102), ('ok', 1.809)]),
            (2, [('timeout', 6.008), ('timeout', 6.008)]),
            (4, [('timeout', 6.008), ('timeout', 6.009), ('truncated', 2.692)]),
        ],
        'elapsed_45': 41.28,
    },
    '2a29cad1': {
        'start': 6.170,
        'gaps': [0.508, 0.708, 0.669, 0.124, 0.193],
        'invocations': [
            (2, [('ok', 1.641)]),
            (3, [('ok', 5.891), ('ok', 0.934)]),
            (1, [('ok', 1.742)]),
            (5, [('ok', 4.108), ('ok', 1.723), ('ok', 0.819)]),
            (2, [('timeout', 6.002), ('timeout', 6.007)]),
            (1, [('truncated', 0.763)]),
        ],
        'elapsed_45': 43.23,
    },
    '98cda519': {
        'start': 6.001,
        'gaps': [0.804, 0.381, 0.124, 0.228, 0.179],
        'invocations': [
            (1, [('ok', 0.805)]),
            (2, [('ok', 2.687)]),
            (1, [('ok', 1.789)]),
            (3, [('ok', 1.283), ('ok', 0.903)]),
            (5, [('timeout', 6.008), ('timeout', 6.007), ('ok', 1.864), ('ok', 0.843)]),
            (2, [('timeout', 6.007), ('truncated', 2.098)]),
        ],
        'elapsed_45': 41.89,
    },
}


def _replay_recorded(shape, outer, request_id):
    """Replay one recorded shape. Truncated attempts are UNKNOWN beyond their
    observed cut, so they pessimistically consume the remaining shared budget
    and stay timeouts; they are never converted into successes."""
    clock = Clock()
    invocations = []

    def validate(_query, concepts, *_args, deadline, **_kwargs):
        index = len(invocations) + 1
        invocations.append(index)
        spec = shape['invocations'][index - 1]
        accepted = rejected = error = 0
        for kind, seconds in spec[1]:
            if kind == 'ok':
                clock.advance(seconds)
                rejected += 2
            elif kind == 'timeout':
                clock.advance(min(6.0, seconds))
                error += 2
            else:  # truncated: unknown beyond the observed point, fail closed
                clock.advance(max(0.0, min(6.0, deadline - clock())))
                error += 1
                break
        if clock() >= deadline:
            error = max(error, 1)
        counts = {'accepted': accepted, 'rejected': rejected, 'error': error,
            'attempts': len(spec[1]), 'attempt_errors': (len(spec[1]) if error else 0),
            'retries': 0}
        counts.update({name: 0 for name in RELATIONS})
        return [], counts

    events = [event((f'{request_id}-活動{index}',), eid=f'{request_id}-event-{index}')
        for index in range(len(shape['invocations']))]
    service = Adapter(Graph({'requester': [preference('打籃球')]}, events), None, MODEL,
        deadline=outer, clock=clock, now=lambda: NOW, eligible=lambda _owner: True,
        allowed=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {s.source_hash: VECTOR[:] for s in signals},
        validate=validate)
    clock.advance(shape['start'])
    for gap in shape['gaps']:
        clock.advance(gap)
    try:
        service.select('requester', [])
        outcome = 'completed'
    except EventUnavailable as exc:
        outcome = exc.code
    return outcome, clock(), invocations


@pytest.mark.parametrize('request_id', sorted(_RUN27_SHAPES))
def test_recorded_shape_terminates_under_38s(request_id):
    shape = _RUN27_SHAPES[request_id]
    outcome, _elapsed, invocations = _replay_recorded(shape, 38.0, request_id)
    assert outcome == 'semantic_validator_unavailable'
    assert invocations, 'the recorded flow must reach the validator'


@pytest.mark.parametrize('request_id', sorted(_RUN27_SHAPES))
def test_recorded_shape_finishes_under_45s(request_id):
    shape = _RUN27_SHAPES[request_id]
    outcome, elapsed, invocations = _replay_recorded(shape, 45.0, request_id)
    assert outcome == 'completed'
    assert shape['elapsed_45'] - 0.5 <= elapsed <= shape['elapsed_45'] + 0.5
    assert elapsed < 45.0
