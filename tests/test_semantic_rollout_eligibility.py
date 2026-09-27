import json
from types import SimpleNamespace
from unittest.mock import Mock

import mongomock
import pytest

from matchmaker_agent.concept_identity import canonicalize_concept
from matchmaker_agent.related_interest_contract import embedding_fingerprint
from matchmaker_agent.semantic_evidence_readiness import verified_vector, recheck_owner_evidence, pair_preferences_safe
from matchmaker_agent import semantic_rollout_policy as routing
from services import semantic_user_eligibility as eligibility

MODEL = "models/gemini-embedding-2"
FP = embedding_fingerprint(MODEL)
VECTOR = [1.0] + [0.0]*767


@pytest.fixture
def rollout(monkeypatch, tmp_path):
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ROLLOUT_MODE", "enabled_accounts")
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "on")
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_MODE", "active")
    monkeypatch.setenv("MATCH_RELATED_INTEREST_KILL_SWITCH_FILE", str(tmp_path / "kill"))
    monkeypatch.delenv("MATCH_RELATED_INTEREST_CANARY_USER_IDS", raising=False)
    return tmp_path / "kill"


def test_rollout_population_is_not_an_allowlist_or_ten_user_cap(rollout):
    assert routing.legacy_owner_scope() is None
    assert all(routing.requester_route_allowed(f"synthetic-{i}") for i in range(10001))
    assert routing.pair_route_allowed("synthetic-9000", "synthetic-10000")
    assert not routing.pair_route_allowed("synthetic-9000", "synthetic-9000")


@pytest.mark.parametrize("mode", ["off", "unknown", "enabled", "*"])
def test_bad_rollout_mode_and_off_are_closed(rollout, monkeypatch, mode):
    monkeypatch.setenv(routing.ROLLOUT_MODE_ENV, mode)
    assert not routing.requester_route_allowed("owner")


def test_kill_precedes_global_flags_and_old_config_stays_compatible(rollout, monkeypatch):
    rollout.touch()
    assert not routing.requester_route_allowed("owner")
    assert not routing.pair_route_allowed("owner", "person")
    rollout.unlink()
    monkeypatch.delenv(routing.ROLLOUT_MODE_ENV)
    assert not routing.requester_route_allowed("owner")
    monkeypatch.setenv("MATCH_RELATED_INTEREST_CANARY_USER_IDS", json.dumps(["owner", "person"]))
    assert routing.requester_route_allowed("owner")
    assert not routing.requester_route_allowed("outsider")


def checker(profiles=None, users=None, account=None, clock=lambda: 0):
    coll = mongomock.MongoClient().db.profiles
    coll.insert_many(profiles if profiles is not None else [{"user_id": "owner"}])
    session = Mock()
    session.run.return_value = users if users is not None else [{"id": "owner"}]
    account = account or Mock(return_value=True)
    return eligibility.EnabledUserChecks(session, profiles=coll, account_lookup=account, deadline=5, clock=clock), account, session


@pytest.mark.parametrize("profile", [{"user_id": "owner"}, {"user_id": "owner", "profile_memory_preview": []},
    {"user_id": "owner", "profile_memory_preview": [{"key": "truncated_legacy"}]}])
def test_empty_and_legacy_requester_use_metadata_not_preference_readiness(profile):
    gate, account, graph = checker(profiles=[profile])
    assert gate.check("owner") and gate.check("owner")
    account.assert_called_once()
    assert graph.run.call_count == 1
    assert "Concept" not in str(graph.run.call_args.args[0])


@pytest.mark.parametrize("profiles,users,account", [
    ([{"user_id": "owner"}], [{"id": "owner"}], False),
    ([{"user_id": "owner"}, {"user_id": "owner"}], [{"id": "owner"}], True),
    ([{"user_id": "other"}], [{"id": "owner"}], True),
    ([{"user_id": "owner"}], [], True),
    ([{"user_id": "owner"}], [{"id": "owner"}, {"id": "owner"}], True),
    ([{"user_id": "owner", "disabled": True}], [{"id": "owner"}], True),
    ([{"user_id": "owner", "enabled": False}], [{"id": "owner"}], True),
    ([{"user_id": "owner", "blocked": "false"}], [{"id": "owner"}], True),
    ([{"user_id": "owner", "status": "unrecognized"}], [{"id": "owner"}], True),
    ([{"user_id": "owner"}], [{"id": "owner", "is_active": False}], True),
    ([{"user_id": "owner", "preference_bootstrap_pending": "operation"}], [{"id": "owner"}], True),
    ([{"user_id": "owner"}], [{"id": "owner", "preference_projection_pending": "operation"}], True),
    ([{"user_id": "owner"}], [{"id": "owner", "blocked": True}], True),
])
def test_disabled_ambiguous_or_pending_identity_is_fail_closed(profiles, users, account):
    gate, _, _ = checker(profiles, users, Mock(return_value=account))
    assert not gate.check("owner")


def test_no_cross_request_cache_and_no_retry_of_account_failure():
    account = Mock(side_effect=[True, False, RuntimeError("private-secret")])
    first, _, _ = checker(account=account)
    assert first.check("owner")
    second, _, _ = checker(account=account)
    assert not second.check("owner")
    third, _, _ = checker(account=account)
    assert not third.check("owner") and not third.check("owner")
    assert account.call_count == 3


def test_deadline_exhaustion_never_starts_metadata_call():
    gate, account, graph = checker(clock=lambda: 5)
    assert not gate.check("owner")
    account.assert_not_called(); graph.run.assert_not_called()


def vector_record(text="Playing Football"):
    return {**canonicalize_concept(text).as_dict(), "vector": VECTOR.copy(), "fingerprint": FP,
        "source_hash": canonicalize_concept(text).semantic_input_hash, "provenance": "a"*64}


@pytest.mark.parametrize("mutation", [
    {"fingerprint": "b"*64}, {"source_hash": "stale"}, {"provenance": None},
    {"semantic_text": "changed source"}, {"canonicalization_version": "v1"},
    {"vector": [0.0]*768}, {"vector": [2.0]+[0.0]*767},
    {"vector": [float('nan')]+[0.0]*767}, {"vector": [True]+[0.0]*767},
    {"vector": [1.0]*767}, {"embedding": VECTOR, "vector": None},
])
def test_stale_source_invalid_fingerprint_and_bad_vector_are_not_ready(mutation):
    row = vector_record(); assert verified_vector(row, FP)
    row.update(mutation)
    assert verified_vector(row, FP) is None


def packet():
    query = canonicalize_concept("Watching Football")
    candidate = canonicalize_concept("Playing Football")
    return {"kind": "semantic_related", "basis_type": "related_interest", "policy_version": "related_interest_v1",
        "query_preference": query.semantic_text, "candidate_preference": candidate.semantic_text,
        "concept_key": candidate.key, "relation": "role_mismatch", "semantic_score": .93,
        "similarity": .93, "validator_status": "accepted"}, query.key


@pytest.mark.parametrize("rows", [[], [dict(vector_record(), polarity="AVOIDS")],
    [dict(vector_record(), polarity="PREFERS"), dict(vector_record(), polarity="AVOIDS")],
    [dict(vector_record(), polarity="PREFERS"), dict(vector_record(), polarity="PREFERS")]])
def test_legacy_empty_avoids_and_ambiguous_owner_edges_never_become_evidence(rows):
    proof, key = packet(); session = Mock(); session.run.return_value = rows
    assert not recheck_owner_evidence(session, "person", [proof], key, FP)


def test_current_verified_prefers_and_allowed_relation_required():
    proof, key = packet(); session = Mock(); session.run.return_value = [dict(vector_record(), polarity="PREFERS")]
    assert recheck_owner_evidence(session, "person", [proof], key, FP)
    for relation in ["candidate_more_broad", "constraint_conflict", "lexical_ambiguity", "unrelated", "unknown", "ERROR"]:
        session.reset_mock()
        assert not recheck_owner_evidence(session, "person", [{**proof, "relation": relation}], key, FP)
        session.run.assert_not_called()


def test_changed_preference_after_selection_is_rejected():
    proof, key = packet(); session = Mock()
    session.run.return_value = [dict(vector_record(), polarity="PREFERS")]
    assert recheck_owner_evidence(session, "person", [proof], key, FP)
    session.run.return_value = [dict(vector_record(), polarity="PREFERS", source_hash="changed")]
    assert not recheck_owner_evidence(session, "person", [proof], key, FP)


def test_fresh_pair_conflict_and_query_claim_are_not_inferred():
    source = canonicalize_concept("Playing Football").as_dict()
    session = Mock(); session.run.side_effect = [[{**source, "polarity": "AVOIDS"}], [{**source, "polarity": "PREFERS"}]]
    assert not pair_preferences_safe(session, "owner", "person", query_key=source['key'])
    session.run.side_effect = [[], [{**source, "polarity": "PREFERS"}]]
    assert pair_preferences_safe(session, "owner", "person", query_key=source['key'])
    session.run.side_effect = [[], [{**source, "polarity": "PREFERS"}]]
    assert not pair_preferences_safe(session, "owner", "person", query_key=source['key'], requester_prefers_query=True)
    session.run.side_effect = [[{"key": "unknown_legacy", "polarity": "AVOIDS"}], [{**source, "polarity": "PREFERS"}]]
    assert not pair_preferences_safe(session, "owner", "person", query_key=source['key'])


def test_account_http_contract_and_secret_safe_failure(monkeypatch, capsys):
    from agent_quota import service
    monkeypatch.setattr(service, "AppwriteStore", lambda: SimpleNamespace(endpoint="https://account.invalid/v1",
        headers={"X-Appwrite-Key": "synthetic-secret-never-log"}, verify=True))
    response = Mock(status_code=200); response.json.return_value = {"$id": "owner", "status": True}
    http = Mock(); http.get.return_value = response
    assert eligibility.lookup_enabled_account("owner", deadline=5, clock=lambda: 0, http=http)
    assert http.get.call_args.kwargs['allow_redirects'] is False
    assert http.get.call_args.kwargs['timeout'].total == 2
    response.json.return_value = {"$id": "wrong", "status": True}
    with pytest.raises(eligibility.SemanticEligibilityUnavailable, match="^semantic_eligibility_unavailable$"):
        eligibility.lookup_enabled_account("owner", deadline=5, clock=lambda: 0, http=http)
    assert 'synthetic-secret' not in capsys.readouterr().out
