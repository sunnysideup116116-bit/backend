from unittest.mock import Mock

import pytest
from services import memory_service as memory
from services import preference_action_projection as projection


@pytest.fixture
def effects(monkeypatch):
    monkeypatch.setattr(memory,'unique_profile',lambda *_a: {'user_id':'owner'})
    stage=Mock(return_value='job');settle=Mock(return_value={'status':'synced'})
    invalidate=Mock();sync=Mock()
    monkeypatch.setattr(projection,'stage',stage);monkeypatch.setattr(projection,'settle',settle)
    monkeypatch.setattr(memory,'_invalidate_memory_projection',invalidate)
    monkeypatch.setattr(memory,'_sync_memory_projection',sync)
    return stage,settle,invalidate,sync


def respond(monkeypatch,result):
    response=Mock();response.json.return_value=result
    post=Mock(return_value=response);monkeypatch.setattr(memory.requests,'post',post)
    return post


def test_rejected_legacy_normalization_never_stages_or_changes_mongo(monkeypatch,effects):
    respond(monkeypatch,{'status':'error','error_code':'legacy_restore_text_not_lossless','retryable':False})
    with pytest.raises(memory.MemoryWriteError) as exc:
        memory.apply_memory_action('owner','legacy','restore')
    assert exc.value.error_code=='legacy_restore_text_not_lossless' and not exc.value.retryable
    for effect in effects:effect.assert_not_called()


def test_unknown_restore_outcome_never_stages_or_replays(monkeypatch,effects):
    post=Mock(side_effect=memory.requests.RequestException());monkeypatch.setattr(memory.requests,'post',post)
    with pytest.raises(memory.MemoryWriteError):memory.apply_memory_action('owner','legacy','restore')
    assert post.call_count==1
    for effect in effects:effect.assert_not_called()


def test_success_projects_the_server_bound_old_key_after_graph_success(monkeypatch,effects):
    respond(monkeypatch,{'status':'success','projection_key':'retired-legacy-source'})
    assert memory.apply_memory_action('owner','legacy','restore')=={'status':'success','projection_status':'synced'}
    effects[0].assert_called_once_with('owner','retired-legacy-source')
    effects[1].assert_called_once_with('job')


def test_missing_projection_proof_fails_closed_without_mongo(monkeypatch,effects):
    respond(monkeypatch,{'status':'success'})
    with pytest.raises(memory.MemoryWriteError,match='memory_action_outcome_unknown'):
        memory.apply_memory_action('owner','legacy','restore')
    for effect in effects:effect.assert_not_called()
