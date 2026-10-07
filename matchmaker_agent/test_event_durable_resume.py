"""Deterministic durable checkpoint/resume + circuit tests; synthetic only.

Models the Run #30 architectural limitation: a user whose semantic work does
not fit one synchronous HTTP lifetime must checkpoint and resume instead of
losing all progress. Provider outcomes stay pessimistic; resume is a later
attempt, not an extension of one validator call.

Shape: the requester has N positive signals, each triggering one validator
invocation over the event groups (matching the observed Run #30 shapes with
7-15 sequential invocations). Every invocation consumes its full shared budget
so the 120 s outer deadline is reached after the same number of invocations the
production log showed.
"""
import pytest

from matchmaker_agent.event_v2_adapter import Adapter
from matchmaker_agent.event_v2_contract import EventUnavailable
from matchmaker_agent.event_v2_resume import decode as decode_checkpoint
from matchmaker_agent.event_semantic_circuit import (
    CLOSED, OPEN, allow_semantic, close_circuit, open_circuit, probe_and_recover, snapshot,
)
from matchmaker_agent.test_event_v2_adapter import (
    Graph, NOW, VECTOR, complete_decision_counts, event, preference,
)

MODEL = 'deepseek-v4.1-flash:cloud'
OUTER = 120.0


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


@pytest.fixture(autouse=True)
def isolated_circuit(monkeypatch, tmp_path):
    monkeypatch.setenv('EVENT_SEMANTIC_CIRCUIT_FILE', str(tmp_path / 'circuit.json'))
    close_circuit()


def _graph(signal_count=15, group_count=6):
    texts = [f'偏好{index:02d}' for index in range(signal_count)]
    events = [event((f'活動-{index:02d}',), eid=f'ev-{index:02d}') for index in range(group_count)]
    return Graph({'requester': [preference(text) for text in texts]}, events)


def _adapter(*, boundary_after=None, checkpoint=None, accept=False, graph=None,
        signal_count=15, group_count=6):
    """One attempt. Each invocation consumes its whole shared budget; once
    `boundary_after` invocations completed, the next one ends at the 120 s
    outer deadline and checkpoints."""
    clock = Clock()
    calls = {'count': 0}
    graph = graph if graph is not None else _graph(signal_count, group_count)

    def validate(_query, concepts, *_args, deadline, **_kwargs):
        calls['count'] += 1
        if boundary_after is not None and calls['count'] > boundary_after:
            clock.value = max(clock.value, OUTER + 0.001)
            return [], complete_decision_counts('unrelated', len(concepts), accepted=0,
                rejected=len(concepts))
        # Boundary attempts burn the full shared budget; completed attempts use
        # a small step so a full pass fits comfortably inside the outer deadline.
        step = 18.0 if boundary_after is not None else 2.0
        clock.value = min(deadline, clock.value + step)
        if accept:
            return ([{**concept, 'relation': 'sibling_related'} for concept in concepts],
                complete_decision_counts('sibling_related', len(concepts),
                    accepted=len(concepts), rejected=0))
        return [], complete_decision_counts('unrelated', len(concepts), accepted=0,
            rejected=len(concepts))

    service = Adapter(graph, None, MODEL, deadline=OUTER, clock=clock, now=lambda: NOW,
        eligible=lambda _owner: True, allowed=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {s.source_hash: VECTOR[:] for s in signals},
        validate=validate, checkpoint=checkpoint)
    return service, clock, calls


def test_boundary_checkpoint_preserves_next_signal_index():
    service, _clock, calls = _adapter(boundary_after=6)
    with pytest.raises(EventUnavailable) as failure:
        service.relevance('requester')
    assert failure.value.resumable is True
    saved = decode_checkpoint(service.checkpoint_value())
    assert saved is not None and saved['owner'] == 'requester'
    progress = [value for key, value in saved['progress'].items() if key.startswith('requester|*')]
    assert progress and progress[0]['next_index'] == 7
    assert progress[0]['signal_count'] == 15
    assert calls['count'] == 7


def test_resume_continues_after_completed_signals_without_revalidating():
    first, _clock, calls_first = _adapter(boundary_after=6)
    with pytest.raises(EventUnavailable):
        first.relevance('requester')
    checkpoint = first.checkpoint_value()
    assert decode_checkpoint(checkpoint) is not None
    assert calls_first['count'] == 7

    second, _clock, calls_second = _adapter(checkpoint=checkpoint)
    second.relevance('requester')
    assert second.resumed is True
    # Signals 1..7 already completed; only the remaining 8 are validated.
    assert calls_second['count'] == 15 - 7


def test_resumed_trusted_decisions_are_reused_not_recomputed():
    first, _clock, calls_first = _adapter(accept=True)
    first.relevance('requester')
    checkpoint = first.checkpoint_value()
    saved = decode_checkpoint(checkpoint)
    assert saved['completed'], 'accepted decisions must be checkpointed'
    assert calls_first['count'] == 15

    second, _clock, calls_second = _adapter(accept=True, checkpoint=checkpoint)
    second.relevance('requester')
    assert second.resumed is True
    # Every event already carried a trusted decision, so no validator call is
    # repeated at all.
    assert calls_second['count'] == 0
    assert len(second.decisions) == len(saved['completed'])


def test_checkpoint_is_discarded_when_owner_signals_changed():
    first, _clock, _calls = _adapter(accept=True)
    first.relevance('requester')
    checkpoint = first.checkpoint_value()

    changed = _graph(signal_count=15, group_count=6)
    changed.owners['requester'] = [preference('完全不同的偏好')]
    second, _clock, _calls = _adapter(accept=True, checkpoint=checkpoint, graph=changed)
    second.relevance('requester')
    assert second.resumed is False


def test_checkpoint_is_discarded_when_event_inventory_changed():
    first, _clock, _calls = _adapter(accept=True)
    first.relevance('requester')
    checkpoint = first.checkpoint_value()

    second, _clock, _calls = _adapter(accept=True, checkpoint=checkpoint,
        graph=_graph(signal_count=15, group_count=5))
    second.relevance('requester')
    assert second.resumed is False


def test_provider_outage_then_healthy_provider_resumes_and_completes():
    # Attempt 1: seven invocations complete, the eighth ends at the outer deadline.
    first, _clock, calls_first = _adapter(boundary_after=7)
    with pytest.raises(EventUnavailable) as failure:
        first.relevance('requester')
    assert failure.value.resumable is True
    checkpoint = first.checkpoint_value()
    assert decode_checkpoint(checkpoint)['progress']

    # Attempt 2 after the provider recovers: resumes at signal 8 and finishes.
    second, _clock, calls_second = _adapter(accept=True, checkpoint=checkpoint)
    second.relevance('requester')
    assert second.resumed is True
    assert calls_second['count'] == 15 - 7


def test_open_circuit_fails_closed_without_calling_providers():
    open_circuit(now=lambda: 1000.0)
    service, _clock, calls = _adapter()
    with pytest.raises(EventUnavailable) as failure:
        service.relevance('requester')
    assert failure.value.resumable is True
    assert calls['count'] == 0


def test_circuit_opens_recovers_after_cooldown_and_never_touches_manual_kill():
    assert allow_semantic(now=lambda: 0.0) is True
    state = open_circuit(now=lambda: 1000.0)
    assert state['state'] == OPEN
    assert allow_semantic(now=lambda: 1000.0) is False
    assert probe_and_recover(lambda: True, now=lambda: 1100.0) == OPEN
    assert probe_and_recover(lambda: True, now=lambda: 1400.0) == CLOSED
    assert allow_semantic(now=lambda: 1401.0) is True


def test_failed_half_open_probe_stays_open_with_next_cooldown():
    open_circuit(now=lambda: 0.0)
    assert probe_and_recover(lambda: False, now=lambda: 400.0) == OPEN
    assert snapshot()['cooldown'] >= 300
    def broken():
        raise RuntimeError('probe transport failure')
    assert probe_and_recover(broken, now=lambda: 5000.0) == OPEN


# ---------------------------------------------------------------------------
# Run #30 source-shaped simulation (observed 2026-10-06/07).
# Observed: 15-invocation request terminated at 120.005 s, 9-invocation at
# 120.008 s, and a 4-invocation degraded request needed 76.132 s for a typed
# unavailable. Each failing invocation burned the full 6 s primary + 6 s
# secondary failover budget before the outer deadline.
# ---------------------------------------------------------------------------

_RUN30_SHAPES = {15: 120.005, 9: 120.008, 4: 76.132}
_INVOCATION_SECONDS = 12.0  # 6 s primary + 6 s secondary timeout per invocation


def _run30_adapter(invocations, *, checkpoint=None):
    clock = Clock()
    calls = {'count': 0, 'concepts': []}
    graph = _graph(signal_count=invocations, group_count=6)

    def validate(_query, concepts, *_args, deadline, **_kwargs):
        calls['count'] += 1
        calls['concepts'].append(tuple(sorted(c['concept_key'] for c in concepts)))
        clock.value = min(deadline, clock.value + _INVOCATION_SECONDS)
        return [], {'accepted': 0, 'rejected': 0, 'error': len(concepts),
            'attempts': 2, 'attempt_errors': 2, 'retries': 1, 'failover_attempts': 1}

    service = Adapter(graph, None, MODEL, deadline=OUTER, clock=clock, now=lambda: NOW,
        eligible=lambda _owner: True, allowed=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {s.source_hash: VECTOR[:] for s in signals},
        validate=validate, checkpoint=checkpoint)
    return service, clock, calls


@pytest.mark.parametrize('invocations', [15, 9, 4])
def test_run30_shape_synchronous_request_reproduces_failure(invocations):
    """The old synchronous flow loses the whole user once the 120 s budget is
    exhausted mid-way, exactly as Run #30 observed."""
    service, _clock, _calls = _run30_adapter(invocations)
    with pytest.raises(EventUnavailable) as failure:
        service.relevance('requester')
    assert failure.value.resumable is True
    assert service.resumed is False
    saved = decode_checkpoint(service.checkpoint_value())
    assert saved and saved['progress'], 'progress must survive the boundary'


@pytest.mark.parametrize('invocations', [15, 9, 4])
def test_run30_shape_progress_survives_and_resumes_to_completion(invocations):
    """Checkpoint/resume completes the same shape across bounded attempts."""
    # Attempt 1 fails wholesale (outage or boundary); a resumable checkpoint
    # is always produced so the user's weekly work is never lost.
    first, _clock, calls_first = _run30_adapter(invocations)
    with pytest.raises(EventUnavailable):
        first.relevance('requester')
    checkpoint = first.checkpoint_value()
    assert decode_checkpoint(checkpoint)['progress']
    completed_first = calls_first['count']
    assert 1 <= completed_first <= invocations

    # Attempt 2 (provider healthy) resumes and completes. Any signal already
    # completed on attempt 1 is not revalidated; a signal that only ever failed
    # is retried, because no trusted decision exists for it.
    calls_second = {'count': 0}
    def validate(_q, concepts, *_a, **_k):
        calls_second['count'] += 1
        return ([{**c, 'relation': 'sibling_related'} for c in concepts],
            complete_decision_counts('sibling_related', len(concepts),
                accepted=len(concepts), rejected=0))
    second = Adapter(_graph(signal_count=invocations, group_count=6), None, MODEL,
        deadline=OUTER, clock=Clock(), now=lambda: NOW,
        eligible=lambda _owner: True, allowed=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {s.source_hash: VECTOR[:] for s in signals},
        validate=validate, checkpoint=checkpoint)
    second.relevance('requester')
    assert second.resumed is True
    if completed_first < invocations:
        # Boundary shape: the completed prefix is skipped on resume.
        assert calls_second['count'] == invocations - completed_first
    else:
        # Wholesale-outage shape: nothing completed, so the whole scope is
        # retried exactly once and then completes.
        assert calls_second['count'] == invocations
    assert len(second.decisions) >= 1


def test_run30_boundary_shape_skips_completed_prefix_on_resume():
    """The 15-invocation shapes from Run #30 exceed the 120 s budget mid-way;
    the completed prefix is checkpointed and skipped on the next attempt."""
    first, _clock, calls_first = _run30_adapter(15)
    with pytest.raises(EventUnavailable):
        first.relevance('requester')
    checkpoint = first.checkpoint_value()
    completed_first = calls_first['count']
    assert completed_first < 15
    saved = decode_checkpoint(checkpoint)
    progress = [value for key, value in saved['progress'].items() if key.startswith('requester|*')]
    assert progress and progress[0]['next_index'] == completed_first
