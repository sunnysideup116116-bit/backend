"""Fresh-input script parity and immutable stored-v2 evidence, entirely offline."""

import json
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import mongomock
import pytest
from pydantic import ValidationError

from matchmaker_agent import concept_identity as identity
from models import ProfileMemoryAddRequest
from routers import system
from services import memory_service as memory, preference_store as facts
from services import profile_skills, registration_graph_service as registration
from services import preference_candidate_service as exact, preference_semantic_service as semantic
from services.ayue_agent.contracts import AgentTurnContext
from services.ayue_agent.pi.match_tools import MatchSearchInput, _prepare_search
from services.ayue_agent.shared import write_executors as writes
from services.match_search_context import safe_search_context, validate_persisted_search_context


class TestFreshNormalizationContract:
    @pytest.mark.parametrize("source", [
        "安静的咖啡厅", "安靜的咖啡廳", "  安静的咖啡厅（免门票）  ",
        "  Board   Games  ", "Science-fiction", "Kpop", "Ｋ－ｐｏｐ", "阅读 Mystery Novels",
    ])
    def test_all_fresh_owner_paths_use_the_same_identity(self, source):
        expected = identity.canonicalize_fresh_concept(source)
        with patch.object(memory, "apply_profile_memory_proposals", side_effect=lambda _uid, items, *_a: items), \
                patch.object(system, "normalize_zh_tw", side_effect=AssertionError("UI converter is not identity authority")):
            manual = system.add_profile_memory(ProfileMemoryAddRequest(
                user_id="synthetic-owner", label=source, request_id="synthetic-request",
            ))["memory"]
        assert manual["key"] == expected.key
        raw = {"label": source, "stance": "like", "category": "activity"}
        assert memory.validate_memory_proposals([raw])[0]["key"] == expected.key
        assert facts._clean_item(raw)["concept_key"] == expected.key
        context = safe_search_context({"search_intent": "preference", "normalized_topic": source})
        assert context["canonical_preference_key"] == expected.key

        payload = {"recent_context": {"action": "none", "confidence": .99, "message_kind": "durable_preference"},
                   "memories": [{"key": "untrusted", "label_zh_tw": source, "stance": "like",
                                 "category": "activity", "confidence": .99, "subject": "owner",
                                 "evidence_span": source.strip()}]}
        with patch.object(profile_skills, "generate_chat_completion", return_value=Mock(content=json.dumps(payload))):
            extracted = profile_skills.analyze_profile_message("我喜歡 " + source)["memories"][0]
            registered = registration.registration_memories(source)[0]
        assert extracted["key"] == registered["key"] == expected.key

        response = Mock()
        response.json.return_value = {"status": "success"}
        with patch.object(memory.requests, "post", return_value=response) as post, \
                patch.object(memory, "_invalidate_memory_projection"), patch.object(memory, "_sync_memory_projection"):
            memory.apply_memory_action("synthetic-owner", "old_key", "correct", source)
        assert identity.canonicalize_concept(post.call_args.kwargs["json"]["value"]).key == expected.key

        ctx = AgentTurnContext(user_id="synthetic-owner", room_id="room", message="幫我找喜歡 " + source + " 的人")
        runtime = SimpleNamespace(turn=SimpleNamespace(_raw_ctx=ctx, message=ctx.message, recent_messages=[]))
        with patch.object(writes, "assess_match_opportunity", return_value=SimpleNamespace(state="ready")):
            prepared, _ = _prepare_search(runtime, MatchSearchInput(kind="preference", topic=source))
        assert prepared is not None
        assert prepared["data"]["search_context"]["canonical_preference_key"] == expected.key

    def test_stored_simplified_v2_is_not_reconverted_even_if_fresh_dependency_is_unavailable(self):
        stored = {**identity.canonicalize_concept("安静的咖啡厅").as_dict(), "stance": "like"}
        original = deepcopy(stored)
        context = {"search_intent": "preference", "normalized_topic": stored["semantic_text"],
                   "semantic_text": stored["semantic_text"], "canonical_preference_key": stored["key"],
                   "canonicalization_version": "v2", "semantic_input_hash": stored["semantic_input_hash"]}
        collection = mongomock.MongoClient().db.preferences
        response = Mock()
        response.json.return_value = {"status": "success", "canonical_key": stored["key"],
                                     "candidate_ids": [], "candidates": []}
        with patch.object(identity, "_fresh_preference_converter", side_effect=identity.PreferenceTextError("preference_normalizer_unavailable")), \
                patch.object(facts, "PREFERENCE_FACTS", collection), \
                patch.object(facts, "projection_write", return_value=nullcontext(None)), \
                patch.object(exact.requests, "post", return_value=response) as post:
            assert memory.validate_memory_proposals([stored])[0]["key"] == stored["key"]
            assert memory.normalize_memory_item(stored)["semantic_text"] == "安静的咖啡厅"
            assert facts.upsert_preference_facts("synthetic-owner", [stored], source="synthetic")[0]["key"] == stored["key"]
            assert safe_search_context(context)["semantic_text"] == "安静的咖啡厅"
            assert validate_persisted_search_context(context)["canonical_preference_key"] == stored["key"]
            exact.retrieve_preference_candidate_ids("synthetic-owner", stored["semantic_text"], excluded_user_ids=set(), limit=20)
            semantic.retrieve_semantic_preference_candidates("synthetic-owner", stored["semantic_text"], excluded_user_ids=set())
        for call in post.call_args_list:
            packet = call.kwargs["json"]
            assert packet["topic"] == packet["semantic_text"] == "安静的咖啡厅"
            assert packet["canonical_key"] == stored["key"]
            assert packet["canonicalization_version"] == "v2"
            assert packet["semantic_input_hash"] == stored["semantic_input_hash"]
        assert post.call_count == 2
        assert stored == original

    @pytest.mark.parametrize("code", ["preference_normalizer_unavailable", "preference_normalizer_version_mismatch"])
    def test_missing_or_wrong_dependency_never_falls_back_to_ui_normalization(self, code):
        with patch.object(identity, "_fresh_preference_converter", side_effect=identity.PreferenceTextError(code)), \
                patch.object(profile_skills, "generate_chat_completion") as model, \
                patch.object(memory.requests, "post") as post:
            result = profile_skills.analyze_profile_message("我喜歡安静的咖啡厅")
            assert result["memories"] == [] and result["memory_codes"] == [code]
            with pytest.raises(identity.PreferenceTextError) as raised:
                safe_search_context({"search_intent": "preference", "normalized_topic": "安静的咖啡厅"})
            assert raised.value.code == code
            with pytest.raises(memory.MemoryWriteError) as raised:
                memory.validate_memory_proposals([{"label": "安静的咖啡厅", "stance": "like"}])
            assert raised.value.retryable is False and raised.value.error_code == code
            with pytest.raises(memory.MemoryWriteError):
                memory.apply_memory_action("synthetic-owner", "legacy", "correct", "安静的咖啡厅")
            with pytest.raises(ValidationError):
                ProfileMemoryAddRequest(user_id="synthetic-owner", label="安静的咖啡厅", request_id="synthetic-request")
        model.assert_not_called()
        post.assert_not_called()

    @pytest.mark.parametrize("length", [40, 41, 499, 500, 501])
    @pytest.mark.parametrize("kind", ["chinese", "emoji", "combining"])
    def test_manual_model_bound_counts_raw_unicode_codepoints(self, length, kind):
        source = ("静" * length if kind == "chinese" else "😀" * length if kind == "emoji"
                  else "e\u0301" * (length // 2) + ("x" if length % 2 else ""))
        assert len(source) == length
        if length > 500:
            with pytest.raises(ValidationError) as raised:
                ProfileMemoryAddRequest(user_id="synthetic-owner", label=source, request_id="synthetic-request")
            assert raised.value.errors()[0]["type"] == "string_too_long"
        else:
            result = ProfileMemoryAddRequest(user_id="synthetic-owner", label=source, request_id="synthetic-request")
            assert result.label == identity.normalize_fresh_preference_text(source)
            assert "…" not in result.label
