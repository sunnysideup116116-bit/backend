"""Real restore handler + synthetic atomic Graph; no database/provider/network."""
import ast
import asyncio
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from neo4j import unit_of_work
from matchmaker_agent import preference_action_reference as refs
from matchmaker_agent import preference_restore as restore
from matchmaker_agent.concept_identity import canonicalize_concept, stored_concept_identity
from matchmaker_agent.preference_write_fence import lock_preferences, bump_preferences, PreferenceFenceError, fence_error_result


class Result(list):
    def single(self, **_kwargs):
        assert len(self) <= 1
        return self[0] if self else None

    def consume(self):
        pass


class Graph:
    def __init__(self):
        self.concepts = {}
        self.edges = []
        self.revisions = {'owner': 0, 'other': 0}
        self.pending = {'owner': [], 'other': []}
        self.serial = 0
        self.lock = threading.RLock()
        self.fail = False

    def add(self, props, relation='MEMORY_DISABLED', owner='owner', **edge_props):
        self.concepts.setdefault(props['key'], deepcopy(props))
        self.serial += 1
        edge = dict(owner=owner, key=props['key'], relation=relation, props=edge_props, id=str(self.serial))
        self.edges.append(edge)
        return edge

    def snapshot(self):
        return deepcopy((self.concepts, self.edges, self.revisions, self.pending, self.serial))

    def execute_write(self, callback):
        with self.lock:
            before = self.snapshot()
            try:
                return callback(self)
            except Exception:
                self.concepts, self.edges, self.revisions, self.pending, self.serial = before
                raise

    def __enter__(self): return self
    def __exit__(self, *_args): pass

    def run(self, q, **p):
        owner = p.get('owner')
        if 'SET u.preference_revision=coalesce(u.preference_revision,0)' in q and '+1' not in q:
            if owner not in self.revisions: return Result()
            return Result([dict(revision=self.revisions[owner], epoch=0, epoch_at=0, pending=None)])
        if 'SET u.preference_revision=coalesce(u.preference_revision,0)+1' in q:
            self.revisions[owner] += 1
            return Result()
        if 'SET u.preference_action_projection_keys' in q:
            self.pending[owner].append(p['key'])
            return Result()
        if ' AS keys' in q:return Result([{'keys':self.pending[owner]}])
        if 'restore_source' in q:
            return Result([dict(id=e['id'], disabled=deepcopy(e['props']), concept=deepcopy(self.concepts[e['key']]))
                for e in self.edges if e['owner']==owner and e['key']==p['key'] and e['relation']=='MEMORY_DISABLED'])
        if 'restore_target_identity' in q:
            return Result([dict(concept=deepcopy(self.concepts[p['key']]))] if p['key'] in self.concepts else [])
        if 'restore_target_associations' in q:
            return Result([dict(id=e['id'],relation=e['relation'],active=e['props'].get('active'))
                for e in self.edges if e['owner']==owner and e['key']==p['key']])
        if 'restore_create_or_reuse_v2' in q:
            self.concepts.setdefault(p['key'],deepcopy(p['props']))
            if self.fail:raise RuntimeError('synthetic failure after target creation')
            return Result([dict(concept=deepcopy(self.concepts[p['key']]))])
        if 'restore_active_v2' in q:
            source=next(e for e in self.edges if e['id']==p['association'] and e['owner']==owner)
            relation='AVOIDS' if '[:AVOIDS]' in q else 'PREFERS'
            if not any(e['owner']==owner and e['key']==p['target'] and e['relation']==relation for e in self.edges):
                self.add(self.concepts[p['target']],relation,owner)
            if 'SET d.restored_v2_key' in q:source['props'].update(restored_v2_key=p['target'],restored_at=p['now'])
            else:self.edges.remove(source)
            return Result()
        if 'restore_context' in q:
            source=next(e for e in self.edges if e['id']==p['association'] and e['owner']==owner)
            self.edges.remove(source)
            if (p['expires'] or 0)>p['now']:
                self.add(self.concepts[p['key']],'CURRENTLY_WANTS',owner,expires_at=p['expires'])
            return Result()
        if 'RETURN properties(c) AS props' in q:
            return Result([dict(props=deepcopy(self.concepts[e['key']])) for e in self.edges
                if e['owner']==owner and e['relation']=='PREFERS' and e['key'] in p['keys']])
        if 'preference_embedding_state' in q:
            for key in p['keys']:
                self.concepts[key].update(preference_embedding_state='pending',preference_embedding_source_hash=self.concepts[key]['semantic_input_hash'])
            return Result()
        raise AssertionError('Unhandled test query: '+q[:80])


def invoke(graph, key, owner='owner'):
    driver=MagicMock();driver.__enter__.return_value=driver;driver.session.return_value=graph
    source=ast.parse(Path(__file__).with_name('agent_api.py').read_text())
    functions=[n for n in source.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))
        and n.name in {'_require_action_live','memory_action'}]
    for node in functions:node.decorator_list=[]
    scope=dict(re=re,time=time,MemoryActionRequest=SimpleNamespace,GraphDatabase=SimpleNamespace(driver=lambda *_a,**_k:driver),
        _neo4j_config=lambda: ('synthetic',None,'synthetic'),unit_of_work=unit_of_work,
        lock_preferences=lock_preferences,bump_preferences=bump_preferences,action_reference=refs,
        PreferenceFenceError=PreferenceFenceError,fence_error_result=fence_error_result)
    exec(compile(ast.fix_missing_locations(ast.Module(body=functions,type_ignores=[])),'real-memory-action','exec'),scope)
    return asyncio.run(scope['memory_action'](SimpleNamespace(action='restore',user_id=owner,key=key,
        value=None,source_created_at=time.time(),expires_at=None)))


@pytest.mark.parametrize('polarity',['PREFERS','AVOIDS'])
def test_legacy_restore_creates_v2_only_and_preserves_owner_polarity(polarity):
    g=Graph();old={'key':'legacy_coffee','label':'喜歡喝咖啡'}
    g.add(old,original_relation=polarity);g.add(old,owner='other',original_relation='AVOIDS')
    peer=deepcopy(g.edges[1]);assert invoke(g,old['key'])['status']=='success'
    active=[e for e in g.edges if e['owner']=='owner' and e['relation'] in {'PREFERS','AVOIDS'}]
    assert len(active)==1 and active[0]['relation']==polarity
    concept=g.concepts[active[0]['key']]
    assert stored_concept_identity(concept) and concept['semantic_text']=='喝咖啡'
    assert g.concepts[old['key']]==old and peer in g.edges
    assert g.edges[0]['relation']=='MEMORY_DISABLED' and g.edges[0]['props']['restored_v2_key']==concept['key']
    assert ('preference_embedding_state' in concept)==(polarity=='PREFERS')
    assert g.pending['owner']==[old['key']] and g.revisions['owner']==1
    before=g.snapshot();assert invoke(g,old['key'])['error_code']=='legacy_restore_already_retired';assert g.snapshot()==before


@pytest.mark.parametrize('polarity',['PREFERS','AVOIDS'])
def test_v2_restore_reuses_exact_identity_and_vectors(polarity):
    g=Graph();props={**canonicalize_concept('Reading').as_dict(),'embedding_v2':[1.0]+[0.0]*767,'embedding':[8.0]}
    g.add(props,original_relation=polarity)
    assert invoke(g,props['key'])['status']=='success'
    assert len(g.concepts)==1 and g.edges[0]['relation']==polarity
    assert all(g.concepts[props['key']][k]==v for k,v in props.items())


@pytest.mark.parametrize('props',[
    {'key':'legacy','label':'x'*501}, {'key':'legacy','label':None},
    {'key':'legacy','label':'喜歡Jazz、Rock'},
    {'key':'legacy','label':'Coffee','canonicalization_version':'v2'},
    {'key':'v2_'+'0'*48,'label':'Coffee'},
    {'key':'legacy','label':'喜歡Jazz但討厭Rock'},
])
def test_failed_legacy_normalization_or_corrupt_identity_has_no_write(props):
    g=Graph();g.add(props,original_relation='PREFERS');before=g.snapshot()
    assert invoke(g,props['key'])['status']=='error'
    assert g.snapshot()==before


def test_lossless_compound_is_one_identity_not_split():
    g=Graph();g.add({'key':'legacy','label':'Jazz、Rock'},original_relation='PREFERS')
    assert invoke(g,'legacy')['status']=='success'
    assert [c['semantic_text'] for c in g.concepts.values() if stored_concept_identity(c)]==['Jazz、Rock']


def test_wrong_owner_and_opposite_target_fail_closed():
    g=Graph();g.add({'key':'legacy','label':'Reading'},original_relation='PREFERS')
    before=g.snapshot();assert invoke(g,'legacy','other')['status']=='not_found';assert g.snapshot()==before
    g.add(canonicalize_concept('Reading').as_dict(),'AVOIDS');before=g.snapshot()
    assert invoke(g,'legacy')['error_code']=='preference_restore_conflict';assert g.snapshot()==before


def test_target_creation_failure_rolls_back_all_graph_state():
    g=Graph();g.add({'key':'legacy','label':'Reading'},original_relation='PREFERS');g.fail=True;before=g.snapshot()
    assert invoke(g,'legacy')['status']=='error';assert g.snapshot()==before


def test_restore_respects_existing_projection_capacity():
    g=Graph();g.add({'key':'legacy','label':'Reading'},original_relation='PREFERS')
    g.pending['owner']=[str(i) for i in range(refs.MAX_PENDING)];before=g.snapshot()
    assert invoke(g,'legacy')['error_code']=='preference_projection_pending'
    assert g.snapshot()==before


def test_concurrent_legacy_restore_only_one_success():
    g=Graph();g.add({'key':'legacy','label':'Reading'},original_relation='PREFERS')
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:invoke(g,'legacy'),range(2)))
    assert sum(r['status']=='success' for r in results)==1
    assert g.revisions['owner']==1


@pytest.mark.parametrize('expiry,expected',[(1,'expired'),(10**12,'success')])
def test_non_preference_context_restore_not_migrated(expiry,expected):
    g=Graph();props={'key':'context','label':'Temporary destination'}
    g.add(props,original_relation='CURRENTLY_WANTS',original_expires_at=expiry)
    assert invoke(g,'context')['status']==expected
    assert g.concepts=={'context':props}
    assert all(e['relation']=='CURRENTLY_WANTS' for e in g.edges)
