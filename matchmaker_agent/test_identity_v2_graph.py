"""Synthetic-only v2 Graph boundaries; no driver/provider is contacted."""
import asyncio
from unittest.mock import MagicMock, patch

import pytest

import agent_api
from concept_identity import canonicalize_concept, canonicalize_concept_v1, PreferenceTextError
from concept_identity import is_v2_preference_key
from registration_graph import seed_registration, registration_message_id


@pytest.mark.parametrize("surface", ["apply", "registration", "correction", "embedding"])
def test_v2_graph_operation_preflights_complete_semantic_batch(surface):
    valid = {**canonicalize_concept("K-pop").as_dict(), "stance": "like", "confidence": .99}
    too_long = {"label": "x" * 501, "stance": "like", "confidence": .99}
    with patch.object(agent_api.GraphDatabase, "driver") as driver:
        if surface == "registration":
            tx = MagicMock()
            with pytest.raises(PreferenceTextError):
                seed_registration(tx, "owner", registration_message_id("owner"), [valid, too_long])
            tx.run.assert_not_called()
            return
        if surface == "apply":
            result = asyncio.run(agent_api.apply_memory(agent_api.MemoryApplyRequest(
                user_id="owner", memories=[valid, too_long])))
        elif surface == "correction":
            result = asyncio.run(agent_api.memory_action(agent_api.MemoryActionRequest(
                user_id="owner", key=valid["key"], action="correct", value="x" * 501)))
        else:
            result = agent_api.project_concept_embeddings(agent_api.ConceptEmbeddingProjectionRequest(
                concepts=[{**valid, "kind": "interest", "embedding": [1.] * 768}, too_long]))
        assert result["status"] == "error"
        driver.assert_not_called()


def test_v2_projection_rejects_old_worker_payload():
    identity = canonicalize_concept("Visiting a historical castle with a quiet guide and no group tours")
    with patch.object(agent_api.GraphDatabase, "driver") as driver:
        result = agent_api.project_concept_embeddings(agent_api.ConceptEmbeddingProjectionRequest(
            concepts=[{"key": identity.key, "label": identity.label[:60],
                       "kind": "interest", "embedding": [1.] * 768}]))
    assert result["error_code"] == "embedding_identity_unverified"
    driver.assert_not_called()


@pytest.mark.parametrize("legacy_proof", [False, True])
def test_v2_graph_exact_proof_boundaries(legacy_proof):
    import hashlib
    identity = canonicalize_concept("K-pop")
    legacy = canonicalize_concept_v1("K-pop")
    driver, session, metadata = MagicMock(), MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    metadata.single.return_value = None
    row = {"candidate_id": "synthetic-candidate", "key": legacy.key,
           "canonicalization_version": "legacy_unknown", "label": "K-pop"}
    if legacy_proof:
        row.update(legacy_evidence_scope="owner_assertion", legacy_fidelity_status="complete",
                   legacy_semantic_text="K-pop",
                   legacy_semantic_input_hash=hashlib.sha256(b"K-pop").hexdigest())
    session.run.side_effect = [metadata, [row]]
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
        result = agent_api.preference_candidates(agent_api.PreferenceCandidateRequest(
            requester_user_id="owner", topic="Kpop", limit=20))
    assert result["canonical_key"] == identity.key
    assert result["candidate_ids"] == (["synthetic-candidate"] if legacy_proof else [])
    assert all("Concept {key:" in call.args[0] for call in session.run.call_args_list)
    assert session.run.call_args.kwargs["limit"] == 20


def test_v2_writer_preserves_long_qualifiers_and_never_updates_existing_identity():
    prefix = "Visiting historic city landmarks every weekend with "
    texts = [prefix + "wheelchair accessible step-free paths", prefix + "steep stairs"]
    driver, session, tx = MagicMock(), MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    session.execute_write.side_effect = lambda callback: callback(tx)
    req = agent_api.MemoryApplyRequest(user_id="owner", memories=[
        {**canonicalize_concept(text).as_dict(), "confidence": .99, "stance": "like"}
        for text in texts])
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
        result = asyncio.run(agent_api.apply_memory(req))
    assert result["status"] == "success"
    assert [item["semantic_text"] for item in result["memories"]] == texts
    assert len({item["key"] for item in result["memories"]}) == 2
    call = next(call for call in tx.run.call_args_list if "UNWIND $memories" in call.args[0])
    assert "ON CREATE SET c.label=item.semantic_text" in call.args[0]
    assert "ON MATCH SET" not in call.args[0]
    assert "c.canonicalization_version=item.canonicalization_version" in call.args[0]
    assert all(item["canonicalization_version"] == "v2" for item in call.kwargs["memories"])


def test_v2_projection_only_updates_vectors_after_exact_source_guard():
    identity = canonicalize_concept("Visiting historical castles quietly with no guided tours and wheelchair access")
    driver, session = MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    def run(query, **kwargs):
        if "UNWIND $keys" in query:
            return [identity.as_dict()]
        result = MagicMock()
        result.single.return_value = {"written": 1, "count": 0}
        return result
    session.run.side_effect = run
    session.execute_write.side_effect = lambda callback, *args: callback(session, *args)
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver), \
            patch.object(agent_api, "_refresh_semantic_event_links", return_value={}):
        result = agent_api.project_concept_embeddings(agent_api.ConceptEmbeddingProjectionRequest(
            concepts=[{**identity.as_dict(), "kind": "interest", "embedding": [1.] * 768,
                       "embedding_model": "synthetic", "embedding_task": "semantic_similarity"}]))
    assert result["embedded_count"] == 1
    write = next(call for call in session.run.call_args_list if "UNWIND $concepts" in call.args[0])
    assert write.kwargs["concepts"][0]["label"] == identity.semantic_text
    assert "concept.semantic_input_hash=item.semantic_input_hash" in write.args[0]
    assert "concept.semantic_text=item.label" in write.args[0]
    assert "concept.label=item.label" in write.args[0]  # WHERE guard only, never SET.
    assert "SET concept.label" not in write.args[0]
    assert "SET concept.semantic_text" not in write.args[0]


def test_legacy_correction_requires_reconfirmation_without_graph_access():
    with patch.object(agent_api.GraphDatabase, "driver") as driver:
        result = asyncio.run(agent_api.memory_action(agent_api.MemoryActionRequest(
            user_id="owner", key="coffee", action="correct", value="Quiet Coffee Shops")))
    assert result["error_code"] == "legacy_identity_unverified"
    assert result["reconfirmation_required"] is True
    driver.assert_not_called()


def test_legacy_prefix_collision_is_not_an_exact_match():
    prefix = "Visiting historic city landmarks every weekend with "
    query, other = prefix + "step-free access", prefix + "steep stairs"
    assert canonicalize_concept_v1(query).key == canonicalize_concept_v1(other).key
    candidate = canonicalize_concept(other)
    metadata, driver, session = MagicMock(), MagicMock(), MagicMock()
    metadata.single.return_value = None
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    session.run.side_effect = [metadata, [{
        "candidate_id": "synthetic", "key": canonicalize_concept_v1(other).key,
        "canonicalization_version": "legacy_unknown", "legacy_evidence_scope": "owner_assertion",
        "legacy_fidelity_status": "complete", "legacy_semantic_text": other,
        "legacy_semantic_input_hash": candidate.semantic_input_hash,
    }]]
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
        result = agent_api.preference_candidates(agent_api.PreferenceCandidateRequest(
            requester_user_id="owner", topic=query))
    assert result["candidate_ids"] == []


def test_recent_context_projection_cannot_overwrite_v2_identity():
    import time
    driver, session = MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    session.execute_write.side_effect = lambda callback: callback(session)
    def run(query, **_kwargs):
        row = MagicMock()
        row.single.return_value = {"revision": 0, "epoch_at": 0} if "AS revision" in query else None
        return row
    session.run.side_effect = run
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
        result = asyncio.run(agent_api.project_current_context(agent_api.ContextProjectionRequest(
            user_id="owner", concepts=[{"label": "Hiking", "key": canonicalize_concept("Hiking").key}],
            expires_at=time.time() + 1000)))
    assert result["status"] == "success"
    write = next(call for call in session.run.call_args_list if "SET c.label=$label" in call.args[0])
    assert "WHERE coalesce(c.canonicalization_version, '') <> 'v2'" in write.args[0]


def test_versioned_write_routes_exist_and_unversioned_memory_fails_closed():
    paths = {route.path for route in agent_api.app.routes}
    assert {"/api/v2/memory/apply", "/api/v2/memory/action", "/api/v2/concepts/embeddings/project"} <= paths
    with patch.object(agent_api.GraphDatabase, "driver") as driver:
        result = asyncio.run(agent_api.apply_memory(agent_api.MemoryApplyRequest(
            user_id="owner", memories=[{"label": "K-pop", "key": "k_pop", "stance": "like", "confidence": .99}])))
    assert result["error_code"] == "preference_identity_unverified"
    assert result["retryable"] is False
    driver.assert_not_called()


@pytest.mark.parametrize("invalid", [{"embedding": [1.]}, {"embedding": [float("nan")] * 768}, {"kind": "bad"}])
def test_invalid_embedding_item_rejects_whole_batch_without_graph(invalid):
    valid = {**canonicalize_concept("K-pop").as_dict(), "kind": "interest", "embedding": [1.] * 768}
    with patch.object(agent_api.GraphDatabase, "driver") as driver:
        result = agent_api.project_concept_embeddings(agent_api.ConceptEmbeddingProjectionRequest(
            concepts=[valid, {**valid, **invalid}]))
    assert result["status"] == "error"
    driver.assert_not_called()


def test_owner_memory_query_overflow_is_not_a_prefix_lookup():
    with patch.object(agent_api.GraphDatabase, "driver") as driver:
        result = asyncio.run(agent_api.list_memories("owner", query="x" * 121))
    assert result["status"] == "invalid_query"
    driver.assert_not_called()


def test_feedback_overlong_reason_rejected_before_provider():
    from fastapi import HTTPException
    with patch.object(agent_api.agent, "generate_graph_reflection") as provider, \
            patch.object(agent_api.GraphDatabase, "driver") as driver:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(agent_api.receive_feedback(agent_api.FeedbackRequest(
                user_id="owner", target_id="other", action="decline", target_traits={},
                explicit_reasons=["x" * 501])))
    assert caught.value.status_code == 422
    provider.assert_not_called()
    driver.assert_not_called()


def test_correction_of_same_v2_identity_is_read_only():
    identity = canonicalize_concept("Kpop")
    driver, session, tx = MagicMock(), MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    session.execute_write.side_effect = lambda callback: callback(tx)
    tx.run.return_value.single.return_value = identity.as_dict()
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
        result = asyncio.run(agent_api.memory_action(agent_api.MemoryActionRequest(
            user_id="owner", key=identity.key, action="correct", value="K-pop")))
    assert result["status"] == "success"
    assert tx.run.call_count == 2  # serialization guard + read; no memory mutation/bump
    assert "AS revision" in tx.run.call_args_list[0].args[0]
    assert "DELETE" not in tx.run.call_args.args[0]


@pytest.mark.parametrize("surface", ["apply", "registration"])
def test_atomic_expansion_cap_rejects_all_preferences(surface):
    texts = ["Tea, Coffee, Cocoa", "Hiking, Cycling, Running", "Origami, Pottery, Painting"]
    memories = [{**canonicalize_concept(text).as_dict(), "stance": "like", "confidence": .99,
                 "category": "activity", "evidence_span": text} for text in texts]
    with patch.object(agent_api.GraphDatabase, "driver") as driver:
        if surface == "registration":
            tx = MagicMock()
            with pytest.raises(ValueError, match="memory_limit_exceeded"):
                seed_registration(tx, "owner", registration_message_id("owner"), memories)
            tx.run.assert_not_called()
        else:
            result = asyncio.run(agent_api.apply_memory(agent_api.MemoryApplyRequest(user_id="owner", memories=memories)))
            assert result["error_code"] == "memory_limit_exceeded"
        driver.assert_not_called()


def test_embedding_db_mismatch_never_creates_index_or_writes_any_vector():
    first, second = canonicalize_concept("K-pop"), canonicalize_concept("Reading")
    driver, session = MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    session.execute_write.side_effect = lambda callback, *args: callback(session, *args)
    session.run.return_value = [first.as_dict(), {**second.as_dict(), "semantic_input_hash": "invalid"}]
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver), \
            patch.object(agent_api, "_refresh_semantic_event_links") as refresh:
        result = agent_api.project_concept_embeddings(agent_api.ConceptEmbeddingProjectionRequest(concepts=[
            {**identity.as_dict(), "kind": "interest", "embedding": [1.] * 768}
            for identity in (first, second)]))
    assert result["error_code"] == "embedding_identity_mismatch"
    assert session.run.call_count == 1
    assert "MATCH (concept:Concept {key:key})" in session.run.call_args.args[0]
    assert "SET " not in session.run.call_args.args[0]
    refresh.assert_not_called()


def test_embedding_write_count_mismatch_raises_for_transaction_rollback():
    identity, tx = canonicalize_concept("K-pop"), MagicMock()
    partial = MagicMock()
    partial.single.return_value = {"written": 0}
    tx.run.side_effect = [[identity.as_dict()], partial]
    payload = {**identity.as_dict(), "label": identity.semantic_text, "kind": "interest",
               "embedding": [1.] * 768, "embedding_model": "test", "embedding_task": "semantic_similarity"}
    with pytest.raises(ValueError, match="embedding_identity_mismatch"):
        agent_api._write_concept_embedding_batch(tx, [payload], 1.0)


@pytest.mark.parametrize("model_label", ["Writing Novels", "Reading", "Whisky Tasting"])
def test_feedback_never_promotes_model_only_or_weakened_concepts(model_label):
    import json
    from fastapi import HTTPException
    from unittest.mock import AsyncMock
    response = json.dumps({"relationships": [
        {"relation_type": "DISLIKES_TRAIT", "trait": "Reading Mystery Novels"},
        {"relation_type": "DISLIKES_TRAIT", "trait": model_label}]})
    with patch.object(agent_api.agent, "generate_graph_reflection", return_value=response), \
            patch.object(agent_api, "apply_memory", new_callable=AsyncMock) as write:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(agent_api.receive_feedback(agent_api.FeedbackRequest(
                user_id="owner", target_id="other", action="decline", target_traits={},
                explicit_reasons=["Reading Mystery Novels"])))
    assert caught.value.detail["code"] == "feedback_ungrounded_concept"
    write.assert_not_called()


def test_feedback_deterministic_alias_and_closed_enumeration_remain_grounded():
    import json
    from unittest.mock import AsyncMock
    response = json.dumps({"relationships": [
        {"relation_type": "DISLIKES_TRAIT", "trait": label}
        for label in ("K-pop", "Tea", "Coffee")]})
    with patch.object(agent_api.agent, "generate_graph_reflection", return_value=response), \
            patch.object(agent_api, "apply_memory", new_callable=AsyncMock,
                         return_value={"status": "success", "memories": []}) as write:
        asyncio.run(agent_api.receive_feedback(agent_api.FeedbackRequest(
            user_id="owner", target_id="other", action="decline", target_traits={},
            explicit_reasons=["Kpop", "Tea, Coffee"])))
    assert [item["semantic_text"] for item in write.call_args.args[0].memories] == ["K-pop", "Tea", "Coffee"]


def test_reserved_v2_key_format_does_not_capture_legacy_slug():
    assert is_v2_preference_key(canonicalize_concept("Rocket Modeling").key)
    assert canonicalize_concept_v1("V2 Rocket Modeling").key == "v2_rocket_modeling"
    assert not is_v2_preference_key("v2_rocket_modeling")
    assert not is_v2_preference_key("v2_" + "a" * 47)
    assert not is_v2_preference_key("v2_" + "z" * 48)
    assert not is_v2_preference_key(None)


def test_legacy_v2_prefix_slug_embedding_projection_is_supported():
    legacy = canonicalize_concept_v1("V2 Rocket Modeling")
    driver, session = MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    session.execute_write.side_effect = lambda callback, *args: callback(session, *args)
    def run(query, **kwargs):
        if "UNWIND $keys" in query:
            return [{"key": legacy.key, "label": legacy.label}]
        result = MagicMock()
        result.single.return_value = {"written": 1, "count": 0}
        return result
    session.run.side_effect = run
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver), \
            patch.object(agent_api, "_refresh_semantic_event_links", return_value={}):
        result = agent_api.project_concept_embeddings(agent_api.ConceptEmbeddingProjectionRequest(
            concepts=[{"key": legacy.key, "label": legacy.label, "kind": "interest", "embedding": [1.] * 768}]))
    assert result["status"] == "success"
    assert result["embedded_count"] == 1


@pytest.mark.parametrize("existing_poison", [False, True])
def test_context_projection_never_creates_reserved_v2_identity(existing_poison):
    import time
    key = canonicalize_concept("Hiking").key
    driver, session = MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    session.execute_write.side_effect = lambda callback: callback(session)
    def run(query, **_kwargs):
        row = MagicMock()
        row.single.return_value = {"revision": 0, "epoch_at": 0} if "AS revision" in query else ({"key": key} if existing_poison else None)
        return row
    session.run.side_effect = run
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
        asyncio.run(agent_api.project_current_context(agent_api.ContextProjectionRequest(
            user_id="owner", concepts=[{"label": "Hiking", "key": key}], expires_at=time.time() + 1000)))
    writes = [call for call in session.run.call_args_list if "SET c.label=$label" in call.args[0]]
    assert len(writes) == 1
    assert writes[0].kwargs["key"] == agent_api._concept_key("Hiking")
    assert not is_v2_preference_key(writes[0].kwargs["key"])


@pytest.mark.parametrize("surface", ["apply", "registration", "correction", "feedback"])
def test_explicit_nine_atom_operations_fail_with_typed_error(surface):
    from fastapi import HTTPException
    text = ", ".join(f"Concept{index}" for index in range(9))
    with patch.object(agent_api.GraphDatabase, "driver") as driver, \
            patch.object(agent_api.agent, "generate_graph_reflection") as provider:
        if surface in {"apply", "registration"}:
            item = {**canonicalize_concept(text).as_dict(), "confidence": .99, "stance": "like",
                    "category": "activity", "evidence_span": text}
            if surface == "registration":
                tx = MagicMock()
                with pytest.raises(PreferenceTextError, match="memory_limit_exceeded"):
                    seed_registration(tx, "owner", registration_message_id("owner"), [item])
                tx.run.assert_not_called()
            else:
                result = asyncio.run(agent_api.apply_memory(agent_api.MemoryApplyRequest(user_id="owner", memories=[item])))
                assert result["error_code"] == "memory_limit_exceeded"
                assert result["retryable"] is False
        elif surface == "correction":
            result = asyncio.run(agent_api.memory_action(agent_api.MemoryActionRequest(
                user_id="owner", key=canonicalize_concept("Hiking").key, action="correct", value=text)))
            assert result["error_code"] == "memory_limit_exceeded"
            assert result["retryable"] is False
        else:
            with pytest.raises(HTTPException) as caught:
                asyncio.run(agent_api.receive_feedback(agent_api.FeedbackRequest(
                    user_id="owner", target_id="other", action="decline", target_traits={}, explicit_reasons=[text])))
            assert caught.value.status_code == 422
        provider.assert_not_called()
        driver.assert_not_called()


def _boundary_preference_text(size):
    prefix, suffix = "Reading long-form stories ", " only while seated"
    return prefix + "x" * (size - len(prefix) - len(suffix)) + suffix


@pytest.mark.parametrize("size", [499, 500])
@pytest.mark.parametrize("surface", ["apply", "registration_component", "correction", "embedding", "exact"])
def test_graph_semantic_boundaries_preserve_complete_499_500(size, surface):
    # This tests the Graph component contract, NOT registration's 120-character
    # initial_interest ingress. Registration evidence keeps its separate bound.
    text = _boundary_preference_text(size)
    identity = canonicalize_concept(text)
    assert len(text) == size
    driver, session, tx = MagicMock(), MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    session.execute_write.side_effect = lambda callback, *args: callback(tx, *args)
    original = canonicalize_concept("Reading Short Stories")

    def run(query, **params):
        if "UNWIND $keys" in query:
            return [identity.as_dict()] if surface == "embedding" else []
        if "RETURN concept.key AS key,concept.semantic_text" in query:
            result = MagicMock()
            result.single.return_value = identity.as_dict()
            return result
        if "RETURN candidate.id AS candidate_id" in query:
            return [{"candidate_id": "synthetic-candidate"}]
        if "legacy_evidence_scope" in query:
            return []
        result = MagicMock()
        result.single.return_value = (
            original.as_dict() if "RETURN old.key AS key" in query
            else {"seed_allowed": True, "written": 1, "count": 0, "relation": "PREFERS"}
        )
        return result

    session.run.side_effect = run
    tx.run.side_effect = run
    item = {**identity.as_dict(), "stance": "like", "confidence": .99,
            "category": "activity", "evidence_span": "Reading long-form stories"}
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver), \
            patch.object(agent_api, "_refresh_semantic_event_links", return_value={}):
        if surface == "apply":
            result = asyncio.run(agent_api.apply_memory(agent_api.MemoryApplyRequest(user_id="owner", memories=[item])))
            assert result["memories"][0]["semantic_text"] == text
            write = next(call for call in tx.run.call_args_list if "UNWIND $memories" in call.args[0])
            assert write.kwargs["memories"][0]["semantic_text"] == text
        elif surface == "registration_component":
            result = seed_registration(tx, "owner", registration_message_id("owner"), [item])
            assert result[0]["semantic_text"] == text
            write = next(call for call in tx.run.call_args_list if "UNWIND $memories" in call.args[0])
            assert write.kwargs["memories"][0]["semantic_text"] == text
            return
        elif surface == "correction":
            result = asyncio.run(agent_api.memory_action(agent_api.MemoryActionRequest(
                user_id="owner", key=original.key, action="correct", value=text)))
            write = next(call for call in tx.run.call_args_list if "MERGE (corrected:Concept" in call.args[0])
            assert write.kwargs["label"] == text
            assert write.kwargs["semantic_input_hash"] == identity.semantic_input_hash
            assert write.kwargs["corrected_key"] == identity.key
        elif surface == "embedding":
            result = agent_api.project_concept_embeddings(agent_api.ConceptEmbeddingProjectionRequest(concepts=[{
                **identity.as_dict(), "kind": "interest", "embedding": [1.] * 768,
                "embedding_model": "synthetic", "embedding_task": "semantic_similarity"}]))
            write = next(call for call in tx.run.call_args_list if "UNWIND $concepts" in call.args[0])
            assert write.kwargs["concepts"][0]["label"] == text
            assert write.kwargs["concepts"][0]["semantic_input_hash"] == identity.semantic_input_hash
        else:
            result = agent_api.preference_candidates(agent_api.PreferenceCandidateRequest(
                requester_user_id="owner", topic=text))
            assert result["normalized_topic"] == text
            assert result["canonical_key"] == identity.key
            assert result["candidate_ids"] == ["synthetic-candidate"]
        assert result["status"] == "success"


@pytest.mark.parametrize("surface", ["apply", "registration_component", "correction", "embedding", "exact"])
def test_graph_semantic_501_rejects_before_any_graph_call(surface):
    from pydantic import ValidationError
    text = _boundary_preference_text(501)
    allowed_identity = canonicalize_concept(_boundary_preference_text(500))
    bad_item = {**allowed_identity.as_dict(), "semantic_text": text, "label": text,
                "stance": "like", "confidence": .99, "category": "activity",
                "evidence_span": "Reading long-form stories"}
    with patch.object(agent_api.GraphDatabase, "driver") as driver:
        if surface == "exact":
            with pytest.raises(ValidationError):
                agent_api.PreferenceCandidateRequest(requester_user_id="owner", topic=text)
        elif surface == "registration_component":
            tx = MagicMock()
            with pytest.raises(PreferenceTextError, match="preference_text_too_long"):
                seed_registration(tx, "owner", registration_message_id("owner"), [bad_item])
            tx.run.assert_not_called()
        else:
            if surface == "apply":
                result = asyncio.run(agent_api.apply_memory(agent_api.MemoryApplyRequest(user_id="owner", memories=[bad_item])))
            elif surface == "correction":
                result = asyncio.run(agent_api.memory_action(agent_api.MemoryActionRequest(
                    user_id="owner", key=allowed_identity.key, action="correct", value=text)))
            else:
                result = agent_api.project_concept_embeddings(agent_api.ConceptEmbeddingProjectionRequest(
                    concepts=[{**bad_item, "kind": "interest", "embedding": [1.] * 768}]))
            assert result["status"] == "error"
            assert result["error_code"] == "preference_text_too_long"
        driver.assert_not_called()


@pytest.mark.parametrize("source", ["阅读科幻小说", "閱讀科幻小說"])
@pytest.mark.parametrize("packet", [False, True])
def test_fresh_exact_request_unifies_scripts_but_verified_packet_stays_frozen(source, packet):
    from concept_identity import canonicalize_fresh_concept
    identity = canonicalize_concept(source) if packet else canonicalize_fresh_concept(source)
    metadata, driver, session = MagicMock(), MagicMock(), MagicMock()
    metadata.single.return_value = identity.as_dict()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    session.run.side_effect = [metadata, [{"candidate_id": "synthetic"}], []]
    fields = {name: identity.as_dict()[name] for name in (
        "canonical_key", "canonicalization_version", "semantic_text", "semantic_input_hash")} if packet else {}
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
        result = agent_api.preference_candidates(agent_api.PreferenceCandidateRequest(
            requester_user_id="owner", topic=source, **fields))
    assert result["status"] == "success"
    assert result["candidate_ids"] == ["synthetic"]
    assert result["canonical_key"] == identity.key
    assert result["normalized_topic"] == (source if packet else "閱讀科幻小說")
    assert session.run.call_args_list[0].kwargs["key"] == identity.key


@pytest.mark.parametrize("request_class, endpoint", [
    (agent_api.PreferenceCandidateRequest, agent_api.preference_candidates),
    (agent_api.PreferenceSemanticCandidateRequest, agent_api.preference_semantic_candidates),
])
@pytest.mark.parametrize("fault", ["partial", "source", "key", "hash"])
def test_fresh_request_rejects_partial_or_mismatched_identity_packet(request_class, endpoint, fault):
    identity = canonicalize_concept("阅读科幻小说")
    fields = {name: identity.as_dict()[name] for name in (
        "canonical_key", "canonicalization_version", "semantic_text", "semantic_input_hash")}
    if fault == "partial":
        fields.pop("semantic_input_hash")
    elif fault == "source":
        fields["semantic_text"] = "閱讀科幻小說"
    elif fault == "key":
        fields["canonical_key"] = canonicalize_concept("閱讀科幻小說").key
    else:
        fields["semantic_input_hash"] = "0" * 64
    with patch.object(agent_api.GraphDatabase, "driver") as driver:
        result = endpoint(request_class(requester_user_id="owner", topic="阅读科幻小说", **fields))
    assert result["error_code"] == "preference_search_identity_mismatch"
    assert result["retryable"] is False
    driver.assert_not_called()


@pytest.mark.parametrize("surface", ["apply", "registration"])
def test_validated_v2_apply_and_registration_do_not_reconvert_stored_source(surface):
    import registration_graph
    identity = canonicalize_concept("阅读科幻小说")
    item = {**identity.as_dict(), "stance": "like", "category": "activity",
            "confidence": .99, "evidence_span": "阅读科幻小说"}
    driver, session, tx = MagicMock(), MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    session.execute_write.side_effect = lambda callback, *args: callback(tx, *args)
    tx.run.return_value.single.return_value = {"seed_allowed": True}
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver), \
            patch.object(agent_api, "canonicalize_fresh_concept", side_effect=AssertionError("stored source must not reconvert")), \
            patch.object(agent_api, "normalize_fresh_preference_text", side_effect=AssertionError("stored source must not reconvert")):
        result = (asyncio.run(agent_api.apply_memory(agent_api.MemoryApplyRequest(user_id="owner", memories=[item])))
                  if surface == "apply" else {"memories": registration_graph.seed_registration(
                      tx, "owner", registration_message_id("owner"), [item])})
    assert result["memories"][0]["semantic_text"] == "阅读科幻小说"
    assert result["memories"][0]["key"] == identity.key
    assert result["memories"][0]["semantic_input_hash"] == identity.semantic_input_hash


def test_fresh_correction_and_feedback_apply_one_language_contract():
    import json
    from concept_identity import canonicalize_fresh_concept
    from unittest.mock import AsyncMock
    old, fresh = canonicalize_concept("Reading"), canonicalize_fresh_concept("阅读科幻小说")
    driver, session, tx = MagicMock(), MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    session.execute_write.side_effect = lambda callback: callback(tx)
    tx.run.return_value.single.return_value = old.as_dict()
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
        corrected = asyncio.run(agent_api.memory_action(agent_api.MemoryActionRequest(
            user_id="owner", key=old.key, action="correct", value="阅读科幻小说")))
    assert corrected["key"] == fresh.key
    correction = next(call for call in tx.run.call_args_list if "DELETE existing" in call.args[0])
    assert correction.kwargs["label"] == "閱讀科幻小說"
    assert correction.kwargs["semantic_input_hash"] == fresh.semantic_input_hash
    with patch.object(agent_api.agent, "generate_graph_reflection", return_value=json.dumps({
        "relationships": [{"relation_type": "DISLIKES_TRAIT", "trait": "阅读科幻小说"}]})), \
            patch.object(agent_api, "apply_memory", new_callable=AsyncMock,
                         return_value={"status": "success", "memories": []}) as write:
        asyncio.run(agent_api.receive_feedback(agent_api.FeedbackRequest(
            user_id="owner", target_id="other", action="decline", target_traits={},
            explicit_reasons=["兴趣：阅读科幻小说"])))
    memory = write.call_args.args[0].memories[0]
    assert memory["semantic_text"] == "閱讀科幻小說"
    assert memory["key"] == fresh.key


def test_matchmaker_fresh_query_and_persisted_packet_have_distinct_boundaries():
    import matchmaker
    raw = matchmaker.safe_search_context({"search_intent": "preference", "normalized_topic": "阅读科幻小说"})
    assert raw["semantic_text"] == "閱讀科幻小說"
    old = canonicalize_concept("阅读科幻小说")
    packet = {"search_intent": "preference", "normalized_topic": old.semantic_text,
              "semantic_text": old.semantic_text, "canonical_preference_key": old.key,
              "canonicalization_version": "v2", "semantic_input_hash": old.semantic_input_hash}
    with patch.object(matchmaker, "canonicalize_fresh_concept", side_effect=AssertionError("stored source must not reconvert")), \
            patch.object(matchmaker, "normalize_fresh_preference_text", side_effect=AssertionError("stored source must not reconvert")):
        stored = matchmaker.safe_search_context(packet)
    assert stored["semantic_text"] == old.semantic_text
    assert stored["canonical_preference_key"] == old.key
    assert stored["semantic_input_hash"] == old.semantic_input_hash
