from unittest.mock import Mock
import pytest
from fastapi import HTTPException
import agent_api
import related_interest_retrieval as retrieval
from matchmaker_agent.semantic_rollout_policy import compute_allowed, pair_route_allowed
from test_related_interest import setup_graph, Result, VECTOR, MODEL, embedding_fingerprint, response
import related_interest_validator as validator


@pytest.fixture
def shadow(monkeypatch,tmp_path):
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MODE','shadow')
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ENABLED','off')
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ROLLOUT_MODE','enabled_accounts')
    kill=tmp_path/'kill';monkeypatch.setenv('MATCH_RELATED_INTEREST_KILL_SWITCH_FILE',str(kill))
    monkeypatch.setattr(validator,'_completion',lambda client,**request:client.chat.completions.create(**request))
    return kill


@pytest.mark.parametrize('relation',['role_mismatch','unrelated','ERROR'])
def test_shadow_same_v2_ann_validator_no_proposal_authority(shadow,monkeypatch,relation):
    session,req,q,c,client,events=setup_graph(relation if relation!='ERROR' else 'unrelated')
    req.shadow_only=True;original=session.run
    def run(statement,**params):
        if 'type(r) AS polarity' in str(statement):
            return Result([{**c.as_dict(),'vector':VECTOR,'fingerprint':embedding_fingerprint(MODEL),
                'source_hash':c.semantic_input_hash,'provenance':'a'*64,'polarity':'PREFERS'}])
        if 'type(r) AS polarity' not in str(statement) and 'type(r)' in str(statement):
            return Result([])
        rows=original(statement,**params)
        for row in rows:
            if 'vector' in row:row['provenance']='a'*64
        return rows
    session.run=run
    if relation=='ERROR':client.chat.completions.create.side_effect=TimeoutError()
    checks=Mock(unavailable=False);checks.check.return_value=True
    # Full conflict checker has independent coverage; prove shared deadline and call.
    conflicts=Mock(return_value=True);monkeypatch.setattr(retrieval,'pair_preferences_safe',conflicts)
    result=retrieval.retrieve(session,req,q,client,'deepseek-test',MODEL,clock=lambda:0,
        eligibility_factory=lambda *_a,**_k:checks)
    assert events[0]=='ann' and 'legacy' not in str(result)
    assert compute_allowed('owner',shadow_only=True) and not pair_route_allowed('owner','person')
    if relation=='role_mismatch':
        assert result['candidates'][0]['evidence'][0]['relation']=='role_mismatch'
        conflicts.assert_called_once()
    else:assert result['candidates']==[] and 'expand' not in events
    if relation=='ERROR':assert result['error_code']=='semantic_validator_unavailable'


def test_kill_and_shadow_purpose_cannot_be_bypassed(shadow,monkeypatch):
    s,req,q,c,client,events=setup_graph();req.shadow_only=True
    shadow.touch()
    assert retrieval.retrieve(s,req,q,client,'deepseek-test',MODEL)['error_code']=='semantic_policy_disabled'
    assert not events and not client.chat.completions.create.called
    shadow.unlink()
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MODE','active')
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ENABLED','on')
    assert not compute_allowed('owner',shadow_only=True)
    assert compute_allowed('owner')


def test_shadow_still_requires_signed_owner_quota_context(shadow):
    _,req,*_=setup_graph();req.shadow_only=True
    with pytest.raises(HTTPException) as exc:agent_api.related_interest_candidates(req)
    assert exc.value.status_code==403


@pytest.mark.parametrize('fault',['fingerprint','source_hash','legacy','AVOIDS','disabled_owner','constraint'])
def test_shadow_drops_unready_or_conflicting_candidates(shadow,monkeypatch,fault):
    s,req,q,c,client,events=setup_graph();req.shadow_only=True;original=s.run
    def current(statement,**params):
        if 'type(r) AS polarity' in str(statement):
            return Result([{**c.as_dict(),'vector':VECTOR,'fingerprint':embedding_fingerprint(MODEL),
                'source_hash':c.semantic_input_hash,'provenance':'a'*64,'polarity':'AVOIDS' if fault=='AVOIDS' else 'PREFERS'}])
        rows=original(statement,**params)
        for row in rows:
            if 'vector' not in row:continue
            row['provenance']='a'*64
            if fault=='fingerprint':row['fingerprint']='0'*64
            if fault=='source_hash':row['source_hash']='0'*64
            if fault=='legacy':row['canonicalization_version']='v1'
        return rows
    s.run=current
    checks=Mock(unavailable=False);checks.check.side_effect=lambda uid: not(fault=='disabled_owner' and uid=='person')
    monkeypatch.setattr(retrieval,'pair_preferences_safe',lambda *_a,**_k:fault!='constraint')
    result=retrieval.retrieve(s,req,q,client,'deepseek-test',MODEL,clock=lambda:0,
        eligibility_factory=lambda *_a,**_k:checks)
    assert result['candidates']==[]
    if fault in {'fingerprint','source_hash','legacy'}:client.chat.completions.create.assert_not_called()


def test_kill_during_validator_cannot_expand_or_leak_evidence(shadow,monkeypatch):
    s,req,q,c,client,events=setup_graph();req.shadow_only=True;original=s.run
    def current(statement,**params):
        rows=original(statement,**params)
        for row in rows:
            if 'vector' in row:row['provenance']='a'*64
        return rows
    s.run=current
    def complete(**kwargs):
        shadow.touch();return response(['role_mismatch'])
    client.chat.completions.create.side_effect=complete
    checks=Mock(unavailable=False);checks.check.return_value=True
    result=retrieval.retrieve(s,req,q,client,'deepseek-test',MODEL,clock=lambda:0,
        eligibility_factory=lambda *_a,**_k:checks)
    assert result['error_code']=='semantic_policy_disabled' and result['candidates']==[]
    assert events==['ann']
