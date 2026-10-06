"""Deterministic outer-deadline authority coverage; synthetic, no providers.

Pins the single Event outer-deadline authority to 75 s and replays the recorded
Run #28 termination shapes:
  * 45 s reproduces the three observed premature outer-deadline terminations,
  * 60 s still cannot complete the worst recorded request (5cc09a5deeb3, whose
    pessimistic observed prefix needs ~61 s),
  * 75 s clears all three historical terminations.

The replay is deliberately pessimistic: observed 6 s timeouts stay timeouts,
observed secondary timeouts stay timeouts, and attempts that the old 45 s budget
skipped are re-run and fail at full budget. No historical timeout is converted
into a synthetic success. Also pins the unchanged 18 s / 6 s / 2-attempt
contract, and that a true provider outage still fails closed.
"""
import math
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from matchmaker_agent import event_v2_api
from matchmaker_agent.event_v2_api import EVENT_OUTER_DEADLINE_SECONDS, find_matches
from matchmaker_agent.event_v2_adapter import Adapter
from matchmaker_agent.event_v2_contract import EventUnavailable
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


def test_outer_deadline_authority_is_75_seconds():
    assert EVENT_OUTER_DEADLINE_SECONDS == 75.0
    source = EVENT_API_SOURCE.read_text(encoding='utf-8')
    assert 'deadline=time.monotonic()+EVENT_OUTER_DEADLINE_SECONDS' in source
    assert 'time.monotonic()+45' not in source
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
    assert seen['deadline'] == 1000.0 + 75.0


# ---------------------------------------------------------------------------
# Run #28 recorded shapes (source: run #28 telemetry, 2026-10-06).
# Each invocation is (concept_count, batches) where batches is a list of
# observed attempts: ('ok', seconds) trusted decision | ('to', seconds|None)
# observed provider timeout (None = truncated by the old 45 s outer budget) |
# ('skip',) attempt never started because the shared/outer budget ran out.
# Every non-'ok' attempt replays pessimistically as a full-budget failure.
# ---------------------------------------------------------------------------

_RUN28_SHAPES = {
    '5cc09a5deeb3': {
        'start': 6.09,
        'gaps': [0.51, 0.70, 0.67, 0.64, 0.12],
        'invocations': [
            (2, [[('ok', 2.370)]]),
            (3, [[('ok', 2.489)], [('ok', 1.177)]]),
            (1, [[('ok', 2.605)]]),
            (5, [[('to', 6.008)], [('to', 6.008)], [('ok', 4.040)], [('ok', 0.883)]]),
            (4, [[('ok', 5.993)], [('ok', 2.661)]]),
            (3, [[('to', 2.056), ('skip',)], [('skip',)]]),
        ],
    },
    '759726da371f': {
        'start': 5.74,
        'gaps': [0.76, 0.95, 0.62],
        'invocations': [
            (3, [[('ok', 1.745)], [('ok', 3.561)]]),
            (2, [[('to', 6.007)], [('to', 6.007)]]),
            (4, [[('to', 6.008)], [('to', 6.007)], [('to', 5.993), ('skip',)]]),
            (1, [[('to', 1.616), ('skip',)]]),
        ],
    },
    'adeb6548740c': {
        'start': 5.68,
        'gaps': [0.0, 0.51, 0.0, 0.01, 0.0, 0.29],
        'invocations': [
            (2, [[('ok', 1.302)]]),
            (2, [[('ok', 3.381)]]),
            (2, [[('to', 6.008)], [('to', 6.007)]]),
            (1, [[('ok', 2.220)]]),
            (3, [[('to', 6.007)], [('to', 6.010)], [('ok', 2.286)]]),
            (1, [[('ok', 2.558)]]),
            (2, [[('to', 2.740), ('skip',)]]),
        ],
    },
}


def _scripted_run28(shape):
    """Build a validate() hook that walks the recorded plan with the production
    shared/outer/per-call budgets. Returns (validate, clock, invocations)."""
    clock = Clock()
    invocations = []

    def validate(_query, concepts, *_args, deadline, **_kwargs):
        index = len(invocations) + 1
        invocations.append(index)
        spec = shape['invocations'][index - 1]
        accepted = rejected = error = attempts = 0
        for batch in spec[1]:
            if clock() >= deadline:
                break
            batch_done = False
            for attempt in list(batch):
                remaining = min(deadline, shape['outer']) - clock()
                if remaining <= 0.1:
                    break
                budget = min(6.0, remaining)
                attempts += 1
                kind = attempt[0]
                if kind == 'ok':
                    if budget + 1e-6 >= attempt[1]:
                        clock.advance(attempt[1])
                        rejected += 1
                        batch_done = True
                    else:
                        # a trusted decision that cannot fit is unknown -> fail
                        clock.advance(budget)
                        error += 1
                    break
                clock.advance(budget)
                error += 1
            if batch_done:
                continue
            if len(batch) < 2 and clock() < deadline:
                # unobserved second attempt: pessimistic full-budget failure
                remaining = min(deadline, shape['outer']) - clock()
                if remaining > 0.1:
                    clock.advance(min(6.0, remaining))
                    attempts += 1
                    error += 1
        # any batch implied by concept_count but absent from the prefix
        implied = math.ceil(spec[0] / 2) if spec[0] else 0
        for _ in range(implied - len(spec[1])):
            for _ in range(2):
                remaining = min(deadline, shape['outer']) - clock()
                if remaining <= 0.1:
                    break
                clock.advance(min(6.0, remaining))
                attempts += 1
                error += 1
        counts = {'accepted': accepted, 'rejected': rejected, 'error': error,
            'attempts': attempts, 'attempt_errors': attempts if error else 0,
            'retries': 0}
        return [], counts

    return validate, clock, invocations


def _replay_run28(shape, outer, request_id):
    validate, clock, invocations = _scripted_run28({**shape, 'outer': outer})
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


@pytest.mark.parametrize('request_id', sorted(_RUN28_SHAPES))
def test_run28_shape_all_terminate_under_45s(request_id):
    shape = _RUN28_SHAPES[request_id]
    outcome, elapsed, invocations = _replay_run28(shape, 45.0, request_id)
    assert outcome == 'semantic_validator_unavailable'
    assert elapsed == 45.0
    assert invocations


def test_run28_worst_shape_still_terminates_under_60s():
    """60 s is still 1 s short of the worst recorded pessimistic prefix (~61 s)."""
    shape = _RUN28_SHAPES['5cc09a5deeb3']
    outcome, elapsed, _ = _replay_run28(shape, 60.0, '5cc09a5deeb3')
    assert outcome == 'semantic_validator_unavailable'
    assert elapsed == 60.0


@pytest.mark.parametrize('request_id', sorted(_RUN28_SHAPES))
def test_run28_shape_completes_under_75s(request_id):
    shape = _RUN28_SHAPES[request_id]
    outcome, elapsed, invocations = _replay_run28(shape, 75.0, request_id)
    assert outcome == 'completed'
    assert elapsed < 75.0
    assert invocations


def test_observed_six_second_timeout_stays_a_timeout(monkeypatch, tmp_path):
    enable_event_semantics(monkeypatch, tmp_path)
    clock = Clock()
    attempts = []

    def fixed_six_second_timeout(_client, *, timeout, model, **_request):
        attempts.append(timeout)
        clock.advance(timeout)
        raise TimeoutError('synthetic transport delay')

    monkeypatch.setattr('matchmaker_agent.related_interest_validator._completion',
        fixed_six_second_timeout)
    service = Adapter(Graph({'requester': [preference('打籃球')]},
        [event(('三對三籃球',))]), object(), MODEL, deadline=75.0, clock=clock,
        now=lambda: NOW, eligible=lambda _owner: True, allowed=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {s.source_hash: VECTOR[:] for s in signals},
        fallback_model='glm-5.3-flash:cloud')
    with pytest.raises(EventUnavailable, match='semantic_validator_unavailable'):
        service.relevance('requester')
    assert attempts and all(timeout == 6.0 for timeout in attempts)
    assert clock() > 0.0


def test_validator_shared_budget_remains_18s_under_75s_outer(monkeypatch, tmp_path):
    enable_event_semantics(monkeypatch, tmp_path)
    seen = []

    def validate(_query, concepts, *_args, **kwargs):
        seen.append(kwargs)
        return [], complete_decision_counts('unrelated', len(concepts), accepted=0,
            rejected=len(concepts))

    service = Adapter(Graph({'requester': [preference('打籃球')]}, [event(('籃球',))]),
        None, MODEL, deadline=75.0, clock=lambda: 0.0, now=lambda: NOW,
        eligible=lambda _owner: True, allowed=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {s.source_hash: VECTOR[:] for s in signals},
        validate=validate)
    service.relevance('requester')
    assert seen and seen[0]['deadline'] == 18.0


def test_genuine_full_provider_failure_still_fails_closed(monkeypatch, tmp_path):
    enable_event_semantics(monkeypatch, tmp_path)
    attempts = []

    def dead_provider(_client, *, timeout, model, **_request):
        attempts.append((timeout, model))
        raise ConnectionError('synthetic outage')

    monkeypatch.setattr('matchmaker_agent.related_interest_validator._completion', dead_provider)
    service = Adapter(Graph({'requester': [preference('打籃球')]}, [event(('籃球',))]),
        object(), MODEL, deadline=75.0, clock=Clock(), now=lambda: NOW,
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

    enable_event_semantics(monkeypatch, tmp_path)

    def unavailable_503(_client, *, timeout, model, **_request):
        raise APIStatusError('synthetic', response=httpx.Response(503,
            request=httpx.Request('POST', 'http://provider.invalid/v1')), body=None)

    monkeypatch.setattr('matchmaker_agent.related_interest_validator._completion', unavailable_503)
    service = Adapter(Graph({'requester': [preference('打籃球')]}, [event(('籃球',))]),
        object(), MODEL, deadline=75.0, clock=Clock(), now=lambda: NOW,
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
        None, MODEL, deadline=75.0, clock=Clock(), now=lambda: NOW,
        eligible=lambda _owner: True, allowed=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {s.source_hash: VECTOR[:] for s in signals},
        validate=mixed)
    assert service.select('requester', []) == []
    assert len(calls) == 2
