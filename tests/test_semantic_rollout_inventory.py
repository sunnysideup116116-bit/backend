import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from matchmaker_agent.concept_identity import canonicalize_concept
from matchmaker_agent.related_interest_contract import embedding_fingerprint
from matchmaker_agent.semantic_rollout_inventory import classify_owner, summarize_readiness
from scripts.report_semantic_rollout import enabled_accounts

FP = embedding_fingerprint('models/gemini-embedding-2')


def classify(rows, vectors=None, code=None):
    return classify_owner({'$id': 'synthetic', 'status': True}, [{'_id': 'profile', 'user_id': 'synthetic'}],
        [{'id': 'synthetic'}], {'owner': 'synthetic', 'rows': rows}, vectors or {}, FP,
        preview_check=lambda _: code)


def test_empty_is_eligible_requester_but_never_candidate():
    row = classify([])
    assert row['category'] == 'EMPTY' and row['requester_eligible'] and not row['semantic_ready_candidate']


def test_v2_clean_ready_requires_valid_vectors_and_positive_owner_edge():
    identity = canonicalize_concept('Playing Football').as_dict()
    vector = {**identity, 'vector': [1.]+[0.]*767, 'fingerprint': FP,
        'source_hash': identity['semantic_input_hash'], 'provenance': 'a'*64}
    positive = {'relation': 'PREFERS', 'concept': identity, 'properties': {}}
    ready = classify([positive], {identity['key']: [vector]})
    assert ready['category'] == 'READY' and ready['v2_clean'] and ready['semantic_ready_candidate']
    negative = classify([{**positive, 'relation': 'AVOIDS'}], {identity['key']: [vector]})
    assert negative['category'] == 'READY' and not negative['semantic_ready_candidate']
    assert classify([positive])['reason_codes'] == ['embedding_v2_missing_or_incompatible']
    assert classify([positive, positive], {identity['key']: [vector]})['category'] == 'BLOCKED'


def test_legacy_preview_is_not_confirmation_or_authority_to_write():
    legacy = {'relation': 'PREFERS', 'concept': {'key': 'legacy', 'label': 'Compound original'}, 'properties': {}}
    possible = classify([legacy])
    assert possible['category'] == 'MIGRATABLE' and possible['requires_owner_confirmation']
    assert possible['requester_eligible'] and not possible['semantic_ready_candidate']
    overcap = classify([legacy], code='preference_set_limit')
    assert overcap['category'] == 'BLOCKED' and overcap['requester_eligible']
    assert classify([legacy], code='polarity_ambiguous')['category'] == 'BLOCKED'
    summary = summarize_readiness([possible, overcap, classify([])])
    assert summary['enabled_semantic_requesters'] == 3 and summary['needs_owner_confirmation'] == 2


def http_pages(pages):
    http = Mock(); responses = []
    for page in pages:
        response = Mock(status_code=200); response.json.return_value = page; responses.append(response)
    http.get.side_effect = responses
    return http


def test_account_pagination_has_no_population_ten_cap_and_no_secret_report():
    accounts = [{'$id': f'synthetic-{i}', 'status': True} for i in range(123)]
    http = http_pages([{'total': 123, 'users': accounts[:100]}, {'total': 123, 'users': accounts[100:]}])
    settings = SimpleNamespace(endpoint='https://identity.invalid/v1', project_id='synthetic', verify_tls=True)
    assert enabled_accounts(settings, 'not-a-real-secret', max_accounts=1000, http=http) == accounts
    assert json.loads(http.get.call_args.kwargs['params']['queries[]'][1])['values'] == [100]


@pytest.mark.parametrize('pages,max_count', [
    ([{'total': 2, 'users': []}], 100),
    ([{'total': 2, 'users': [{'$id': 'one', 'status': True}]}, {'total': 1, 'users': []}], 100),
    ([{'total': 2, 'users': [{'$id': 'one', 'status': True}]*2}], 100),
    ([{'total': 1, 'users': [{'$id': 'one', 'status': 'true'}]}], 100),
    ([{'total': 101, 'users': []}], 100),
])
def test_partial_changed_ambiguous_or_truncated_account_listing_is_not_success(pages, max_count):
    with pytest.raises(ValueError):
        enabled_accounts(SimpleNamespace(endpoint='http://identity.invalid', project_id='test', verify_tls=True),
            'synthetic', max_accounts=max_count, http=http_pages(pages))
