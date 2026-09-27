"""Owner HTTP conflict mapping and zero Mongo work on rejected actions."""
from copy import deepcopy
from unittest.mock import Mock
import mongomock
import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import system, preference_bootstrap as auth
from services import memory_service as memory, preference_action_projection as projection
from services import semantic_user_eligibility as eligibility


@pytest.fixture
def boundary(monkeypatch):
    db = mongomock.MongoClient().db
    db.profiles.insert_one({'user_id':'owner'})
    monkeypatch.setattr(memory, 'profiles_coll', db.profiles)
    monkeypatch.setattr(auth, 'authenticate_owner', lambda _header:'owner')
    monkeypatch.setattr(eligibility, 'lookup_enabled_account', lambda *_a,**_k:True)
    stage, settle = Mock(), Mock(return_value={'status':'synced'})
    invalidate, sync = Mock(), Mock()
    monkeypatch.setattr(projection, 'stage', stage)
    monkeypatch.setattr(projection, 'settle', settle)
    monkeypatch.setattr(memory, '_invalidate_memory_projection', invalidate)
    monkeypatch.setattr(memory, '_sync_memory_projection', sync)
    app = FastAPI(); app.include_router(system.router)
    return TestClient(app), db, [stage,settle,invalidate,sync]


@pytest.mark.parametrize('action', ['correct','disable'])
def test_typed_stale_is_409_and_no_projection_or_cache_side_effect(boundary, monkeypatch, action):
    client, db, effects = boundary
    before = deepcopy(list(db.profiles.find()))
    response = Mock(); response.json.return_value={'status':'error','error_code':'stale_source','retryable':False}
    monkeypatch.setattr(memory.requests, 'post', Mock(return_value=response))
    result = client.post('/api/profile/memories/action', json={
        'user_id':'owner','key':'par1.'+'A'*43,'action':action,'value':'Tea'})
    assert result.status_code == 409 and result.json()['detail']['code'] == 'stale_source'
    for effect in effects: effect.assert_not_called()
    assert list(db.profiles.find()) == before
    assert set(db.list_collection_names()) == {'profiles'}


def test_forged_user_never_reaches_graph_or_projection(boundary, monkeypatch):
    client, _db, effects = boundary
    post = Mock(); monkeypatch.setattr(memory.requests, 'post', post)
    assert client.post('/api/profile/memories/action', json={
        'user_id':'other','key':'par1.'+'A'*43,'action':'disable'}).status_code == 403
    post.assert_not_called()
    for effect in effects: effect.assert_not_called()


def test_success_stages_only_after_graph_ack_and_never_returns_canonical_keys(boundary, monkeypatch):
    client, _db, effects = boundary
    def post(*_a, **_k):
        effects[0].assert_not_called()
        response = Mock(); response.json.return_value={
            'status':'success','key':'internal-new','projection_key':'internal-old'}
        return response
    monkeypatch.setattr(memory.requests, 'post', post)
    result=client.post('/api/profile/memories/action',json={
        'user_id':'owner','key':'par1.'+'A'*43,'action':'correct','value':'Tea'})
    assert result.status_code == 200
    assert result.json() == {'status':'success','projection_status':'synced'}
    effects[0].assert_called_once_with('owner','internal-old')


def test_unknown_graph_outcome_does_not_pre_stage_or_replay(boundary, monkeypatch):
    client, _db, effects = boundary
    post=Mock(side_effect=requests.Timeout('synthetic lost ack'))
    monkeypatch.setattr(memory.requests,'post',post)
    result=client.post('/api/profile/memories/action',json={
        'user_id':'owner','key':'par1.'+'A'*43,'action':'disable'})
    assert result.status_code == 503 and post.call_count == 1
    for effect in effects: effect.assert_not_called()


def test_committed_graph_marker_recovery_never_replays_action(monkeypatch):
    db=mongomock.MongoClient().db
    monkeypatch.setattr(projection,'JOBS',db.jobs)
    calls=[]
    monkeypatch.setattr(projection,'graph_call',lambda name,body:
        calls.append((name,body)) or {'items':[{'owner':'owner','key':'internal-key'}]})
    stage=Mock(return_value='recovery'); settle=Mock()
    monkeypatch.setattr(projection,'stage',stage);monkeypatch.setattr(projection,'settle',settle)
    projection.recover_committed()
    assert calls == [('action-projection-pending',{'limit':16})]
    stage.assert_called_once_with('owner','internal-key');settle.assert_called_once_with('recovery')
