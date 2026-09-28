"""Projection maintenance cannot manufacture/restore preference authority."""
from copy import deepcopy
from unittest.mock import Mock

import pytest
from scripts import rebuild_neo4j_projection as rebuild
from matchmaker_agent.concept_identity import canonicalize_concept


class Result(list):
    def consume(self): pass
    def single(self, **_kwargs): return self[0] if self else None


class Transaction:
    def __init__(self, authority=None, users=2):
        self.authority=authority or {};self.users=users;self.calls=[]

    def run(self, query, **p):
        self.calls.append((query,p))
        if 'SET u.preference_revision=coalesce(u.preference_revision,0)' in query:
            return Result([dict(revision=4,epoch=0,epoch_at=0,pending=None)])
        if 'projection_v2_authority' in query:
            return Result([dict(owner=owner,**deepcopy(r)) for (owner,_key),rows in self.authority.items()
                if owner in p['owners'] for r in rows])
        if ' AS version' in query:return Result([dict(version=None)])
        if 'RETURN count(DISTINCT user) AS users' in query:
            return Result([dict(users=self.users,relations=2)])
        return Result()


@pytest.fixture
def enqueue(monkeypatch):
    fn=Mock();monkeypatch.setattr(rebuild,'enqueue_keys',fn);return fn


@pytest.mark.parametrize('polarity',['PREFERS','AVOIDS'])
def test_rebuild_verifies_v2_preserves_durable_edges_shared_concepts_and_history(enqueue,polarity):
    identity=canonicalize_concept('Reading').as_dict()
    tx=Transaction({('owner',identity['key']):[{'relation':polarity,'concept':identity}]})
    before=deepcopy(tx.authority)
    result=rebuild.rebuild_projection_transaction(tx,[{'user_id':'owner'},{'user_id':'other'}],
        [{'user_id':'owner','key':identity['key'],'relation':polarity,'label':'Stale ignored Mongo text'}],
        [{'user_id':'other','expires_at':100,'concepts':[{'key':'market','label':'市集'}]}])
    queries='\n'.join(q for q,_ in tx.calls)
    assert result=={'users':2,'relations':2,'verified_v2_preferences':1,'legacy_preferences_written':0}
    assert tx.authority==before
    assert 'DETACH DELETE' not in queries and 'SET user =' not in queries
    assert 'MemoryObservation' not in queries and 'Trait' not in queries
    assert 'MERGE (user)-[:PREFERS]' not in queries and 'MERGE (user)-[:AVOIDS]' not in queries
    assert 'WHERE u.id IN $owners DELETE r' in queries
    enqueue.assert_any_call(tx,'owner',[identity['key']] if polarity=='PREFERS' else [])


@pytest.mark.parametrize('key',['legacy_music','concept_0123456789abcdef','v2_not_a_digest'])
def test_legacy_payload_rejected_before_any_transaction_write(enqueue,key):
    tx=Transaction()
    with pytest.raises(ValueError,match='legacy_preference_rebuild_forbidden'):
        rebuild.rebuild_projection_transaction(tx,[{'user_id':'owner'}],
            [{'user_id':'owner','key':key,'label':'Reading','relation':'PREFERS'}],[])
    assert tx.calls==[];enqueue.assert_not_called()


@pytest.mark.parametrize('kind',['retired','other_owner','wrong_polarity','corrupt'])
def test_no_resurrection_or_guessed_authority(enqueue,kind):
    identity=canonicalize_concept('Reading').as_dict();records=[]
    if kind=='wrong_polarity':records=[dict(relation='AVOIDS',concept=identity)]
    if kind=='corrupt':records=[dict(relation='PREFERS',concept={**identity,'semantic_input_hash':'bad'})]
    tx=Transaction({('owner',identity['key']):records})
    with pytest.raises(ValueError,match='projection_v2_authority_missing'):
        rebuild.rebuild_projection_transaction(tx,[{'user_id':'owner'}],
            [{'user_id':'owner','key':identity['key'],'relation':'PREFERS'}],[])
    assert not any('DELETE' in q or 'MERGE' in q for q,_ in tx.calls)
    enqueue.assert_not_called()


def test_unknown_polarity_is_not_defaulted_to_prefers():
    with pytest.raises(ValueError,match='projection_polarity_unknown'):
        rebuild.preference_references([{'user_id':'owner','concept_key':canonicalize_concept('Reading').key,'stance':'unknown'}])


def test_empty_mongo_cache_does_not_authorize_legacy_graph_state(enqueue):
    tx=Transaction({('owner','legacy'):[{'relation':'PREFERS','concept':{'key':'legacy','label':'Reading'}}]})
    with pytest.raises(ValueError,match='projection_v2_authority_missing'):
        rebuild.rebuild_projection_transaction(tx,[{'user_id':'owner'}],[],[])
    assert not any('DELETE' in q or 'MERGE' in q for q,_ in tx.calls)


def test_reference_loader_never_rekeys_or_uses_payload_label():
    identity=canonicalize_concept('Reading')
    assert rebuild.preference_references([{'user_id':'owner','concept_key':identity.key,'label':'different','stance':'avoid'}])==[
        {'user_id':'owner','key':identity.key,'relation':'AVOIDS'}]


def test_context_cannot_overwrite_reserved_preference_space(enqueue):
    identity=canonicalize_concept('Reading');tx=Transaction()
    with pytest.raises(ValueError,match='context_cannot_overwrite_v2'):
        rebuild.rebuild_projection_transaction(tx,[{'user_id':'owner'}],[],
            [{'user_id':'owner','expires_at':100,'concepts':[{'key':identity.key,'label':'different'}]}])
    assert tx.calls==[]
    assert rebuild.concept_key('Activity',identity.key)!=identity.key


def test_duplicate_owner_and_failed_verification_fail_closed(enqueue):
    tx=Transaction()
    with pytest.raises(ValueError,match='projection_owner_missing_or_ambiguous'):
        rebuild.rebuild_projection_transaction(tx,[{'user_id':'owner'}]*2,[],[])
    assert tx.calls==[]
    with pytest.raises(ValueError,match='projection_verification_failed'):
        rebuild.rebuild_projection_transaction(Transaction(users=0),[{'user_id':'owner'}],[],[])


def test_apply_requires_explicit_scope_before_any_connection(monkeypatch):
    mongo=Mock();monkeypatch.setattr(rebuild,'MongoClient',mongo)
    monkeypatch.setattr(rebuild.sys,'argv',['rebuild','--apply'])
    with pytest.raises(SystemExit) as exc:rebuild.main()
    assert exc.value.code==2;mongo.assert_not_called()
