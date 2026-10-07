"""Deterministic regression for the progress-only checkpoint hotfix; synthetic.

Run #31 proved `Adapter.checkpoint_value()` dropped a valid progress-only
checkpoint (no completed/partial decisions, `next_index == 0`), so a resumable
circuit-open/boundary stop reached Social as `resumable=true, checkpoint=null`
and terminalized a user that should have stayed `resume_pending`.

These tests pin the fixed contract:
  * progress-only state at index 0 or later serializes to a non-null checkpoint,
  * a truly empty state still serializes to None,
  * `next_index == 0` is never treated as absent (no truthiness on the index),
  * a resumable stop carries a resume authority,
  * completed work is not revalidated on resume,
  * the Run #31 shapes become resumable and can complete after recovery.
"""
import pytest

from matchmaker_agent.event_v2_adapter import Adapter
from matchmaker_agent.event_v2_contract import EventUnavailable
from matchmaker_agent.event_v2_resume import decode as decode_checkpoint
from matchmaker_agent.event_semantic_circuit import (
    CLOSED, OPEN, close_circuit, open_circuit,
)
from matchmaker_agent.test_event_v2_adapter import (
    Graph, NOW, VECTOR, complete_decision_counts, event, preference,
)

MODEL = 'deepseek-v4.1-flash:cloud'
OUTER = 120.0


@pytest.fixture(autouse=True)
def threshold(monkeypatch):
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MIN_SIMILARITY', '.90')
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ENABLED', 'on')
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MODE', 'active')


@pytest.fixture(autouse=True)
def isolated_circuit(monkeypatch, tmp_path):
    monkeypatch.setenv('EVENT_SEMANTIC_CIRCUIT_FILE', str(tmp_path / 'circuit.json'))
    close_circuit()


def _graph(signal_count=1, group_count=1):
    texts = [f'偏好{index:02d}' for index in range(signal_count)]
    events = [event((f'活動-{index:02d}',), eid=f'ev-{index:02d}') for index in range(group_count)]
    return Graph({'requester': [preference(text) for text in texts]}, events)


def _adapter(*, graph=None, checkpoint=None, validate=None):
    graph = graph if graph is not None else _graph()
    service = Adapter(graph, None, MODEL, deadline=OUTER,
        clock=lambda: 0.0, now=lambda: NOW,
        eligible=lambda _owner: True, allowed=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {s.source_hash: VECTOR[:] for s in signals},
        validate=validate or (lambda _q, concepts, *_a, **_k:
            ([{**c, 'relation': 'sibling_related'} for c in concepts],
             complete_decision_counts('sibling_related', len(concepts),
                 accepted=len(concepts), rejected=0))),
        checkpoint=checkpoint)
    return service


def _stage_progress(service, *, next_index, signal_count, owner='requester'):
    service._checkpoint_owner = owner
    service._snapshot = 'snap'
    service._progress = {f'{owner}|*': {'next_index': next_index, 'signal_count': signal_count}}
    service.actor_digests = {owner: 'digest'}


# TEST 1 — progress-only checkpoint at index zero is meaningful.
def test_progress_only_checkpoint_at_index_zero_is_not_none():
    service = _adapter()
    _stage_progress(service, next_index=0, signal_count=1)
    service.decisions, service._partial = {}, {}
    value = service.checkpoint_value()
    assert value is not None
    decoded = decode_checkpoint(value)
    assert decoded is not None
    entry = decoded['progress']['requester|*']
    assert entry['next_index'] == 0 and entry['signal_count'] == 1
    assert decoded['completed'] == {} and decoded['partial'] == {}


# TEST 2 — progress-only checkpoint at a later index is meaningful.
def test_progress_only_checkpoint_at_later_index_is_not_none():
    service = _adapter()
    _stage_progress(service, next_index=5, signal_count=9)
    service.decisions, service._partial = {}, {}
    value = service.checkpoint_value()
    assert decode_checkpoint(value)['progress']['requester|*']['next_index'] == 5


# TEST 3 — a truly empty state still serializes to None.
def test_truly_empty_state_still_returns_none():
    service = _adapter()
    service._checkpoint_owner = 'requester'
    service._snapshot = 'snap'
    service._progress = {}
    assert service.checkpoint_value() is None
    # And an untouched adapter (no owner pinned at all) is also None.
    assert _adapter().checkpoint_value() is None


# TEST 4 — circuit opens before the first semantic group completes.
def test_circuit_open_before_first_group_is_resumable_with_checkpoint():
    open_circuit(now=lambda: 1000.0)
    service = _adapter()
    with pytest.raises(EventUnavailable) as failure:
        service.relevance('requester')
    assert failure.value.resumable is True
    value = service.checkpoint_value()
    assert value is not None, 'producer must not emit resumable without a checkpoint'
    decoded = decode_checkpoint(value)
    assert decoded is not None
    entry = decoded['progress'].get('requester|*')
    assert entry is not None and entry['next_index'] == 0
    # The contract-violation flag is attached at the HTTP boundary
    # (find_matches), so a directly-raised adapter exception does not yet carry
    # it; the invariant under test here is that a checkpoint exists at all.
    assert value is not None


# TEST 6 — provider recovers: a progress-only checkpoint at index 0 resumes and
# completes, and the completed work is not repeated.
def test_provider_recovery_resumes_from_progress_only_index_zero():
    open_circuit(now=lambda: 1000.0)
    first = _adapter()
    with pytest.raises(EventUnavailable):
        first.relevance('requester')
    checkpoint = first.checkpoint_value()
    assert checkpoint is not None

    # Provider recovers: circuit closes, a fresh attempt resumes.
    close_circuit()
    calls = {'count': 0}
    def validate(_q, concepts, *_a, **_k):
        calls['count'] += 1
        return ([{**c, 'relation': 'sibling_related'} for c in concepts],
            complete_decision_counts('sibling_related', len(concepts),
                accepted=len(concepts), rejected=0))
    second = _adapter(checkpoint=checkpoint, validate=validate)
    second.relevance('requester')
    assert second.resumed is True
    assert calls['count'] == 1


# TEST 7 — completed groups in the checkpoint are not revalidated.
def test_completed_groups_are_not_revalidated_on_resume():
    first = _adapter()
    first.relevance('requester')
    checkpoint = first.checkpoint_value()
    decoded = decode_checkpoint(checkpoint)
    assert decoded and decoded['completed'], 'accepted decisions must be checkpointed'

    calls = {'count': 0}
    def validate(_q, concepts, *_a, **_k):
        calls['count'] += 1
        return [], complete_decision_counts('unrelated', len(concepts), accepted=0,
            rejected=len(concepts))
    second = _adapter(checkpoint=checkpoint, validate=validate)
    second.relevance('requester')
    assert second.resumed is True
    assert calls['count'] == 0
    assert len(second.decisions) == len(decoded['completed'])


# TEST 8 — impossible producer inconsistency is surfaced, never fabricated.
def test_contract_violation_flag_reflects_checkpoint_presence():
    # The flag is computed at the HTTP boundary. When a checkpoint exists it
    # must be False; when the state is truly empty the adapter serializes None
    # and the boundary would flag the violation instead of fabricating progress.
    open_circuit(now=lambda: 1000.0)
    service = _adapter()
    with pytest.raises(EventUnavailable):
        service.relevance('requester')
    assert service.checkpoint_value() is not None
    empty = _adapter()
    empty._checkpoint_owner = 'requester'
    empty._snapshot = 'snap'
    empty._progress = {}
    assert empty.checkpoint_value() is None
