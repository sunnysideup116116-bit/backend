from copy import deepcopy
from unittest.mock import Mock
import pytest

from matchmaker_agent.concept_identity import canonicalize_concept
from matchmaker_agent.related_interest_contract import POLICY, validated_evidence
from services.related_interest_reason_service import related_friend_intro
from services.match_reason_service import reason_for_viewer, FRIEND_COPY_VERSION, V4_REASON_VERSION
from services.related_interest_telemetry import summarize, record_search
from services.match_search_job_service import _bounded_diagnostics
from services import preference_semantic_service as semantic
from routers import match as router


@pytest.fixture(autouse=True)
def canary_runtime_config(monkeypatch):
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_MODE", "active")
    monkeypatch.setenv("MATCH_RELATED_INTEREST_CANARY_USER_IDS", '["owner","candidate"]')


def evidence(q="看足球", c="踢足球", relation="role_mismatch"):
    return {"kind": "semantic_related", "basis_type": "related_interest", "policy_version": POLICY,
        "query_preference": q, "candidate_preference": c, "concept_key": canonicalize_concept(c).key,
        "relation": relation, "semantic_score": .93, "similarity": .93, "validator_status": "accepted"}


def document(*, confirmed=False, packet=None):
    projection = related_friend_intro({}, {"user_id": "owner"}, {"user_id": "candidate"},
        [packet or evidence()], requester_prefers_query=confirmed)
    return {"from_user": "owner", "to_user": "candidate", "status": "draft",
        "reason_version": V4_REASON_VERSION, "reason_copy_version": FRIEND_COPY_VERSION,
        "friend_intro_v4": projection}


@pytest.mark.parametrize("confirmed", [False, True])
def test_ayue_reasons_are_directional_and_query_is_not_an_owner_fact(confirmed):
    doc = document(confirmed=confirmed)
    owner, candidate = reason_for_viewer(doc, "owner"), reason_for_viewer(doc, "candidate")
    assert "看足球" in owner and "踢足球" in owner
    assert "參與方式不同" in owner and "不代表適合一起做同一活動" in owner
    assert "你喜歡「踢足球」" in candidate
    assert ("你喜歡「看足球」" in owner) is confirmed
    assert ("這位朋友喜歡「看足球」" in candidate) is confirmed
    assert "你們都喜歡" not in owner+candidate
    assert reason_for_viewer(doc, "unrelated-viewer") == ""


def test_provider_or_saved_prose_cannot_override_related_evidence():
    doc = document(confirmed=True)
    doc["friend_intro_v4"]["initiator_preview"]["viewer_text"] = "你們都喜歡看足球。"
    assert "你們都喜歡" not in reason_for_viewer(doc, "owner")


@pytest.mark.parametrize("confirmed", [False, True])
def test_shared_chat_opening_keeps_different_interests_and_query_provenance(monkeypatch, confirmed):
    from services import pair_opening_service as opening
    provider = Mock(side_effect=AssertionError("related copy must stay fact bound"))
    monkeypatch.setattr(opening, "generate_chat_completion_with_tools", provider)
    doc = document(confirmed=confirmed)
    text, source = opening.compose_pair_opening(doc, "小宇", "小晴")
    assert source == "related_interest_fact_bound"
    assert "小晴喜歡「踢足球」" in text
    assert ("小宇喜歡「看足球」" in text) is confirmed
    assert "參與方式不同" in text and "你們都喜歡" not in text
    provider.assert_not_called()
    doc["friend_intro_v4"]["initiator_preview"]["related_interest_basis"]["candidate_id"] = "wrong"
    text, _ = opening.compose_pair_opening(doc, "小宇", "小晴")
    assert "踢足球" not in text and "看足球" not in text


def test_long_or_private_label_is_not_partially_disclosed():
    for packet in (evidence(c="a"*80+" wheelchair accessible"), evidence(c="write to person@example.com")):
        doc = document(packet=packet)
        text = reason_for_viewer(doc, "owner")
        assert "a"*40 not in text and "@" not in text
        assert "偏好相同" in text


def test_constraint_error_and_forged_key_do_not_become_evidence():
    for bad in ({"relation": "constraint_conflict"}, {"validator_status": "ERROR"}, {"concept_key": "fake"}):
        assert validated_evidence({**evidence(), **bad}) is None


def test_related_does_not_enter_shared_preferences_and_hard_conflict_still_wins(monkeypatch):
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "on")
    packet = evidence(); query = canonicalize_concept(packet["query_preference"])
    context = {"search_intent": "preference", "normalized_topic": query.semantic_text,
        "canonical_preference_key": query.key}
    result = router.candidate_qualification({"user_id": "owner"}, {"user_id": "candidate"},
        target_stances={query.key: {"like"}}, candidate_stances={packet["concept_key"]: {"like"}},
        search_context=context, preference_evidence=[packet])
    assert result["eligible"] and result["semantic_related_preference_matched"]
    assert result["shared_preference_keys"] == [] and not result["requested_preference_matched"]
    result = router.candidate_qualification({"user_id": "owner"}, {"user_id": "candidate"},
        target_stances={packet["concept_key"]: {"avoid"}}, candidate_stances={packet["concept_key"]: {"like"}},
        search_context=context, preference_evidence=[packet])
    assert not result["eligible"] and result["hard_conflict_keys"] == [packet["concept_key"]]


def test_new_transport_uses_dedicated_index_contract_and_preserves_complete_evidence(monkeypatch):
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "on")
    from agent_quota import internal
    monkeypatch.setattr(internal, "signed_headers", lambda: {"X-Agent-Quota": "synthetic-context"})
    packet = evidence(); key = canonicalize_concept(packet["query_preference"]).key
    response = Mock(); response.json.return_value = {"status": "success", "canonical_key": key,
        "candidates": [{"candidate_id": "candidate", "evidence": [packet]}],
        "validator_counts": {"accepted": 1, "role_mismatch": 1, "raw_message": "SECRET"}}
    post = Mock(return_value=response); monkeypatch.setattr(semantic.requests, "post", post)
    result = semantic.retrieve_semantic_preference_candidates("owner", packet["query_preference"], excluded_user_ids=set())
    assert post.call_args.args[0] == semantic.AGENT_RELATED_URL
    assert post.call_args.kwargs["headers"] == {"X-Agent-Quota": "synthetic-context"}
    assert len(post.call_args.kwargs["json"]["embedding_fingerprint"]) == 64
    assert result["evidence_by_candidate"]["candidate"] == [packet]
    assert "SECRET" not in str(result["validator_counts"])
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_QUALIFIED_EXACT_THRESHOLD", "5")
    assert semantic.qualified_exact_trigger_threshold() == 1


def test_telemetry_is_count_only_and_draft_decline_is_not_invitation_decline():
    job = {"related_interest_pilot": {"policy_version": POLICY, "triggered": True,
        "validator_counts": {"accepted": 2, "rejected": 1, "error": 1, "role_mismatch": 2, "raw_message": "SECRET"}}}
    proposal = {"related_interest_pilot": {"policy_version": POLICY}, "status": "accepted",
        "state_history": [{"to": "draft"}, {"to": "pending"}, {"to": "accepted"}]}
    draft = {**proposal, "status": "declined", "state_history": [{"to": "draft"}, {"to": "declined"}]}
    result = summarize([job], [proposal, draft])
    assert result["fallback_trigger_jobs"] == 1 and result["semantic_proposals"] == 2
    assert result["semantic_invitations"] == 1 and result["invitation_outcomes"]["accepted"] == 1
    assert result["invitation_outcomes"]["declined"] == 0 and "SECRET" not in str(result)
    assert "SECRET" not in str(_bounded_diagnostics({"related_interest_validator": job["related_interest_pilot"]["validator_counts"]}))


def test_metric_update_is_owner_bound_set_not_memory_or_increment(monkeypatch):
    import database
    db = Mock(); collection = Mock(); db.__getitem__ = Mock(return_value=collection)
    monkeypatch.setattr(database, "db", db)
    record_search("job", "owner", counts={"accepted": 1, "candidate_ids": ["SECRET"]})
    query, update = collection.update_one.call_args.args
    assert query == {"job_id": "job", "user_id": "owner"}
    assert set(update) == {"$set"} and not collection.update_one.call_args.kwargs["upsert"]
    assert "SECRET" not in str(update)
    db.__getitem__.assert_called_once_with("match_search_jobs")


def test_missing_server_pilot_flag_cannot_start_unvalidated_semantic_search(monkeypatch):
    from .test_preference_match_search import _flow, _preference_context, K_POP
    _profiles, matches = _flow(monkeypatch, candidate={"user_id": "candidate"})
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "off")
    monkeypatch.setattr(router, "preference_semantic_mode", lambda: "active")
    monkeypatch.setattr(router, "_trait_stances", lambda _: {})
    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
        "candidate_ids": [], "canonical_key": K_POP})
    lookup = Mock(side_effect=AssertionError("must not use legacy ANN"))
    monkeypatch.setattr(router, "retrieve_semantic_preference_candidates", lookup)
    result = router.generate_matches_for_user("owner", source="automatic", search_context=_preference_context(),
        report_progress=lambda _: True, can_commit=lambda: True)
    assert result["status"] == "no_suitable_candidate"
    assert result["diagnostics"]["semantic_mode"] == "off"
    lookup.assert_not_called(); matches.insert_one.assert_not_called()


def test_kill_switch_after_ranking_stops_new_proposal(monkeypatch):
    from .test_preference_match_search import _flow, _preference_context, _activate_semantic, _semantic_result, K_POP, KOREAN_POP
    _profiles, matches = _flow(monkeypatch, candidate={"user_id": "semantic"})
    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
        "candidate_ids": [], "canonical_key": K_POP})
    _activate_semantic(monkeypatch, _semantic_result())
    monkeypatch.setattr(router, "_trait_stances", lambda uid: {KOREAN_POP: {"like"}} if uid == "semantic" else {})
    def selected(_payload, **_kwargs):
        monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "off")
        return [{"matched_user_id": "semantic"}]
    monkeypatch.setattr(router, "_request_matchmaker_selection", selected)
    intro = Mock(side_effect=AssertionError("no new model reason call after kill"))
    monkeypatch.setattr(router, "build_friend_intro_v4", intro)
    with pytest.raises(router.MatchSearchPipelineError) as error:
        router.generate_matches_for_user("owner", source="automatic", search_context=_preference_context(),
            report_progress=lambda _: True, can_commit=lambda: True)
    assert error.value.code == "semantic_policy_disabled"
    matches.insert_one.assert_not_called()
    intro.assert_not_called()


def test_operator_kill_file_disables_both_services_without_env_restart(monkeypatch, tmp_path):
    from matchmaker_agent.related_interest_contract import enabled
    path = tmp_path / "disabled"
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "on")
    monkeypatch.setenv("MATCH_RELATED_INTEREST_KILL_SWITCH_FILE", str(path))
    assert enabled() and semantic.related_interest_enabled()
    path.write_text("operator stop")
    assert not enabled() and not semantic.related_interest_enabled() and not router.related_interest_enabled()


def test_killed_versioned_request_does_not_fall_back_to_legacy_endpoint(monkeypatch):
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "off")
    post = Mock(side_effect=AssertionError("no HTTP after kill"))
    monkeypatch.setattr(semantic.requests, "post", post)
    with pytest.raises(semantic.PreferenceSemanticRetrievalError) as error:
        semantic._post_semantic({"embedding_fingerprint": "bound"}, 1)
    assert error.value.code == "semantic_policy_disabled"
    post.assert_not_called()
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_MODE", "active")
    with pytest.raises(semantic.PreferenceSemanticRetrievalError):
        semantic.retrieve_semantic_preference_candidates("owner", "K-pop", excluded_user_ids=set())
    post.assert_not_called()
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_MODE", "off")
    with pytest.raises(semantic.PreferenceSemanticRetrievalError):
        semantic.retrieve_semantic_preference_candidates("owner", "K-pop", excluded_user_ids=set(),
            related_policy_required=True)
    post.assert_not_called()


def test_exact_hit_payload_and_proposal_parity_when_pilot_is_enabled(monkeypatch):
    from .test_preference_match_search import _flow, _preference_context, K_POP
    captured = []
    documents = []
    for mode in ("off", "active"):
        monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "on")
        monkeypatch.setattr(router, "preference_semantic_mode", lambda selected=mode: selected)
        _profiles, matches = _flow(monkeypatch, candidate={"user_id": "candidate", "current_context": "最近在爬山"})
        monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
            "candidate_ids": ["candidate"], "canonical_key": K_POP, "query_provenance": "deterministic_alias"})
        monkeypatch.setattr(router, "_trait_stances", lambda uid: {K_POP: {"like"}} if uid == "candidate" else {})
        semantic_call = Mock(side_effect=AssertionError("exact must not call semantic"))
        monkeypatch.setattr(router, "retrieve_semantic_preference_candidates", semantic_call)
        def select(payload, **_kwargs):
            captured.append(deepcopy(payload)); return [{"matched_user_id": "candidate"}]
        monkeypatch.setattr(router, "_request_matchmaker_selection", select)
        context = _preference_context(); context["query_text"] = "幫我找喜歡 Kpop 的人"
        result = router.generate_matches_for_user("owner", source="automatic", search_context=context,
            report_progress=lambda _: True, can_commit=lambda: True)
        assert result["status"] == "success"
        semantic_call.assert_not_called()
        saved = deepcopy(matches.insert_one.call_args.args[0]); saved.pop("created_at"); saved.pop("state_history")
        documents.append(saved)
    assert captured[0] == captured[1]
    assert documents[0] == documents[1]


def test_query_embedding_handshake_keeps_one_outer_budget_and_one_embedding_call(monkeypatch):
    from agent_quota import internal
    monkeypatch.setattr(internal, "signed_headers", lambda: {})
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "on")
    now, sent = [0.0], []
    monkeypatch.setattr(semantic.time, "monotonic", lambda: now[0])
    p = evidence(); key = canonicalize_concept(p["query_preference"]).key
    def post(_url, **kwargs):
        sent.append(deepcopy(kwargs["json"]))
        if len(sent) == 1:
            now[0] += 15.0
            payload = {"status": "query_embedding_required", "canonical_key": key}
        else:
            payload = {"status": "success", "canonical_key": key, "candidates": [
                {"candidate_id": "candidate", "evidence": [p]}]}
        response = Mock(); response.json.return_value = payload
        return response
    def embed(texts, **kwargs):
        assert texts == [p["query_preference"]]
        assert kwargs["request_timeout_seconds"] == 23.0
        now[0] += 9.0
        return [[1.0]+[0.0]*767]
    embedding = Mock(side_effect=embed)
    monkeypatch.setattr(semantic.requests, "post", post)
    monkeypatch.setattr(semantic, "get_embeddings", embedding)
    result = semantic.retrieve_semantic_preference_candidates("owner", p["query_preference"], excluded_user_ids=set())
    assert result["evidence_by_candidate"]["candidate"] == [p]
    assert [r["request_budget_seconds"] for r in sent] == [26.9, 13.9]
    assert "query_embedding" not in sent[0] and len(sent[1]["query_embedding"]) == 768
    embedding.assert_called_once()


def test_matchmaker_transport_only_adds_remaining_budget_for_related_packets(monkeypatch):
    from agent_quota import internal
    monkeypatch.setattr(internal, "signed_headers", lambda: {})
    response = Mock(); response.json.return_value = {"outcome": "no_suitable_candidate", "matches": []}
    post = Mock(return_value=response); monkeypatch.setattr(router.requests, "post", post)
    exact = {"target_user": {}, "candidates": [{"user_id": "candidate"}]}
    router._request_matchmaker_selection(exact, timeout=7.0)
    assert post.call_args.kwargs["json"] == exact
    related = {"target_user": {}, "candidates": [{"user_id": "candidate", "preference_retrieval_evidence": [evidence()]}]}
    router._request_matchmaker_selection(related, timeout=2.0)
    assert post.call_args.kwargs["json"]["request_budget_seconds"] == 1.9
    assert "request_budget_seconds" not in related
    assert post.call_count == 2
    with pytest.raises(router.MatchSearchPipelineError):
        router._request_matchmaker_selection(related, timeout=.01)
    assert post.call_count == 2


def test_confirmation_and_mutual_consent_preserve_packet_without_memory_writes(monkeypatch):
    from bson import ObjectId
    from services import match_decision_service as decisions, match_action_service as actions
    from services.pair_opening_service import compose_pair_opening
    doc = document(); doc.update(_id=ObjectId(), proposal_revision=0)
    doc["preference_retrieval_evidence"] = [evidence()]
    original = deepcopy(doc["friend_intro_v4"])
    collection = Mock(); collection.find_one.side_effect = lambda *_a, **_k: deepcopy(doc)
    collection.count_documents.return_value = 0
    def update(query, changes, **_kwargs):
        assert query["status"] == doc["status"]
        assert not any(k in changes.get("$set", {}) for k in ("friend_intro_v4", "preference_retrieval_evidence"))
        doc.update(changes["$set"])
        doc["proposal_revision"] += changes["$inc"]["proposal_revision"]
        doc.setdefault("state_history", []).append(changes["$push"]["state_history"])
        return deepcopy(doc)
    collection.find_one_and_update.side_effect = update
    monkeypatch.setattr(decisions, "matches_coll", collection)
    writer = Mock(side_effect=AssertionError("query/consent must never persist a preference"))
    monkeypatch.setattr(actions, "upsert_preference_facts", writer)
    monkeypatch.setattr(actions, "queue_mediator_event", Mock())
    monkeypatch.setattr(actions, "profiles_coll", Mock(find_one=Mock(return_value={})))
    scheduled = []
    monkeypatch.setattr(actions, "schedule_match_celebration_gifs", Mock())
    for owner, before, revision, after in (("owner", "draft", 0, "pending"), ("candidate", "pending", 1, "accepted")):
        result = decisions.apply_match_decision(user_id=owner, match_id=str(doc["_id"]), action="accept",
            expected_status=before, expected_revision=revision, idempotency_key=f"confirmation-{revision}",
            after_transition=lambda *args: actions.apply_transition_effects(*args, schedule_task=scheduled.append))
        assert result["new_status"] == after
        assert doc["friend_intro_v4"] == original and doc["preference_retrieval_evidence"] == [evidence()]
        text = reason_for_viewer(doc, owner)
        assert "看足球" in text and "踢足球" in text and "你們都喜歡" not in text
    opening, kind = compose_pair_opening(doc, "小宇", "小晴")
    assert kind == "related_interest_fact_bound" and "小宇這次想找" in opening
    writer.assert_not_called()


def test_public_card_does_not_serialize_internal_related_packet(monkeypatch):
    import json
    monkeypatch.setattr(router, "public_display_name", lambda *_a, **_k: "朋友")
    doc = document(); doc["_id"] = "proposal"; doc["preference_retrieval_evidence"] = [evidence()]
    card = router.build_active_proposal_card(doc, "owner")
    encoded = json.dumps(card, ensure_ascii=False)
    for field in ("friend_intro_v4", "preference_retrieval_evidence", "related_interest_basis", "semantic_score", "validator_status", "concept_key"):
        assert field not in encoded
    assert "看足球" in card["viewer_reason"] and "踢足球" in card["viewer_reason"]
    assert "你們都喜歡" not in card["viewer_reason"]
