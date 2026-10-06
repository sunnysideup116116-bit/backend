"""Synthetic offline Event V2 contract/regression; no SDK or production data."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from matchmaker_agent.concept_identity import canonicalize_fresh_concept
from matchmaker_agent.event_v2_contract import (
    EventUnavailable, owner_signals, query_signal, exact_relevance,
)
from matchmaker_agent.event_v2_adapter import Adapter, infrastructure, recheck
from matchmaker_agent.preference_embedding_contract import RUNTIME, PROVENANCE
from matchmaker_agent.related_interest_contract import RELATIONS

NOW = 1800000000.
VECTOR = [1.] + [0.] * 767


def preference(text, *, polarity='PREFERS', ready=True, reference=None, kind='preference'):
    identity = canonicalize_fresh_concept(text)
    props = {**identity.as_dict(), 'kind': kind, 'fidelity_status': 'complete', 'embedding': None}
    if ready:
        props.update(embedding_v2=VECTOR[:], embedding_v2_source_hash=identity.semantic_input_hash,
            embedding_v2_fingerprint=RUNTIME, embedding_v2_provenance_fingerprint=PROVENANCE)
    return {'reference': reference or polarity+text, 'polarity': polarity, 'active': True,
        'expires_at': None, 'props': props}


def recent(text, *, expires=NOW+60):
    return {'reference': 'recent-'+text, 'polarity': 'CURRENTLY_WANTS', 'active': True,
        'expires_at': expires, 'props': {'label': text, 'kind': 'activity', 'key': 'historical-intent-key'}}


def event(tags=('籃球',), vibes=(), eid='event-one'):
    signals = [{'reference': eid+'-tag-'+t, 'type': 'HAS_TAG', 'text': t} for t in tags]
    signals += [{'reference': eid+'-vibe-'+t, 'type': 'HAS_VIBE', 'text': t} for t in vibes]
    return {'event': {'id': eid, 'title': 'Synthetic activity', 'expires_at': NOW+3600,
        'starts_at': NOW+1800, 'ends_at': NOW+3000, 'time_precision': 'datetime',
        'source_url': 'https://example.invalid/activity'}, 'signals': signals}


class Records(list):
    def single(self, **kwargs):
        return self[0] if self else None


class Graph:
    def __init__(self, owners, events=None, *, index=True):
        self.owners, self.events = owners, events or [event()]
        self.index, self.calls, self.ann_overrides = index, [], None

    def run(self, query, **params):
        query = str(query)
        self.calls.append((query, params))
        assert not any(word in query for word in ('MERGE ', 'SET ', 'DELETE ', 'CREATE '))
        if 'RETURN e{' in query:
            return Records(deepcopy([r for r in self.events if r['event']['expires_at']>params['now']]))
        if 'AS reference,type(r)' in query:
            return Records(deepcopy(self.owners.get(params['owner'], [])))
        if 'legacy_evidence_scope' in query:
            return Records([dict(r['props'], polarity=r['polarity']) for r in self.owners[params['owner']]
                if r['polarity'] in {'PREFERS','AVOIDS'} and r.get('active') is not False])
        if 'SHOW VECTOR INDEXES' in query:
            return Records([{'name': params['index_name'], 'state': 'ONLINE' if self.index else 'POPULATING',
                'labelsOrTypes': ['Concept'], 'properties': ['embedding_v2'],
                'options': {'indexConfig': {'vector.dimensions': 768, 'vector.similarity_function': 'COSINE'}}}])
        if 'SHOW INDEXES' in query:
            return Records([{'count': 1, 'user_count': 1}])
        if 'db.index.vector.queryNodes' in query:
            assert params['index_name'] == 'concept_embedding_v2_index'
            rows = self.ann_overrides
            if rows is None:
                by_key = {r['props']['key']: r['props'] for values in self.owners.values()
                    for r in values if r['polarity']=='PREFERS' and r.get('active') is not False}
                rows = [{'props': props, 'score': .97} for props in by_key.values()]
            return Records(deepcopy(rows))
        if 'RETURN u.id AS id LIMIT' in query:
            return Records([{'id': u} for u in self.owners if u!=params['owner'] and u not in params['excluded']])
        raise AssertionError(query)


@pytest.fixture(autouse=True)
def threshold(monkeypatch):
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MIN_SIMILARITY', '.90')


def enable_event_semantics(monkeypatch, tmp_path):
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ENABLED', 'on')
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ROLLOUT_MODE', 'enabled_accounts')
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MODE', 'active')
    monkeypatch.setenv('MATCH_RELATED_INTEREST_KILL_SWITCH_FILE', str(tmp_path / 'not-engaged'))


def adapter(graph, *, validate=None, vectors=None, now=lambda: NOW, allowed=lambda _: True, eligible=lambda _: True,
        fallback_model=None):
    return Adapter(graph, None, 'deepseek-v4.1-flash:cloud', deadline=100,
        clock=lambda: 0., now=now, eligible=eligible, allowed=allowed,
        vectors=vectors or (lambda signals, **_: {s.source_hash: VECTOR[:] for s in signals}),
        validate=validate or (lambda _q, concepts, *_a, **_k:
            ([{**c, 'relation': 'sibling_related'} for c in concepts], {'accepted': len(concepts), 'rejected': 0, 'error': 0})),
        fallback_model=fallback_model)


def complete_decision_counts(relation, count, *, accepted, rejected, error=0, attempts=1,
        attempt_errors=0, retries=0, job_unavailable=0):
    result = {name: 0 for name in RELATIONS}
    result[relation] = count
    result.update(accepted=accepted, rejected=rejected, error=error,
        attempts=attempts, attempt_errors=attempt_errors, retries=retries,
        job_unavailable=job_unavailable)
    return result


def test_v2_ready_legacy_null_is_not_pending():
    graph = Graph({'owner': [preference('投籃')]})
    assert infrastructure(graph)['semantic_ready'] is True
    positive, _ = owner_signals(graph.owners['owner'], NOW)
    assert positive[0].vector_ready is True
    assert not any('.embedding ' in query for query,_ in graph.calls)


def test_exact_works_without_vectors_index_or_provider_and_is_user_to_event():
    graph = Graph({'a': [preference('籃球', ready=False)], 'b': [preference('爵士樂', ready=False)]},
        [event(('籃球','爵士樂'))], index=False)
    never = Mock(side_effect=AssertionError('exact must not enter semantic'))
    service = adapter(graph, validate=never, vectors=never, allowed=lambda _: False)
    result = service.select('a', [])
    assert result[0]['candidate_id'] == 'b'
    assert graph.owners['a'][0]['props']['key'] != graph.owners['b'][0]['props']['key']
    assert service.counts['ann_calls'] == service.counts['validator_calls'] == 0
    never.assert_not_called()
    assert not any('SHOW VECTOR' in query or 'queryNodes' in query for query,_ in graph.calls)


def test_semantic_uses_only_v2_index_and_current_owner_vector():
    graph = Graph({'owner': [preference('投籃')]})
    service = adapter(graph)
    assert service.relevance('owner')['event-one'][0]['basis'] == 'semantic'
    assert service.counts['ann_calls'] == 1
    assert service.counts['validator_calls'] == 1
    assert not any('.embedding ' in query or 'concept_embedding_index' in query for query,_ in graph.calls)


@pytest.mark.parametrize('field,value', [
    ('embedding_v2_source_hash','0'*64), ('embedding_v2_fingerprint','0'*64),
    ('embedding_v2_provenance_fingerprint','0'*64), ('embedding_v2',[0.]*768),
    ('embedding_v2',[float('nan')]+[0.]*767),
])
def test_incompatible_owner_vector_never_semantic_evidence(field,value):
    row = preference('投籃'); row['props'][field] = value
    graph = Graph({'owner': [row]})
    never = Mock(side_effect=AssertionError('unready source cannot be encoded'))
    with pytest.raises(EventUnavailable, match='event_positive_embedding_pending'):
        adapter(graph, vectors=never).relevance('owner')
    never.assert_not_called()


@pytest.mark.parametrize('field,value', [
    ('embedding_v2_source_hash','0'*64), ('embedding_v2_fingerprint','0'*64),
    ('embedding_v2_provenance_fingerprint','0'*64),
])
def test_incompatible_ann_hit_is_dropped(field,value):
    graph = Graph({'owner': [preference('投籃')]})
    props = deepcopy(graph.owners['owner'][0]['props']); props[field]=value
    graph.ann_overrides = [{'props': props, 'score': .97}]
    never = Mock(side_effect=AssertionError('invalid ANN cannot reach validator'))
    assert adapter(graph, validate=never).relevance('owner') == {}
    never.assert_not_called()


@pytest.mark.parametrize('field',['canonical_key','concept_key'])
def test_optional_stored_identity_keys_cannot_disagree_with_authoritative_source(field):
    row=preference('籃球');row['props'][field]='v2_'+'0'*48
    with pytest.raises(EventUnavailable,match='event_preference_source_unverified'):
        adapter(Graph({'owner':[row]})).relevance('owner')


def test_systemic_index_failure_is_typed_but_does_not_block_exact():
    graph = Graph({'owner': [preference('投籃')]}, index=False)
    readiness = infrastructure(graph)
    assert readiness['ready'] and readiness['exact_ready'] and not readiness['semantic_ready']
    with pytest.raises(EventUnavailable, match='event_v2_index_unavailable') as raised:
        adapter(graph).relevance('owner')
    assert raised.value.systemic is True


def test_exact_avoidance_blocks_and_unresolved_negative_is_unavailable():
    positive, negative = owner_signals([preference('爵士樂'), preference('籃球', polarity='AVOIDS')], NOW)
    result, unknown = exact_relevance(positive, negative,
        {'blocked': [query_signal('籃球','tag','x'),query_signal('爵士樂','tag','y')]})
    assert result == {} and unknown is False
    graph = Graph({'owner': [preference('籃球'), preference('爵士樂', polarity='AVOIDS', ready=False)]})
    with pytest.raises(EventUnavailable, match='event_negative_unavailable'):
        adapter(graph).relevance('owner')


def test_avoids_only_never_positive():
    graph = Graph({'owner': [preference('籃球', polarity='AVOIDS')]})
    assert adapter(graph).relevance('owner') == {}


def test_currently_wants_exact_retains_expiry_and_activity_tag_rule():
    graph = Graph({'owner': [recent('籃球')]})
    assert adapter(graph).relevance('owner')['event-one'][0]['source_kind'] == 'recent'
    graph.owners['owner'][0]['expires_at'] = NOW
    assert adapter(graph).relevance('owner') == {}
    graph.owners['owner'][0]['expires_at'] = float('inf')
    with pytest.raises(EventUnavailable, match='event_recent_source_invalid'):
        adapter(graph).relevance('owner')


def test_request_local_cache_cannot_revive_expired_recent_or_activity():
    graph=Graph({'owner':[recent('籃球')]})
    clock=[NOW]
    service=adapter(graph,now=lambda:clock[0])
    assert service.relevance('owner')
    clock[0]=NOW+60
    assert service.relevance('owner')=={}
    graph=Graph({'owner':[preference('籃球')]})
    service=adapter(graph,now=lambda:clock[0])
    assert service.relevance('owner')
    clock[0]=NOW+3600
    assert service.relevance('owner')=={}


def test_currently_wants_semantic_is_separate_ephemeral_source():
    graph = Graph({'owner': [recent('投籃')]})
    service = adapter(graph)
    assert service.relevance('owner')['event-one'][0]['source_kind']=='recent'
    assert not any('queryNodes' in q for q,_ in graph.calls)  # transient recent source is not durable ANN evidence
    assert graph.owners['owner'][0]['props'].get('canonicalization_version') is None


def test_activity_does_not_exact_match_vibe_but_interest_does():
    signals = {'event': [query_signal('戶外','vibe','vibe')]}
    p,_ = owner_signals([recent('戶外')], NOW)
    assert exact_relevance(p,[],signals)[0] == {}
    p,_ = owner_signals([preference('戶外')], NOW)
    assert exact_relevance(p,[],signals)[0]


def test_partial_error_excludes_error_signals_only():
    graph = Graph({'owner': [preference('投籃')]}, [event(('籃球','羽球'))])
    def validate(_q, concepts, *_a, **_k):
        return [{**concepts[0], 'relation': 'sibling_related'}], {'accepted': 1, 'error': 1, 'rejected': 0}
    evidence = adapter(graph, validate=validate).relevance('owner')['event-one']
    assert len(evidence)==1 and evidence[0]['event_text']=='籃球'


def test_trusted_reject_plus_partial_error_is_normal_no_match():
    graph = Graph({'owner': [preference('投籃')]}, [event(('籃球','羽球'))])
    validate = lambda *_a, **_k: ([], {'accepted': 0, 'rejected': 1, 'error': 1})
    assert adapter(graph, validate=validate).relevance('owner') == {}


@pytest.mark.parametrize('counts', [{'error': 2}, {'job_unavailable': 1, 'error': 2, 'accepted': 1}])
def test_zero_trusted_or_systemic_validator_error_is_unavailable(counts):
    graph = Graph({'owner': [preference('投籃')]})
    with pytest.raises(EventUnavailable, match='semantic_validator_unavailable'):
        adapter(graph, validate=lambda *_a, **_k: ([], counts)).relevance('owner')


def test_zero_trusted_selection_stays_typed_unavailable_for_breaker_accounting():
    graph=Graph({'a':[preference('投籃')], 'b':[preference('籃球')]})
    service=adapter(graph,validate=lambda *_a,**_k:([],{'error':2}))
    with pytest.raises(EventUnavailable,match='^semantic_validator_unavailable$'):
        service.select('a',[])


def test_partial_error_in_other_owner_does_not_escalate_trusted_request_to_unavailable():
    graph=Graph({'a':[preference('投籃')], 'b':[preference('射籃')]})
    def validate(query,concepts,*_a,**_k):
        if query=='投籃': return [{**concepts[0],'relation':'sibling_related'}],{'accepted':1,'error':0}
        return [],{'error':2}
    service=adapter(graph,validate=validate)
    assert service.select('a',[])==[]
    assert service.validator_counts['accepted']==1 and service.validator_counts['error']==2


def test_kill_disabled_semantic_never_calls_provider():
    graph = Graph({'owner': [preference('投籃')]})
    never=Mock(side_effect=AssertionError('disabled'))
    with pytest.raises(EventUnavailable, match='event_semantic_disabled'):
        adapter(graph, vectors=never, allowed=lambda _: False).relevance('owner')
    never.assert_not_called()


def test_pending_candidate_does_not_stop_other_candidate_exact_scan():
    graph = Graph({'owner': [preference('籃球')], 'pending': [preference('投籃', ready=False)],
        'ready': [preference('籃球', ready=False)]})
    assert adapter(graph).select('owner', [])[0]['candidate_id']=='ready'


def test_exact_on_one_activity_does_not_suppress_related_relevance_to_another():
    graph = Graph({'a':[preference('慢跑')], 'b':[preference('越野跑')]},
        [event(('慢跑',),eid='road'),event(('越野跑',),eid='trail')])
    service = adapter(graph)
    match = service.select('a',[])[0]
    assert match['event_id']=='road' and match['candidate_id']=='b'
    assert match['target_user_concepts']==['慢跑']
    assert match['candidate_user_concepts']==['越野跑']
    assert service.counts['validator_calls']==1


def test_qualified_exact_bridge_is_selected_before_semantic_candidate():
    graph=Graph({'a':[preference('籃球')], 'semantic-first':[preference('投籃')],
        'exact-last':[preference('籃球',ready=False)]})
    never=Mock(side_effect=AssertionError('qualified exact already available'))
    service=adapter(graph,vectors=never,validate=never)
    assert service.select('a',[])[0]['candidate_id']=='exact-last'
    assert service.counts['ann_calls']==service.counts['validator_calls']==0
    never.assert_not_called()


def test_semantic_proof_rechecks_kill_and_current_threshold(monkeypatch):
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ENABLED','on')
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ROLLOUT_MODE','enabled_accounts')
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MODE','active')
    graph=Graph({'a':[preference('慢跑')],'b':[preference('越野跑')]},[event(('慢跑',))])
    match=adapter(graph).select('a',[])[0]
    kwargs={'deadline':100,'clock':lambda:0.,'now':lambda:NOW,'eligible':lambda _:True}
    assert recheck(graph,match['event_receipt'],'a','b',**kwargs)
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MIN_SIMILARITY','.99')
    assert not recheck(graph,match['event_receipt'],'a','b',**kwargs)
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MIN_SIMILARITY','.90')
    monkeypatch.setattr('matchmaker_agent.related_interest_contract.kill_engaged',lambda:True)
    assert not recheck(graph,match['event_receipt'],'a','b',**kwargs)


@pytest.mark.parametrize('change', ['disabled','new_negative','retired','polarity','source','event','expiry','revision'])
def test_final_recheck_rejects_current_state_changes(change):
    graph = Graph({'a': [recent('籃球')], 'b': [preference('籃球')]})
    time=[NOW]
    selected = adapter(graph, now=lambda: time[0]).select('a', [])[0]
    assert recheck(graph, selected['event_receipt'], 'a','b', deadline=100,
        now=lambda:time[0], clock=lambda:0., eligible=lambda _:True)
    eligible = lambda _:True
    if change=='disabled': eligible=lambda u:u!='b'
    elif change=='new_negative': graph.owners['b'].append(preference('爵士樂',polarity='AVOIDS'))
    elif change=='retired': graph.owners['b']=[]
    elif change=='polarity': graph.owners['b'][0]['polarity']='AVOIDS'
    elif change=='source': graph.owners['b'][0]['props']['semantic_text']='羽球'
    elif change=='event': graph.events[0]['signals'][0]['text']='羽球'
    elif change=='expiry': time[0]=NOW+60
    elif change=='revision': graph.owners['b'][0]['owner_revision']=42
    assert not recheck(graph,selected['event_receipt'],'a','b',deadline=100,
        now=lambda:time[0],clock=lambda:0.,eligible=eligible)


def test_forged_other_owner_or_expired_receipt_never_rechecks():
    graph=Graph({'a':[preference('籃球')],'b':[preference('籃球')]})
    proof=adapter(graph).select('a',[])[0]['event_receipt']
    for token, owner, candidate, stamp in [(proof,'b','a',NOW),('forged','a','b',NOW),(proof,'a','b',NOW+61)]:
        assert not recheck(graph,token,owner,candidate,deadline=100,clock=lambda:0.,now=lambda:stamp,eligible=lambda _:True)


def test_exact_final_proof_does_not_wait_for_or_depend_on_derived_vector_updates():
    graph=Graph({'a':[preference('籃球',ready=False)],'b':[preference('籃球',ready=False)]})
    graph.owners['a'][0]['props']['embedding_v2']=[float('nan')]+[0.]*767
    match=adapter(graph).select('a',[])[0]
    graph.owners['a'][0]['props'].update(preference('籃球')['props'])
    assert recheck(graph,match['event_receipt'],'a','b',deadline=100,
        clock=lambda:0.,now=lambda:NOW,eligible=lambda _:True)


def test_semantic_proof_binds_current_vector_state(monkeypatch):
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ENABLED','on')
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ROLLOUT_MODE','enabled_accounts')
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MODE','active')
    graph=Graph({'a':[preference('慢跑')],'b':[preference('越野跑')]},[event(('慢跑',))])
    match=adapter(graph).select('a',[])[0]
    graph.owners['b'][0]['props']['embedding_v2_source_hash']='0'*64
    assert not recheck(graph,match['event_receipt'],'a','b',deadline=100,
        clock=lambda:0.,now=lambda:NOW,eligible=lambda _:True)


def test_ambiguous_source_fail_closed_and_legacy_positive_is_not_migrated():
    graph=Graph({'owner':[preference('籃球'),preference('籃球',reference='duplicate')]})
    with pytest.raises(EventUnavailable,match='ambiguous'):
        adapter(graph).relevance('owner')
    graph.owners['owner']=[{'reference':'legacy','polarity':'PREFERS','props':{'key':'籃球','label':'籃球'}}]
    with pytest.raises(EventUnavailable,match='source_unverified'):
        adapter(graph).relevance('owner')


def test_active_endpoint_uses_truthful_activity_wording_without_preference_claim(monkeypatch):
    import agent_api
    from matchmaker_agent.event_v2_api import EventMatches
    selected={'user_id':'a','candidate_id':'b','event_id':'e','event_name':'Synthetic event',
        'target_links':['慢跑'],'candidate_links':['越野跑'],'target_user_concepts':['慢跑'],
        'candidate_user_concepts':['越野跑'],'target_source_kinds':['durable'],'candidate_source_kinds':['durable']}
    monkeypatch.setattr('matchmaker_agent.event_v2_api.find_matches',lambda *_a:EventMatches([selected],{'semantic_triggered':True}))
    monkeypatch.setattr(agent_api.agent,'choose_event_invitation_order',lambda _:{'first':'target'})
    never=Mock(side_effect=AssertionError('template must not invent shared preference'))
    monkeypatch.setattr(agent_api.agent,'generate_proactive_event_hook',never)
    result=agent_api.proactive_event_match(agent_api.ProactiveEventMatchRequest(user_id='a'))
    assert result['status']=='success'
    assert '相關' in result['first_hook'] and 'Synthetic event' in result['first_hook']
    assert not any(word in result['first_hook'] for word in ('共同偏好','相同偏好','shared','identical'))
    never.assert_not_called()


def test_same_trusted_directional_pair_is_validated_once_across_repeated_activities(monkeypatch, tmp_path):
    """Repeated Event references to one Q/C pair share a trusted request-local decision."""
    enable_event_semantics(monkeypatch, tmp_path)
    owners = {'requester': [preference('打籃球', reference='requester-preference')]}
    owners.update({f'candidate-{index:02d}': [
        preference('打籃球', reference=f'candidate-{index:02d}-preference')
    ] for index in range(45)})
    activities = [event(('三對三籃球',), eid=f'activity-{index:02d}') for index in range(10)]
    graph = Graph(owners, activities)
    input_calls = []

    def valid_reject(query, concepts, *_args, **_kwargs):
        input_calls.append((query, tuple(item['semantic_text'] for item in concepts)))
        return [], complete_decision_counts('candidate_more_broad', len(concepts), accepted=0,
            rejected=len(concepts))

    service = adapter(graph, validate=valid_reject)
    assert service.select('requester', []) == []
    logical_calls = len(activities)
    assert len(input_calls) == 1
    assert set(input_calls) == {('打籃球', ('三對三籃球',))}
    assert service.counts['validator_calls'] == 1
    assert service.counts['ann_calls'] == 1
    assert graph.calls and not any(any(word in query for word in ('MERGE ', 'SET ', 'DELETE ', 'CREATE '))
        for query, _params in graph.calls)
    # Reuse remains request-scoped. A new adapter must perform its own call.
    another = adapter(graph, validate=valid_reject)
    another.relevance('requester', event_ids=['activity-00'])
    assert len(input_calls) == 2
    assert logical_calls > len(input_calls)


def test_repeated_timeout_retry_shape_avoids_outer_deadline_only_for_reused_pair(monkeypatch, tmp_path):
    """Synthetic Run #24 timing shape: 6 s timeout + trusted retry across repeated activity refs."""
    from types import SimpleNamespace
    import matchmaker_agent.related_interest_validator as validator

    enable_event_semantics(monkeypatch, tmp_path)

    class Clock:
        def __init__(self):
            self.value = 0.0

        def __call__(self):
            return self.value

        def advance(self, seconds):
            self.value += seconds

    clock = Clock()
    activities = [event(('三對三籃球',), eid=f'timed-{index:02d}') for index in range(10)]
    graph = Graph({'requester': [preference('打籃球')]}, activities)
    attempts = []

    def slow_then_trusted_reject(_client, *, timeout, model, **_request):
        attempt = len(attempts) + 1
        attempts.append((attempt, timeout))
        if attempt % 2:
            clock.advance(timeout)
            raise TimeoutError('synthetic transport delay')
        clock.advance(min(2.0, timeout))
        response = SimpleNamespace(model=model, usage=None, choices=[SimpleNamespace(
            finish_reason='stop', message=SimpleNamespace(content=
                '{"results":[{"id":"0","relation":"candidate_more_broad"}]}'))])
        return response

    monkeypatch.setattr(validator, '_completion', slow_then_trusted_reject)
    service = Adapter(graph, object(), 'deepseek-v4.1-flash:cloud', deadline=38.0,
        clock=clock, now=lambda: NOW, eligible=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {signal.source_hash: VECTOR[:] for signal in signals})
    assert service.select('requester', []) == []
    assert clock() == 8.0
    assert len(attempts) == 2
    assert service.counts['validator_calls'] == 1
    assert service.validator_counts['rejected'] == len(activities)


def test_trusted_accept_is_rebound_across_owner_fanout(monkeypatch, tmp_path):
    enable_event_semantics(monkeypatch, tmp_path)
    graph = Graph({
        'requester': [preference('慢跑', reference='requester-preference')],
        'candidate': [preference('慢跑', reference='candidate-preference')],
    }, [event(('越野跑',), eid='shared-activity')])
    calls = []

    def trusted_accept(query, concepts, *_args, **_kwargs):
        calls.append((query, tuple(item['semantic_text'] for item in concepts)))
        return ([{**concept, 'relation': 'sibling_related'} for concept in concepts],
            complete_decision_counts('sibling_related', len(concepts), accepted=len(concepts), rejected=0))

    service = adapter(graph, validate=trusted_accept)
    selected = service.select('requester', [])
    assert len(selected) == 1
    assert selected[0]['candidate_id'] == 'candidate'
    assert len(calls) == 1
    assert service.counts['validator_calls'] == 1
    assert service.decisions['requester', 'shared-activity'][0]['user_ref'] == 'requester-preference'
    assert service.decisions['candidate', 'shared-activity'][0]['user_ref'] == 'candidate-preference'
    assert service.decisions['requester', 'shared-activity'][0]['event_ref'] == 'shared-activity-tag-越野跑'
    assert service.decisions['candidate', 'shared-activity'][0]['event_ref'] == 'shared-activity-tag-越野跑'


@pytest.mark.parametrize('systemic', [False, True])
def test_error_or_systemic_unavailable_is_never_cached(monkeypatch, tmp_path, systemic):
    enable_event_semantics(monkeypatch, tmp_path)
    graph = Graph({'requester': [preference('打籃球')]}, [
        event(('三對三籃球',), eid='first'), event(('三對三籃球',), eid='second')])
    calls = []

    def fail_then_reject(query, concepts, *_args, **_kwargs):
        calls.append((query, tuple(item['semantic_text'] for item in concepts)))
        if len(calls) == 1:
            counts = {'accepted': 0, 'rejected': 0, 'error': len(concepts),
                'attempts': 1, 'attempt_errors': 1, 'retries': 0}
            if systemic:
                counts['job_unavailable'] = 1
            return [], counts
        return [], complete_decision_counts('unrelated', len(concepts), accepted=0, rejected=len(concepts))

    service = adapter(graph, validate=fail_then_reject)
    with pytest.raises(EventUnavailable, match='semantic_validator_unavailable'):
        service.relevance('requester', event_ids=['first'])
    assert service.relevance('requester', event_ids=['second']) == {}
    assert len(calls) == 2


def test_kill_engaged_after_trusted_decision_blocks_cached_reuse(monkeypatch, tmp_path):
    from matchmaker_agent.semantic_rollout_policy import requester_route_allowed

    enable_event_semantics(monkeypatch, tmp_path)
    kill = tmp_path / 'related-interest-disabled'
    monkeypatch.setenv('MATCH_RELATED_INTEREST_KILL_SWITCH_FILE', str(kill))
    graph = Graph({'requester': [preference('打籃球')]}, [
        event(('三對三籃球',), eid='first'), event(('三對三籃球',), eid='second')])
    calls = []

    def trusted_reject(query, concepts, *_args, **_kwargs):
        calls.append((query, tuple(item['semantic_text'] for item in concepts)))
        return [], complete_decision_counts('unrelated', len(concepts), accepted=0, rejected=len(concepts))

    service = adapter(graph, validate=trusted_reject, allowed=requester_route_allowed)
    assert service.relevance('requester', event_ids=['first']) == {}
    kill.touch()
    with pytest.raises(EventUnavailable, match='event_semantic_disabled'):
        service.relevance('requester', event_ids=['second'])
    assert len(calls) == 1


def test_unique_pairs_still_exhaust_the_original_event_deadline(monkeypatch, tmp_path):
    """Deduplication must not turn genuinely unavailable unique pairs into a match."""
    enable_event_semantics(monkeypatch, tmp_path)
    class Clock:
        def __init__(self):
            self.value = 0.0

        def __call__(self):
            return self.value

        def advance(self, seconds):
            self.value += seconds

    clock = Clock()
    activities = [event((f'activity-{index:02d}',), eid=f'unique-{index:02d}') for index in range(6)]
    graph = Graph({'requester': [preference('打籃球')]}, activities)
    seen = []

    def bounded_transport(query, concepts, *_args, deadline, clock, **_kwargs):
        source = tuple(item['semantic_text'] for item in concepts)
        seen.append((query, source))
        remaining = deadline - clock()
        assert remaining > 0
        # Existing 6 s maximum attempt; a complete trusted response takes 2 s
        # after the first timeout. The last unique pair has only 6 s left.
        clock.advance(min(6.0, remaining))
        if remaining < 8.0:
            return [], {'accepted': 0, 'rejected': 0, 'error': len(concepts),
                'attempts': 1, 'attempt_errors': 1, 'retries': 0}
        clock.advance(2.0)
        return [], {'accepted': 0, 'rejected': len(concepts), 'error': 0,
            'attempts': 2, 'attempt_errors': 1, 'retries': 1, 'unrelated': len(concepts)}

    service = Adapter(graph, None, 'deepseek-v4.1-flash:cloud', deadline=38.0,
        clock=clock, now=lambda: NOW, eligible=lambda _owner: True,
        vectors=lambda signals, **_kwargs: {signal.source_hash: VECTOR[:] for signal in signals},
        validate=bounded_transport)
    with pytest.raises(EventUnavailable, match='semantic_validator_unavailable'):
        service.select('requester', [])
    assert len(seen) == 5
    assert len({pair for pair in seen}) == 5
    assert clock() == 38.0


def test_partial_error_never_populates_the_trusted_relation_cache(monkeypatch, tmp_path):
    enable_event_semantics(monkeypatch, tmp_path)
    graph = Graph({'requester': [preference('打籃球')]}, [
        event(('三對三籃球',), eid='first'), event(('三對三籃球',), eid='second')])
    calls = []

    def partial_error(query, concepts, *_args, **_kwargs):
        calls.append((query, tuple(item['semantic_text'] for item in concepts)))
        if len(calls) == 1:
            return [], {'accepted': 0, 'rejected': 0, 'error': len(concepts),
                'attempts': 2, 'attempt_errors': 2, 'retries': 1}
        return [], {'accepted': 0, 'rejected': len(concepts), 'error': 0,
            'attempts': 1, 'attempt_errors': 0, 'retries': 0, 'unrelated': len(concepts)}

    service = adapter(graph, validate=partial_error)
    with pytest.raises(EventUnavailable, match='semantic_validator_unavailable'):
        service.relevance('requester', event_ids=['first'])
    assert service.relevance('requester', event_ids=['second']) == {}
    assert len(calls) == 2


def test_partial_trusted_result_with_error_is_not_cached(monkeypatch, tmp_path):
    enable_event_semantics(monkeypatch, tmp_path)
    activities = [event(('三對三籃球', '籃球賽事'), eid=name) for name in ('first', 'second')]
    graph = Graph({'requester': [preference('打籃球')]}, activities)
    calls = []

    def partial_trusted(query, concepts, *_args, **_kwargs):
        calls.append((query, tuple(item['semantic_text'] for item in concepts)))
        return ([{**concepts[0], 'relation': 'sibling_related'}], {
            **complete_decision_counts('sibling_related', 1, accepted=1, rejected=0,
                error=1, attempts=2, attempt_errors=2, retries=1),
        })

    service = adapter(graph, validate=partial_trusted)
    first = service.relevance('requester', event_ids=['first'])
    second = service.relevance('requester', event_ids=['second'])
    assert first['first'][0]['relation'] == second['second'][0]['relation'] == 'sibling_related'
    assert first['first'][0]['event_ref'] != second['second'][0]['event_ref']
    assert len(calls) == 2


def test_event_adapter_forwards_secondary_model_to_shared_validator(monkeypatch, tmp_path):
    enable_event_semantics(monkeypatch, tmp_path)
    seen = []

    def validate(query, concepts, *_args, **kwargs):
        seen.append(kwargs.get('fallback_model'))
        return [], complete_decision_counts('unrelated', len(concepts), accepted=0, rejected=len(concepts))

    graph = Graph({'owner': [preference('打籃球')]}, [event(('籃球',))])
    service = adapter(graph, validate=validate, fallback_model='glm-5.3-flash:cloud')
    assert service.relevance('owner', event_ids=['event-one']) == {}
    assert seen and set(seen) == {'glm-5.3-flash:cloud'}


def test_request_local_cache_key_separates_secondary_configuration():
    from matchmaker_agent.event_v2_adapter import _validator_relation_key
    concepts = [{'concept_key': 'k', 'semantic_text': 'text'}]
    plain = _validator_relation_key('q', concepts, 'deepseek-v4.1-flash:cloud')
    fallback = _validator_relation_key('q', concepts, 'deepseek-v4.1-flash:cloud', 'glm-5.3-flash:cloud')
    assert plain and fallback and plain != fallback


def test_exact_bridge_is_independent_of_validator_failover(monkeypatch, tmp_path):
    enable_event_semantics(monkeypatch, tmp_path)
    graph = Graph({'a': [preference('籃球')], 'exact-last': [preference('籃球', ready=False)]})
    never = Mock(side_effect=AssertionError('exact bridge must not call validator'))
    service = adapter(graph, vectors=never, validate=never, fallback_model='glm-5.3-flash:cloud')
    assert service.select('a', [])[0]['candidate_id'] == 'exact-last'
    assert service.counts['ann_calls'] == service.counts['validator_calls'] == 0
    never.assert_not_called()


def test_recheck_endpoint_rejects_unsigned_request_before_graph(monkeypatch):
    import agent_api
    from fastapi.testclient import TestClient
    never=Mock(side_effect=AssertionError('unsigned cannot access Graph'))
    monkeypatch.setattr('matchmaker_agent.event_v2_api.session_for',never)
    response=TestClient(agent_api.app).post('/api/events/v2/recheck',json={'owner':'a','candidate':'b','receipt':'x'*43})
    assert response.status_code==403
    never.assert_not_called()


def test_disabled_or_noncanonical_owner_never_retrieves(monkeypatch):
    import agent_api
    never=Mock(side_effect=AssertionError('must not resolve guessed identity'))
    monkeypatch.setattr('matchmaker_agent.event_v2_api.find_matches',never)
    response=agent_api.proactive_event_match(agent_api.ProactiveEventMatchRequest(user_id='a b'))
    assert response['status']=='error'
    never.assert_not_called()
