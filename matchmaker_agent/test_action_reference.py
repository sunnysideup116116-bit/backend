"""Real handler/real MAC + transactional synthetic Graph; no external I/O.

Ports the original 5-pass/3-fail reproducer to server-issued references, then
adds disabled/revision/tamper/replay/ABA/concurrency/rollback contract cases.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
from unittest.mock import MagicMock

import pytest
import agent_api
from concept_identity import canonicalize_concept
from matchmaker_agent import preference_action_reference as refs
from matchmaker_agent import preference_bootstrap_contract as contract


class Result(list):
    def single(self, **_kwargs):
        assert len(self) <= 1
        return self[0] if self else None

    def consume(self):
        pass


class Graph:
    def __init__(self):
        self.rows = {'owner': [], 'other': []}
        self.revisions = {'owner': 0, 'other': 0}
        self.pending = {'owner': [], 'other': []}
        self.lock = threading.RLock()
        self.fail_write = False
        self.serial = 0

    def add(self, owner='owner', text='Coffee', polarity='PREFERS'):
        self.serial += 1
        row = {'id': f'synthetic-edge-{self.serial}', 'relation': polarity,
               'properties': {}, 'concept': canonicalize_concept(text).as_dict()}
        self.rows[owner].append(row)
        return row

    def ref(self, row, owner='owner'):
        return refs.reference(owner, self.revisions[owner], row)

    def state(self):
        return deepcopy((self.rows, self.revisions, self.pending, self.serial))

    def execute_write(self, callback):
        with self.lock:
            before = self.state()
            try:
                return callback(self)
            except Exception:
                self.rows, self.revisions, self.pending, self.serial = before
                raise

    def run(self, query, **p):
        owner = p.get('owner', p.get('user_id'))
        if 'SET u.preference_revision=coalesce(u.preference_revision,0)+1' in query:
            self.revisions[owner] += 1
            return Result()
        if 'AS revision' in query:
            return Result([{'revision': self.revisions[owner], 'epoch': 0, 'epoch_at': 0, 'pending': None}])
        if 'ORDER BY id LIMIT' in query:
            return Result(deepcopy(self.rows[owner]))
        if 'AS keys' in query:
            return Result([{'keys': list(self.pending[owner])}])
        if 'SET u.preference_action_projection_keys' in query:
            if p['key'] not in self.pending[owner]:
                self.pending[owner].append(p['key'])
            return Result()
        if 'RETURN type(target)' in query:
            return Result()
        matches = [r for r in self.rows[owner] if r['concept']['key'] == p.get('key')
                   and r['id'] == p.get('association_id') and r['relation'] == p.get('polarity')
                   and r['relation'] in {'PREFERS', 'AVOIDS'}]
        if 'RETURN old.key AS key' in query:
            return Result([dict(r['concept']) for r in matches])
        if 'DELETE existing' in query or 'DELETE active' in query:
            if not matches:
                return Result()
            assert len(matches) == 1
            row = matches[0]
            relation = row['relation']
            self.rows[owner].remove(row)
            if 'DELETE active' in query:
                self.rows[owner].append({**row, 'relation': 'MEMORY_DISABLED',
                    'properties': {'original_relation': relation}})
            else:
                self.add(owner, p['label'], relation)
            if self.fail_write:
                raise RuntimeError('synthetic transaction failure after simulated write')
            return Result([{'relation': relation, 'original_relation': relation}])
        raise AssertionError('Unexpected query in transaction adapter: ' + query[:100])


@pytest.fixture
def graph(monkeypatch):
    g = Graph()
    driver = MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = g
    monkeypatch.setattr(agent_api.GraphDatabase, 'driver', lambda *_a, **_k: driver)
    monkeypatch.setattr(contract, 'quota_signing_key', lambda: b'synthetic-reference-tests')
    monkeypatch.setattr(agent_api, 'assert_existing_preference_identities', lambda *_a: None)
    monkeypatch.setattr(agent_api, 'enqueue_keys', lambda *_a: None)
    return g


def action(token, kind='correct', owner='owner', value='Tea'):
    return asyncio.run(agent_api.memory_action(agent_api.MemoryActionRequest(
        user_id=owner, key=token, action=kind, value=value)))


def rejected_without_write(graph, token, kind='correct', owner='owner'):
    before = graph.state()
    result = action(token, kind, owner)
    assert result['error_code'] == 'stale_source', result
    assert graph.state() == before


@pytest.mark.parametrize('polarity', ['PREFERS', 'AVOIDS'])
@pytest.mark.parametrize('kind', ['correct', 'disable'])
def test_current_reference_success_and_replay_rejected(graph, polarity, kind):
    row = graph.add(polarity=polarity)
    token = graph.ref(row)
    assert action(token, kind)['status'] == 'success'
    assert graph.revisions['owner'] == 1
    assert graph.pending['owner'] == [row['concept']['key']]
    rejected_without_write(graph, token, kind)


@pytest.mark.parametrize('retired', ['MEMORY_DISABLED', 'PREFERENCE_SUPERSEDED'])
@pytest.mark.parametrize('kind', ['correct', 'disable'])
def test_retired_old_reference_rejected(graph, retired, kind):
    row = graph.add(); token = graph.ref(row)
    row['relation'] = retired  # Even if an out-of-band writer forgot revision.
    rejected_without_write(graph, token, kind)


@pytest.mark.parametrize('kind', ['correct', 'disable'])
def test_old_prefers_reference_cannot_mutate_now_avoids(graph, kind):
    row = graph.add(); token = graph.ref(row)
    row['relation'] = 'AVOIDS'
    rejected_without_write(graph, token, kind)


@pytest.mark.parametrize('change', ['revision', 'properties', 'edge_identity', 'active_false', 'active_invalid'])
def test_changed_bound_state_rejected(graph, change):
    row = graph.add(); token = graph.ref(row)
    if change == 'revision': graph.revisions['owner'] += 1
    elif change == 'properties': row['properties']['confidence'] = .5
    elif change == 'edge_identity': row['id'] = 'recreated-edge'
    elif change == 'active_false': row['properties']['active'] = False
    else: row['properties']['active'] = 1
    rejected_without_write(graph, token)


@pytest.mark.parametrize('kind', ['correct', 'disable'])
def test_other_owner_and_tampered_or_raw_keys_rejected(graph, kind):
    row = graph.add(); graph.add(owner='other')
    token = graph.ref(row)
    rejected_without_write(graph, token, kind, 'other')
    forged = token[:-1] + ('0' if token[-1] != '0' else '1')
    rejected_without_write(graph, forged, kind)
    rejected_without_write(graph, row['concept']['key'], kind)


def test_shared_concept_other_owner_untouched_and_same_text_restore_no_aba(graph):
    row = graph.add(); graph.add(owner='other')
    other = deepcopy(graph.rows['other']); token = graph.ref(row)
    assert action(token, 'disable')['status'] == 'success'
    assert graph.rows['other'] == other
    row = graph.rows['owner'][0]
    row['relation'] = 'PREFERS'; row['properties'] = {}
    graph.revisions['owner'] += 1  # Existing restore bumps the owner revision.
    rejected_without_write(graph, token)
    assert action(graph.ref(graph.rows['owner'][0]))['status'] == 'success'
    assert graph.rows['other'] == other


def test_same_identity_success_consumes_reference_without_rewriting_concept(graph):
    row = graph.add(); token = graph.ref(row); before = deepcopy(graph.rows)
    assert action(token, value='Coffee')['status'] == 'success'
    assert graph.rows == before and graph.revisions['owner'] == 1
    rejected_without_write(graph, token)


def test_concurrent_current_reference_one_winner_and_stale_cannot_win(graph):
    row = graph.add(); old = graph.ref(row); graph.revisions['owner'] += 1
    current = graph.ref(row)
    barrier = threading.Barrier(3)
    def run(token):
        barrier.wait(); return action(token, 'disable')
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, [old, current, current]))
    assert sum(r['status'] == 'success' for r in results) == 1
    assert results[0]['error_code'] == 'stale_source'
    assert graph.revisions['owner'] == 2


def test_transaction_failure_rolls_back_mutation_marker_and_revision(graph):
    row = graph.add(); token = graph.ref(row); before = graph.state()
    graph.fail_write = True
    assert action(token)['status'] == 'error'
    assert graph.state() == before


def test_64_preferences_complete_reference_space_no_display_cache_limit(graph):
    for i in range(64): graph.add(text=f'Reading collection {i}', polarity='PREFERS' if i % 2 == 0 else 'AVOIDS')
    issued = [graph.ref(r) for r in graph.rows['owner']]
    assert len(set(issued)) == 64
    assert all(len(t) == 48 and ':' not in t for t in issued)
    for index in [13, 30, 63]:
        row = next(r for r in graph.rows['owner'] if r['concept']['semantic_text'] == f'Reading collection {index}')
        current = graph.ref(row)
        assert action(current, 'disable')['status'] == 'success'
