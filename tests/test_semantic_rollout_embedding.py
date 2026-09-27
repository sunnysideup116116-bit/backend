import copy
import inspect
import json
from unittest.mock import Mock
import pytest
from matchmaker_agent.concept_identity import canonicalize_concept
from matchmaker_agent.related_interest_contract import embedding_manifest, embedding_fingerprint
from scripts.prepare_related_interest_embeddings import prepare_batch, existing_vector_state, guarded_write
from scripts.semantic_embedding_pipeline import provenance, validate_frozen, canonical_json, sha, SafeGeminiEmbedding, EmbeddingContractError

MODEL = 'models/gemini-embedding-2'
VECTOR = [1.]+[0.]*767


def frozen():
    value = provenance(MODEL, {'name': MODEL, 'provider_reported_version': '2', 'supported_methods': ['embedContent']},
        prepare_batch, package_version=lambda name: '0.8.3' if name == 'google-generativeai' else '0.6.10')
    return {'provenance': value, 'provenance_fingerprint': sha(canonical_json(value)),
        'runtime_compatibility_fingerprint': embedding_fingerprint(MODEL)}


def saved_vector(props):
    f = frozen()
    return {**props, 'embedding_v2': VECTOR.copy(), 'embedding_v2_source_hash': props['semantic_input_hash'],
        'embedding_v2_fingerprint': f['runtime_compatibility_fingerprint'],
        'embedding_v2_manifest': json.dumps(embedding_manifest(MODEL), sort_keys=True),
        'embedding_v2_provenance_fingerprint': f['provenance_fingerprint'],
        'embedding_v2_provenance_manifest': canonical_json(f['provenance'])}


def test_frozen_source_hashes_and_full_pipeline_are_exactly_unchanged():
    f = frozen()
    assert sha(inspect.getsource(prepare_batch)) == '53cbbc0d6f311a783bf3fbb7883d32cd71f16ddd227ac5edd17ceabb13e83547'
    assert validate_frozen(f, f['provenance']) == '53a1d22fd63bcde9be0fa632df532929a05dcb5aa60ded6879b47ad0c2c1c8af'
    assert f['runtime_compatibility_fingerprint'] == '89cc12c59af783aa2953d856c15ba34625e2db129cf5d8bf5712e53af017b640'
    changed = copy.deepcopy(f['provenance']); changed['sdk_version'] = 'new'
    with pytest.raises(ValueError, match='frozen_embedding_pipeline_drift'): validate_frozen(f, changed)


@pytest.mark.parametrize('change', [{'embedding_v2': VECTOR}, {'embedding_v2_source_hash': 'stale'},
    {'embedding_v2_fingerprint': 'b'*64}, {'embedding_v2_provenance_fingerprint': 'c'*64},
    {'embedding_v2_manifest': '{}'}, {'embedding_v2_provenance_manifest': '{}'},
    {'embedding_v2': [True]+[0.]*767}])
def test_partial_or_incompatible_versioned_data_is_never_overwritten(change):
    record = canonicalize_concept('Cooking').as_dict()
    assert existing_vector_state(record, MODEL, frozen()) == 'missing'
    complete = saved_vector(record)
    assert existing_vector_state(complete, MODEL, frozen()) == 'compatible'
    bad = record | change if set(change) == {'embedding_v2'} and change['embedding_v2'] == VECTOR else complete | change
    with pytest.raises(ValueError): existing_vector_state(bad, MODEL, frozen())


class Result(list):
    def single(self): return self[0] if self else None


class Tx:
    def __init__(self, nodes): self.nodes = copy.deepcopy(nodes); self.writes = 0
    def run(self, query, **params):
        if 'SET c.embedding_v2_fingerprint=c.embedding_v2_fingerprint' in query:
            node = self.nodes[params['key']]
            return Result([{'id': node['id'], 'props': copy.deepcopy(node['props'])}])
        if 'RETURN count(c) AS written' in query:
            self.writes += 1
            for item in params['batch']:
                node = self.nodes[item['key']]
                node['props'] = saved_vector(node['props'])
            return Result([{'written': len(params['batch'])}])
        if 'properties(c) AS props' in query:
            return Result([{'key': k, 'props': self.nodes[k]['props']} for k in params['keys']])
        raise AssertionError(query)


def prepared():
    source = {**canonicalize_concept('Cooking').as_dict(), 'embedding': [123.], 'historical_marker': 'unchanged'}
    nodes = {source['key']: {'id': 'synthetic-node', 'props': source}}
    batch = prepare_batch([source], lambda *_a, **_k: [VECTOR], MODEL)
    return nodes, batch


def test_incremental_write_is_idempotent_and_preserves_historical_data():
    nodes, batch = prepared(); tx = Tx(nodes)
    assert guarded_write(tx, batch, nodes, MODEL, frozen(), now=1) == {'written': 1, 'reused': 0}
    assert tx.nodes[batch[0]['key']]['props']['embedding'] == [123.]
    assert guarded_write(tx, batch, nodes, MODEL, frozen(), now=2) == {'written': 0, 'reused': 1}
    assert tx.writes == 1


@pytest.mark.parametrize('change', ['source', 'node', 'concurrent_compatible', 'concurrent_conflict', 'bad_vector'])
def test_compare_under_lock_prevents_stale_or_conflicting_writes(change):
    nodes, batch = prepared(); tx = Tx(nodes); node = tx.nodes[batch[0]['key']]
    if change == 'source': node['props']['semantic_text'] = 'changed'
    if change == 'node': node['id'] = 'new-node'
    if change.startswith('concurrent_'): node['props'] = saved_vector(node['props'])
    if change == 'concurrent_conflict': node['props']['embedding_v2_source_hash'] = 'stale'
    if change == 'bad_vector': batch[0]['vector'] = [0.]*768
    if change == 'concurrent_compatible':
        assert guarded_write(tx, batch, nodes, MODEL, frozen(), now=1) == {'written': 0, 'reused': 1}
    else:
        with pytest.raises(ValueError): guarded_write(tx, batch, nodes, MODEL, frozen(), now=1)
    assert tx.writes == 0


def test_safe_pool_never_retries_a_local_contract_error_or_logs_secrets(capsys):
    provider = SafeGeminiEmbedding.__new__(SafeGeminiEmbedding)
    provider.keys = ['synthetic-secret-a', 'synthetic-secret-b']; provider.active_key = None
    provider.attempts = provider.retries = 0
    provider.failures = dict.fromkeys(('auth_failure', 'quota_exhausted', 'provider_failure'), 0)
    operation = Mock(side_effect=EmbeddingContractError('embedding_request_contract_changed'))
    with pytest.raises(EmbeddingContractError): provider.execute(operation)
    assert operation.call_count == 1 and provider.retries == 0 and provider.active_key is None
    assert 'synthetic-secret' not in capsys.readouterr().out
    provider.close()
    assert not provider.keys
