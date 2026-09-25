"""499/500/501 preference boundary matrix using real services and offline I/O.

No model, Graph, or remote database is contacted. Registration's actual form
contract is 120 characters; the full durable-memory contract is independently
500. A 500-character concept may use a grounded evidence span of <=160 chars.
"""

import hashlib
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import mongomock
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from matchmaker_agent.concept_identity import PreferenceTextError, canonicalize_concept
from models import ProfileMemoryAddRequest, ProfileMemoryActionRequest, ProfileUpdateRequest
from routers import system
from services import ai_service, concept_embedding_service as worker
from services import memory_service as memory, preference_store as facts
from services import profile_skills, registration_graph_service as registration
from services import preference_candidate_service as exact
from services.ayue_agent.contracts import AgentTurnContext
from services.ayue_agent.shared import write_executors as writes
from services.match_search_context import safe_search_context, validate_persisted_search_context


@pytest.fixture(params=[499, 500, 501], ids=lambda value: f"chars-{value}")
def semantic_text(request):
    prefix = "Quiet accessible castle visits "
    suffix = " with wheelchair access and no guided tours"
    text = prefix + "x" * (request.param - len(prefix) - len(suffix)) + suffix
    assert len(text) == request.param
    return text


class TestPreferenceLengthBoundary:
    def test_extraction_semantic_bound_is_independent_of_evidence_bound(self, semantic_text):
        evidence = "with wheelchair access and no guided tours"
        payload = {"recent_context": {"action": "none", "message_kind": "durable_preference", "confidence": .99},
                   "memories": [{"key": "untrusted", "label_zh_tw": semantic_text, "stance": "like",
                                 "category": "activity", "confidence": .99,
                                 "evidence_span": evidence, "subject": "owner"}]}
        with patch.object(profile_skills, "generate_chat_completion", return_value=Mock(content=json.dumps(payload))):
            result = profile_skills.analyze_profile_message("我喜歡 " + semantic_text)
        if len(semantic_text) == 501:
            assert result["memories"] == []
            assert result["memory_codes"] == ["memory_contract_limit_rejected"]
        else:
            item = result["memories"][0]
            assert item["semantic_text"] == semantic_text
            assert item["key"] == canonicalize_concept(semantic_text).key
            assert item["evidence_span"] == evidence and len(evidence) <= 160
            assert len(item["display_label"]) <= 40

    def test_proposal_validation_never_retains_a_valid_batch_prefix(self, semantic_text):
        proposals = [{"label": "Coffee Shop", "stance": "like"}, {"label": semantic_text, "stance": "like"}]
        if len(semantic_text) == 501:
            with pytest.raises(memory.MemoryWriteError) as raised:
                memory.validate_memory_proposals(proposals)
            assert raised.value.error_code == "preference_text_too_long"
            assert raised.value.retryable is False
        else:
            result = memory.validate_memory_proposals(proposals)
            assert len(result) == 2
            assert result[1]["semantic_text"] == semantic_text
            assert result[1]["semantic_input_hash"] == canonicalize_concept(semantic_text).semantic_input_hash

    def test_mongo_projection_preserves_the_boundary_or_writes_nothing(self, semantic_text):
        collection = mongomock.MongoClient().db.preferences
        items = [{"label": "Coffee Shop", "stance": "like"}, {"label": semantic_text, "stance": "like"}]
        with patch.object(facts, "PREFERENCE_FACTS", collection), patch.object(facts, "projection_write", return_value=nullcontext(None)):
            if len(semantic_text) == 501:
                with pytest.raises(PreferenceTextError) as raised:
                    facts.upsert_preference_facts("synthetic-owner", items, source="synthetic")
                assert raised.value.code == "preference_text_too_long"
                assert collection.count_documents({}) == 0
            else:
                saved = facts.upsert_preference_facts("synthetic-owner", items, source="synthetic")
                stored = collection.find_one({"concept_key": canonicalize_concept(semantic_text).key})
                assert saved[1]["semantic_text"] == stored["semantic_text"] == semantic_text
                assert stored["label"] == semantic_text
                assert len(stored["display_label"]) <= 40

    def test_manual_add_contract_does_not_reuse_short_display_as_identity(self, semantic_text):
        with patch.object(memory, "apply_profile_memory_proposals") as apply:
            if len(semantic_text) == 501:
                with pytest.raises(ValidationError) as raised:
                    ProfileMemoryAddRequest(user_id="synthetic-owner", label=semantic_text, request_id="synthetic-request")
                assert raised.value.errors()[0]["type"] == "string_too_long"
                apply.assert_not_called()
            else:
                identity = canonicalize_concept(semantic_text)
                apply.return_value = [{**identity.as_dict(), "stance": "like"}]
                result = system.add_profile_memory(ProfileMemoryAddRequest(
                    user_id="synthetic-owner", label=semantic_text, request_id="synthetic-request",
                ))
                assert result["memory"]["semantic_text"] == semantic_text
                assert apply.call_args.args[1][0]["semantic_text"] == semantic_text

    def test_manual_correction_keeps_full_value_or_rejects_before_http(self, semantic_text):
        response = Mock()
        response.json.return_value = {"status": "success"}
        request = ProfileMemoryActionRequest(user_id="synthetic-owner", key="legacy_key", action="correct", value=semantic_text)
        with patch.object(memory.requests, "post", return_value=response) as post, \
                patch.object(memory, "_invalidate_memory_projection") as invalidate, \
                patch.object(memory, "_sync_memory_projection"):
            if len(semantic_text) == 501:
                with pytest.raises(HTTPException) as raised:
                    system.profile_memory_action(request)
                assert raised.value.status_code == 422
                assert raised.value.detail["code"] == "preference_text_too_long"
                post.assert_not_called()
                invalidate.assert_not_called()
            else:
                assert system.profile_memory_action(request)["status"] == "success"
                assert post.call_args.args[0].endswith("/api/v2/memory/action")
                assert post.call_args.kwargs["json"]["value"] == semantic_text

    def test_search_sanitizer_and_replay_share_complete_semantic_source(self, semantic_text):
        source = {"search_intent": "preference", "normalized_topic": semantic_text}
        if len(semantic_text) == 501:
            with pytest.raises(PreferenceTextError) as raised:
                safe_search_context(source)
            assert raised.value.code == "preference_text_too_long"
        else:
            result = safe_search_context(source)
            assert result["semantic_text"] == result["normalized_topic"] == semantic_text
            assert result["canonical_preference_key"] == canonicalize_concept(semantic_text).key
            assert validate_persisted_search_context(result) == result

    def test_exact_adapter_transports_complete_topic_or_makes_no_request(self, semantic_text):
        response = Mock()
        with patch.object(exact.requests, "post", return_value=response) as post:
            if len(semantic_text) == 501:
                with pytest.raises(PreferenceTextError) as raised:
                    exact.retrieve_preference_candidate_ids("synthetic-owner", semantic_text, excluded_user_ids=set(), limit=20)
                assert raised.value.code == "preference_text_too_long"
                post.assert_not_called()
            else:
                identity = canonicalize_concept(semantic_text)
                response.json.return_value = {"status": "success", "canonical_key": identity.key, "candidate_ids": ["synthetic-candidate"]}
                result = exact.retrieve_preference_candidate_ids("synthetic-owner", semantic_text, excluded_user_ids=set(), limit=20)
                assert result["candidate_ids"] == ["synthetic-candidate"]
                assert post.call_args.kwargs["json"]["topic"] == semantic_text

    def test_confirmation_preserves_whole_input_separate_from_display(self, semantic_text):
        ctx = AgentTurnContext(user_id="synthetic-owner", room_id="room", message="幫我找喜歡 " + semantic_text + " 的人")
        with patch.object(writes, "assess_match_opportunity", return_value=SimpleNamespace(state="ready")), \
                patch.object(writes, "_claim_once", return_value=True) as claim, \
                patch.object(writes, "_finish"), \
                patch.object(writes, "start_match_search", return_value={"status": "queued"}) as start:
            payload, preview = writes.prepare_write_confirmation(
                "match.start_search", {"search_request": {"kind": "preference", "topic": semantic_text}},
                ctx, SimpleNamespace(active_proposal=None),
            )
            if len(semantic_text) == 501:
                assert payload is None and "過長" in preview
                claim.assert_not_called()
                start.assert_not_called()
            else:
                assert payload["data"]["search_context"]["semantic_text"] == semantic_text
                assert len(payload["data"]["search_context"]["display_label"]) <= 40
                ok, _, code = writes._start_search(ctx, "run", 0, confirmation_id="confirmation", payload=payload["data"])
                assert ok and code is None
                assert start.call_args.kwargs["search_context"]["semantic_text"] == semantic_text

    def test_worker_and_real_provider_wrapper_preserve_complete_input(self, semantic_text, monkeypatch):
        worker._projection_cache.clear()
        monkeypatch.setattr(ai_service, "GOOGLE_EMBEDDING_MODEL", "models/gemini-embedding-2")
        provider = Mock(side_effect=lambda **kwargs: {"embedding": [[1.0] + [0.0] * 767 for _ in kwargs["content"]]})
        pool = Mock(side_effect=lambda operation: operation("synthetic-not-a-credential"))
        read, write, mongo = Mock(), Mock(), Mock(return_value={})
        write.return_value.json.return_value = {"status": "success", "embedded_count": 1, "pending_count": 0}
        if len(semantic_text) == 501:
            # No valid v2 identity can be constructed for 501 characters.
            item = {**canonicalize_concept("Coffee Shop").as_dict(), "semantic_text": semantic_text,
                    "semantic_input_hash": hashlib.sha256(semantic_text.encode()).hexdigest()}
        else:
            item = {**canonicalize_concept(semantic_text).as_dict(), "label": "display is not source"}
        read.return_value.json.return_value = {"status": "success", "concepts": [item]}
        with patch.object(worker.requests, "get", read), patch.object(worker.requests, "post", write), \
                patch.object(worker, "_mongo_kind_overrides", mongo), \
                patch.object(ai_service.genai, "embed_content", provider), \
                patch.object(ai_service.google_key_pool, "execute", pool):
            result = worker.process_pending_concept_embeddings()
        worker._projection_cache.clear()
        if len(semantic_text) == 501:
            assert result["status"] == "stalled" and result["retryable"] is False
            assert result["error_code"] == "embedding_identity_unverified"
            mongo.assert_not_called()
            pool.assert_not_called()
            provider.assert_not_called()
            write.assert_not_called()
        else:
            assert result["status"] == "success"
            assert provider.call_args.kwargs["content"] == ["task: sentence similarity | query: " + semantic_text]
            assert provider.call_args.kwargs["output_dimensionality"] == 768
            projected = write.call_args.kwargs["json"]["concepts"][0]
            assert projected["semantic_text"] == projected["label"] == semantic_text
            assert projected["key"] == canonicalize_concept(semantic_text).key

    def test_provider_wrapper_independently_rejects_overlimit_before_key_pool(self, semantic_text):
        with patch.object(ai_service.google_key_pool, "execute", return_value=[[1.0] + [0.0] * 767]) as pool:
            if len(semantic_text) == 501:
                with pytest.raises(PreferenceTextError) as raised:
                    ai_service.get_embeddings([semantic_text], task_type="semantic_similarity", output_dimensionality=768)
                assert raised.value.code == "preference_text_too_long"
                pool.assert_not_called()
            else:
                assert len(ai_service.get_embeddings([semantic_text], task_type="semantic_similarity", output_dimensionality=768)[0]) == 768
                pool.assert_called_once()

    def test_registration_form_does_not_inherit_the_500_concept_limit(self, semantic_text):
        with pytest.raises(ValidationError) as raised:
            ProfileUpdateRequest(user_id="synthetic-owner", interest=semantic_text)
        assert raised.value.errors()[0]["type"] == "string_too_long"
        with patch.object(profile_skills, "analyze_profile_message") as extract:
            with pytest.raises(PreferenceTextError) as raised:
                registration.registration_memories(semantic_text)
        assert raised.value.code == "preference_text_too_long"
        extract.assert_not_called()
