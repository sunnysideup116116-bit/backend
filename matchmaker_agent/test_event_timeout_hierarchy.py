"""Deterministic Event timeout-hierarchy regression; synthetic, no providers.

Pins the paired timeout hierarchy to the Run #29 evidence:
  * Event outer deadline authority = 120 s,
  * caller HTTP timeout = 150 s (30 s margin above the outer deadline),
and replays the ten recorded Run #29 outer-deadline termination shapes:
  * 75 s terminates 8/10 under strict pessimistic replay (10/10 production),
  * 90 s still terminates 3/10 (insufficient headroom),
  * 105 s clears all ten with ~6 s margin,
  * 120 s clears all ten with ~21 s margin.

The replay is deliberately pessimistic: observed 6 s timeouts stay timeouts,
observed secondary timeouts stay timeouts, and attempts the old budget skipped
are re-run at full budget and fail. No historical timeout becomes a synthetic
success. Also pins the unchanged 18 s / 6 s / 2-attempt validator contract,
fail-closed behavior, breaker accountability and kill authority.
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
CALLER_SOURCE = (Path(event_v2_api.__file__).resolve().parents[1]
    / 'social' / 'services' / 'event_opportunity_service.py')


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


def test_outer_deadline_authority_is_120_seconds():
    assert EVENT_OUTER_DEADLINE_SECONDS == 120.0
    source = EVENT_API_SOURCE.read_text(encoding='utf-8')
    assert 'deadline=time.monotonic()+EVENT_OUTER_DEADLINE_SECONDS' in source
    assert 'time.monotonic()+75' not in source
    assert 'time.monotonic()+45' not in source
    assert 'time.monotonic()+38' not in source


def test_caller_timeout_stays_above_the_outer_deadline_with_margin():
    source = CALLER_SOURCE.read_text(encoding='utf-8')
    assert 'EVENT_OPPORTUNITY_TIMEOUT_SECONDS = 150' in source
    assert 'timeout=(3, EVENT_OPPORTUNITY_TIMEOUT_SECONDS)' in source
    assert 'timeout=(3, 90)' not in source
    caller = 150
    assert caller > EVENT_OUTER_DEADLINE_SECONDS
    assert caller - EVENT_OUTER_DEADLINE_SECONDS >= 20  # documented preference


def test_outer_deadline_is_forwarded_to_the_adapter(monkeypatch):
    seen = {}

    @contextmanager
    def fake_session_for(_agent):
        yield object()

    class Inspect:
        def __init__(self, *_args, **kwargs):
            seen['deadline'] = kwargs['deadline']
            seen['checkpoint'] = kwargs.get('checkpoint')
            self.resumed = False

        def select(self, *_args, **_kwargs):
            return []

        def telemetry(self):
            return {}

        def checkpoint_value(self):
            return None

    monkeypatch.setattr(event_v2_api, 'session_for', fake_session_for)
    monkeypatch.setattr(event_v2_api, 'Adapter', Inspect)
    monkeypatch.setattr(event_v2_api.time, 'monotonic', lambda: 1000.0)
    find_matches(SimpleNamespace(client=None, model=MODEL), 'owner', [], checkpoint={'v': 1})
    assert seen['deadline'] == 1000.0 + 120.0
    assert seen['checkpoint'] == {'v': 1}




# ---------------------------------------------------------------------------
# Recorded Run #29 termination shapes (source: run #29 telemetry, 2026-10-06).
# Per invocation: (concept_count, batches); per attempt:
#   ('ok', seconds)  trusted decision at observed latency
#   ('to', seconds)  observed provider timeout (secondary or truncated)
#   ('skip',)        attempt the old shared/outer budget never started
# Every non-'ok' attempt replays pessimistically at full budget and fails.
# ---------------------------------------------------------------------------
_RUN29_SHAPES = {
    '1c439f32fb85': {
        'start': 5.564,
        'gaps': [0.0, 0.0, 0.001, 0.411, 0.0, 0.0, 0.0],
        'invocations': [
            (2, [[('to', 6.007), ('to', 6.004)]]),
            (1, [[('to', 6.008), ('to', 6.007)]]),
            (3, [[('to', 6.008), ('to', 6.008)], [('to', 5.991), ('skip',)]]),
            (1, [[('to', 6.008), ('to', 6.007)]]),
            (1, [[('to', 6.008), ('to', 6.01)]]),
            (1, [[('ok', 2.948)]]),
            (1, [[('skip',)]]),
            (1, [[('skip',)]]),
        ],
    },
    '6fc5bd6132be': {
        'start': 5.967,
        'gaps': [0.841, 1.488, 0.838, 0.523, 0.502, 1.232],
        'invocations': [
            (3, [[('ok', 1.943)], [('ok', 2.035)]]),
            (2, [[('to', 6.007), ('to', 6.007)]]),
            (2, [[('ok', 1.373)]]),
            (4, [[('ok', 2.365)], [('ok', 4.934)]]),
            (1, [[('to', 6.007), ('ok', 4.573)]]),
            (5, [[('to', 6.007), ('ok', 4.253)], [('ok', 3.833)], [('ok', 1.327)]]),
            (3, [[('to', 6.009), ('to', 6.008)], [('ok', 0.841)]]),
        ],
    },
    '4764a6c161b4': {
        'start': 5.267,
        'gaps': [0.0, 0.0, 0.0, 0.536, 0.33, 0.0, 0.247, 0.0, 0.0],
        'invocations': [
            (2, [[('to', 6.009), ('to', 6.009)]]),
            (1, [[('ok', 1.424)]]),
            (3, [[('ok', 3.697)], [('ok', 2.397)]]),
            (1, [[('ok', 1.542)]]),
            (2, [[('to', 6.008), ('to', 6.007)]]),
            (2, [[('ok', 5.688)]]),
            (1, [[('ok', 1.177)]]),
            (2, [[('ok', 5.25)]]),
            (2, [[('to', 6.007), ('to', 6.008)]]),
            (1, [[('to', 6.007), ('to', 5.394)]]),
        ],
    },
    '2167eed9f836': {
        'start': 5.867,
        'gaps': [0.0, 0.507, 0.001, 0.0, 0.0, 1.388, 0.311, 0.0, 0.307],
        'invocations': [
            (2, [[('ok', 2.055)]]),
            (2, [[('ok', 2.539)]]),
            (2, [[('to', 6.007), ('to', 6.008)]]),
            (1, [[('ok', 3.475)]]),
            (3, [[('to', 6.008), ('to', 6.009)], [('ok', 2.299)]]),
            (1, [[('ok', 1.222)]]),
            (2, [[('to', 6.007), ('to', 6.009)]]),
            (2, [[('to', 6.008), ('to', 6.004)]]),
            (1, [[('to', 6.007), ('skip',)]]),
            (2, [[('to', 0.962), ('skip',)]]),
        ],
    },
    'fd599d238285': {
        'start': 5.545,
        'gaps': [0.452, 0.658, 0.52, 0.177, 0.189, 0.172],
        'invocations': [
            (2, [[('ok', 1.139)]]),
            (3, [[('ok', 4.468)], [('ok', 1.589)]]),
            (1, [[('ok', 2.017)]]),
            (5, [[('to', 6.008), ('to', 6.007)], [('ok', 4.417)], [('ok', 1.445)]]),
            (3, [[('to', 6.008), ('to', 6.006)], [('to', 5.994), ('skip',)]]),
            (2, [[('to', 6.008), ('to', 6.007)]]),
            (2, [[('to', 6.008), ('to', 4.167)]]),
        ],
    },
    '894a36f80411': {
        'start': 5.857,
        'gaps': [0.775, 2.07, 0.672, 0.846, 0.704, 1.563, 0.171, 0.11, 0.218, 0.17, 0.391],
        'invocations': [
            (1, [[('ok', 1.458)]]),
            (2, [[('to', 6.008), ('to', 6.008)]]),
            (1, [[('ok', 2.093)]]),
            (1, [[('ok', 1.612)]]),
            (1, [[('ok', 0.983)]]),
            (1, [[('ok', 1.799)]]),
            (2, [[('to', 6.008), ('ok', 5.384)]]),
            (1, [[('ok', 1.842)]]),
            (1, [[('ok', 0.979)]]),
            (3, [[('to', 6.008), ('to', 6.008)], [('ok', 1.68)]]),
            (2, [[('to', 6.007), ('to', 6.008)]]),
            (1, [[('to', 1.57), ('skip',)]]),
        ],
    },
    'b4ac99a48f7b': {
        'start': 4.444,
        'gaps': [0.0, 0.0, 0.0, 0.75, 0.0, 0.0, 0.0],
        'invocations': [
            (2, [[('to', 6.007), ('to', 6.009)]]),
            (2, [[('to', 6.008), ('to', 6.002)]]),
            (1, [[('ok', 1.209)]]),
            (1, [[('to', 6.009), ('to', 6.007)]]),
            (1, [[('ok', 3.877)]]),
            (3, [[('to', 6.007), ('to', 6.007)], [('ok', 3.826)]]),
            (1, [[('to', 6.007), ('ok', 4.955)]]),
            (1, [[('to', 1.877), ('skip',)]]),
        ],
    },
    '150712dd962a': {
        'start': 5.56,
        'gaps': [0.5, 1.66, 0.831, 0.482, 0.429, 0.188, 0.122, 0.123, 0.123],
        'invocations': [
            (3, [[('ok', 1.396)], [('ok', 1.097)]]),
            (2, [[('to', 6.008), ('to', 6.008)]]),
            (2, [[('ok', 1.745)]]),
            (4, [[('ok', 4.156)], [('ok', 1.859)]]),
            (1, [[('ok', 4.468)]]),
            (5, [[('ok', 4.821)], [('to', 6.007), ('to', 6.007)], [('to', 1.168), ('skip',)]]),
            (1, [[('ok', 1.819)]]),
            (1, [[('ok', 0.824)]]),
            (2, [[('ok', 4.599)]]),
            (5, [[('to', 6.009), ('ok', 4.731)], [('to', 2.26), ('skip',)], [('skip',)]]),
        ],
    },
    '08bb92362a85': {
        'start': 4.421,
        'gaps': [0.749, 1.835, 0.178, 0.181, 0.0, 0.296, 0.178, 0.122],
        'invocations': [
            (2, [[('ok', 5.962)]]),
            (1, [[('ok', 3.201)]]),
            (2, [[('to', 6.007), ('ok', 3.372)]]),
            (3, [[('to', 6.008), ('to', 6.008)], [('ok', 2.068)]]),
            (2, [[('to', 6.008), ('to', 6.007)]]),
            (2, [[('ok', 3.237)]]),
            (2, [[('to', 6.009), ('to', 6.007)]]),
            (1, [[('ok', 3.215)]]),
            (4, [[('ok', 2.424)], [('to', 1.506), ('skip',)]]),
        ],
    },
    '20aaa67697aa': {
        'start': 5.044,
        'gaps': [1.111, 0.412, 0.226, 0.225, 0.113, 0.0, 0.183, 0.0, 0.116, 0.0, 0.182, 0.0, 0.0],
        'invocations': [
            (1, [[('ok', 2.308)]]),
            (3, [[('to', 6.007), ('ok', 5.188)], [('ok', 0.967)]]),
            (1, [[('ok', 2.276)]]),
            (2, [[('ok', 1.525)]]),
            (2, [[('ok', 1.6)]]),
            (2, [[('to', 6.008), ('to', 6.008)]]),
            (1, [[('ok', 3.63)]]),
            (3, [[('ok', 2.097)], [('ok', 1.932)]]),
            (3, [[('ok', 1.668)], [('ok', 3.133)]]),
            (1, [[('ok', 1.761)]]),
            (2, [[('ok', 1.831)]]),
            (2, [[('to', 6.008), ('to', 6.009)]]),
            (1, [[('to', 6.007), ('skip',)]]),
            (2, [[('to', 1.422), ('skip',)]]),
        ],
    },
}

def _replay_run29(shape, outer):
    """Walk one recorded shape with the production budgets (6 s per call,
    min(outer, start+18 s) shared per invocation). Returns (terminated, clock)."""
    clock = shape['start']
    terminated = None
    for index, (concepts, batches) in enumerate(shape['invocations']):
        if index:
            clock += shape['gaps'][index - 1]
        if clock >= outer:
            terminated = index + 1
            break
        shared = min(outer, clock + 18.0)
        plan = [list(batch) for batch in batches]
        implied = math.ceil(concepts / 2) if concepts else 0
        while len(plan) < implied:
            plan.append([])
        for batch in plan:
            done = False
            for attempt in batch:
                remaining = min(shared, outer) - clock
                if remaining <= 0.1:
                    break
                budget = min(6.0, remaining)
                kind = attempt[0]
                if kind == 'skip':
                    clock += budget
                    continue
                if kind == 'ok':
                    if budget + 1e-6 >= attempt[1]:
                        clock += attempt[1]
                        done = True
                    else:
                        clock += budget
                    break
                clock += budget
            if done:
                continue
            if len(batch) == 0:
                for _ in range(2):
                    remaining = min(shared, outer) - clock
                    if remaining <= 0.1:
                        break
                    clock += min(6.0, remaining)
            elif len(batch) == 1:
                remaining = min(shared, outer) - clock
                if remaining > 0.1:
                    clock += min(6.0, remaining)
        if clock >= outer:
            terminated = index + 1
            break
    return terminated, clock


def test_run29_shapes_terminate_under_75s():
    terminated = 0
    for shape in _RUN29_SHAPES.values():
        hit, _clock = _replay_run29(shape, 75.0)
        terminated += int(hit is not None)
    assert terminated >= 8  # strict pessimistic replay; production was 10/10


def test_run29_shapes_still_terminate_under_90s():
    terminated = 0
    for shape in _RUN29_SHAPES.values():
        hit, _clock = _replay_run29(shape, 90.0)
        terminated += int(hit is not None)
    assert terminated >= 1  # 90 s lacks headroom for the heavier shapes


@pytest.mark.parametrize('outer', [105.0, 120.0])
def test_run29_shapes_complete_under_selected_deadlines(outer):
    for shape in _RUN29_SHAPES.values():
        hit, clock = _replay_run29(shape, outer)
        assert hit is None, f'shape terminated at invocation {hit}'
        assert clock < outer


def test_run29_shapes_need_more_than_ninety_seconds():
    """The longest deterministic shape must end above 90 s: this is why 90 s is
    not the selected outer deadline."""
    ends = []
    for shape in _RUN29_SHAPES.values():
        hit, clock = _replay_run29(shape, 120.0)
        assert hit is None
        ends.append(clock)
    assert max(ends) > 90.0
    assert max(ends) < 120.0


def test_fourteen_invocation_source_shaped_request_completes_under_120s():
    shape = _RUN29_SHAPES['20aaa67697aa']  # the 14-invocation Run #29 shape
    assert len(shape['invocations']) == 14
    hit, clock = _replay_run29(shape, 120.0)
    assert hit is None
    assert clock < 120.0


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
        [event(('三對三籃球',))]), object(), MODEL, deadline=120.0, clock=clock,
        now=lambda: NOW, eligible=lambda _owner: True, allowed=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {s.source_hash: VECTOR[:] for s in signals},
        fallback_model='glm-5.3-flash:cloud')
    with pytest.raises(EventUnavailable, match='semantic_validator_unavailable'):
        service.relevance('requester')
    assert attempts and all(timeout == 6.0 for timeout in attempts)
    assert clock() > 0.0


def test_validator_shared_budget_remains_18s_under_120s_outer(monkeypatch, tmp_path):
    enable_event_semantics(monkeypatch, tmp_path)
    seen = []

    def validate(_query, concepts, *_args, **kwargs):
        seen.append(kwargs)
        return [], complete_decision_counts('unrelated', len(concepts), accepted=0,
            rejected=len(concepts))

    service = Adapter(Graph({'requester': [preference('打籃球')]}, [event(('籃球',))]),
        None, MODEL, deadline=120.0, clock=lambda: 0.0, now=lambda: NOW,
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
        object(), MODEL, deadline=120.0, clock=Clock(), now=lambda: NOW,
        eligible=lambda _owner: True, allowed=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {s.source_hash: VECTOR[:] for s in signals},
        fallback_model='glm-5.3-flash:cloud')
    with pytest.raises(EventUnavailable, match='semantic_validator_unavailable'):
        service.relevance('requester')
    assert 1 <= len(attempts) <= 2
    assert all(timeout <= 6.0 for timeout, _ in attempts)
    assert len({model for _, model in attempts}) <= 2


def test_systemic_job_unavailable_stays_breaker_accountable(monkeypatch, tmp_path):
    import httpx
    from openai import APIStatusError

    enable_event_semantics(monkeypatch, tmp_path)

    def unavailable_503(_client, *, timeout, model, **_request):
        raise APIStatusError('synthetic', response=httpx.Response(503,
            request=httpx.Request('POST', 'http://provider.invalid/v1')), body=None)

    monkeypatch.setattr('matchmaker_agent.related_interest_validator._completion', unavailable_503)
    service = Adapter(Graph({'requester': [preference('打籃球')]}, [event(('籃球',))]),
        object(), MODEL, deadline=120.0, clock=Clock(), now=lambda: NOW,
        eligible=lambda _owner: True, allowed=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {s.source_hash: VECTOR[:] for s in signals},
        fallback_model='glm-5.3-flash:cloud')
    with pytest.raises(EventUnavailable, match='semantic_validator_unavailable'):
        service.relevance('requester')


def test_partial_error_with_trusted_decision_does_not_fake_terminal_unavailable(monkeypatch, tmp_path):
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
        None, MODEL, deadline=120.0, clock=Clock(), now=lambda: NOW,
        eligible=lambda _owner: True, allowed=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {s.source_hash: VECTOR[:] for s in signals},
        validate=mixed)
    assert service.select('requester', []) == []
    assert len(calls) == 2
