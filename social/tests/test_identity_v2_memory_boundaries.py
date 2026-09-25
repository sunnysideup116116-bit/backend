"""Synthetic-only durable input fidelity tests; all storage/provider I/O mocked."""

import json
from contextlib import nullcontext
from unittest.mock import Mock, patch

import mongomock
import pytest

from matchmaker_agent.concept_identity import (
    MAX_PREFERENCE_TEXT_CHARS, PreferenceTextError, canonicalize_concept,
    durable_memory_limit,
)
from services import memory_outbox_service as outbox
from services import memory_service as memory
from services import preference_store as store
from services import profile_skills as skills
from services import registration_graph_service as registration


class TestInputFidelityMemory:
    @staticmethod
    def proposal(label, **extra):
        return {"label": label, "key": "untrusted_model_key", "stance": "like",
                "category": "activity", "confidence": .95, **extra}

    @staticmethod
    def extraction(*labels):
        return Mock(content=json.dumps({
            "recent_context": {"action": "none", "confidence": .99,
                               "message_kind": "durable_preference"},
            "memories": [{"label_zh_tw": label, "key": "untrusted_model_key",
                          "stance": "like", "category": "activity", "confidence": .99,
                          "evidence_span": label, "subject": "owner"} for label in labels],
        }))

    @pytest.mark.parametrize("suffix", ["wheelchair accessible and step-free", "quiet with no guided tours",
                                       "vegetarian and alcohol-free", "watching rather than playing",
                                       "reading rather than writing"])
    def test_extractor_preserves_complete_qualifier(self, suffix):
        label = "A preference for planned leisure activities that are " + suffix
        with patch.object(skills, "generate_chat_completion", return_value=self.extraction(label)):
            decision = skills.analyze_profile_message("我喜歡 " + label)
        item = decision["memories"][0]
        assert item["semantic_text"] == label
        assert item["label"] == label
        assert len(item["display_label"]) <= 40
        assert item["canonical_key"] == canonicalize_concept(label).key
        assert item["canonicalization_version"] == "v2"

    @pytest.mark.parametrize("suffix,code", [("不要記", "blocked_input"), ("", "owner_message_too_long")])
    def test_owner_budget_never_extracts_safe_prefix(self, suffix, code):
        with patch.object(skills, "generate_chat_completion") as model:
            decision = skills.analyze_profile_message("我喜歡咖啡" + "風景" * 500 + suffix)
        assert decision["memory_codes"] == [code]
        assert decision["memories"] == []
        model.assert_not_called()

    def test_oversized_extractor_candidate_rejects_whole_operation(self):
        payload = self.extraction("Coffee", "X" * (MAX_PREFERENCE_TEXT_CHARS + 1))
        with patch.object(skills, "generate_chat_completion", return_value=payload):
            decision = skills.analyze_profile_message("我喜歡Coffee")
        assert decision["memories"] == []
        assert not decision["recent_context"]["should_update"]
        assert decision["memory_codes"] == ["memory_contract_limit_rejected"]

    def test_oversized_evidence_never_accepts_valid_prefix(self):
        assert skills._valid_evidence_span("a" * 160 + " suffix", "a" * 160) == ""

    def test_extractor_retains_existing_short_s2t_boundary_without_truncation(self):
        with patch.object(skills, "generate_chat_completion", return_value=self.extraction("阅读")):
            decision = skills.analyze_profile_message("我喜歡阅读")
        assert decision["memories"][0]["semantic_text"] == "閱讀"
        assert decision["memories"][0]["canonical_key"] == canonicalize_concept("閱讀").key

    def test_old_matchmaker_404_cannot_receive_write_through_legacy_endpoint(self):
        response = Mock(status_code=404)
        label = "A preference for quiet castle visits with no guided tours and no crowds"
        with patch.object(memory.requests, "post", return_value=response) as post, \
                patch.object(memory, "_queue_memory_retry") as queue:
            with pytest.raises(memory.MemoryWriteError, match="memory_apply_endpoint_not_found"):
                memory.apply_profile_memory_proposals("owner", [self.proposal(label)], "profile", "message")
        post.assert_called_once()
        assert post.call_args.args[0].endswith("/api/v2/memory/apply")
        sent = post.call_args.kwargs["json"]["memories"][0]
        assert sent["semantic_text"] == label
        assert sent["canonicalization_version"] == "v2"
        assert queue.call_args.args[1][0] == sent

    def test_http_writer_validates_whole_batch_before_effects(self):
        proposals = [self.proposal("Coffee"), self.proposal("X" * 501)]
        with patch.object(memory.requests, "post") as post, \
                patch.object(memory, "_queue_memory_retry") as queue, \
                patch.object(memory.profiles_coll, "update_one") as update:
            with pytest.raises(memory.MemoryWriteError) as raised:
                memory.apply_profile_memory_proposals("owner", proposals, "profile", "message")
        assert not raised.value.retryable
        assert raised.value.error_code == "preference_text_too_long"
        post.assert_not_called()
        queue.assert_not_called()
        update.assert_not_called()

    def test_batch_cap_fails_closed_without_prefix_writes(self):
        with patch.object(memory.requests, "post") as post:
            with pytest.raises(memory.MemoryWriteError, match="too_many_preferences"):
                memory.apply_profile_memory_proposals("owner", [self.proposal("Coffee")] * (durable_memory_limit() + 1), "profile", "message")
        post.assert_not_called()

    def test_mixed_polarity_and_compound_still_rejected(self):
        for text in ("我喜歡咖啡但是不喜歡酒精", "K-pop、J-pop、西洋音樂"):
            with pytest.raises(memory.MemoryWriteError, match="invalid_atomic_preference"):
                memory.validate_memory_proposals([self.proposal(text)])

    def test_mongo_overlimit_has_no_index_or_partial_writes(self):
        with patch.object(store, "ensure_preference_indexes") as indexes, \
                patch.object(store.PREFERENCE_FACTS, "update_one") as update:
            with pytest.raises(PreferenceTextError):
                store.upsert_preference_facts("owner", [self.proposal("Coffee"), self.proposal("X" * 501)], source="test")
        indexes.assert_not_called()
        update.assert_not_called()

    def test_mongo_full_source_and_metadata_round_trip(self):
        collection = mongomock.MongoClient().db.facts
        label = "A preference for quiet castle visits without guided tours and crowds"
        with patch.object(store, "PREFERENCE_FACTS", collection), patch.object(store, "db", collection.database), patch.object(store, "projection_write", return_value=nullcontext(None)):
            saved = store.upsert_preference_facts("owner", [self.proposal(label)], source="test")
            actual = store.list_preference_facts("owner")[0]
        assert saved[0]["semantic_text"] == actual["semantic_text"] == label
        assert actual["canonicalization_version"] == "v2"
        assert actual["key"] == canonicalize_concept(label).key
        assert actual["semantic_input_hash"] == canonicalize_concept(label).semantic_input_hash

    def test_legacy_cache_read_preserves_key_without_claiming_full_identity(self):
        original = {"key": "legacy_k_pop", "label": "K-pop", "stance": "like"}
        actual = memory.normalize_memory_item(original)
        assert actual["key"] == original["key"]
        assert actual["canonicalization_version"] == "legacy_unknown"
        assert actual["fidelity_status"] == "legacy_unknown"
        assert "semantic_text" not in actual
        assert original == {"key": "legacy_k_pop", "label": "K-pop", "stance": "like"}

    def test_v2_cache_does_not_rederive_identity_from_display(self):
        label = "A preference for castle visits that are quiet and have no guided tours"
        original = {**canonicalize_concept(label).as_dict(), "stance": "like"}
        actual = memory.normalize_memory_item(original)
        assert actual["label"] == actual["semantic_text"] == label
        assert actual["key"] == original["key"]

    def test_tampered_v2_cache_is_not_promoted_to_complete_evidence(self):
        original = {**canonicalize_concept("Coffee").as_dict(), "semantic_text": "Whisky"}
        actual = memory.normalize_memory_item(original)
        assert actual["label"] == ""
        assert actual["fidelity_status"] == "invalid"
        assert actual["key"] == original["key"]

    def test_v2_write_does_not_repair_tampered_identity_as_an_implicit_rekey(self):
        item = {**self.proposal("Coffee"), **canonicalize_concept("Coffee").as_dict(), "semantic_text": "Whisky"}
        with patch.object(memory.requests, "post") as post:
            with pytest.raises(memory.MemoryWriteError, match="invalid_preference_identity"):
                memory.apply_profile_memory_proposals("owner", [item], "profile", "message")
        post.assert_not_called()

    def test_legacy_mongo_read_keeps_original_identity_and_unknown_fidelity(self):
        collection = mongomock.MongoClient().db.facts
        collection.insert_one({"user_id": "owner", "concept_key": "legacy_coffee", "label": "Coffee", "active": True})
        with patch.object(store, "PREFERENCE_FACTS", collection), patch.object(store, "db", collection.database):
            actual = store.list_preference_facts("owner")[0]
        assert actual["key"] == "legacy_coffee"
        assert actual["canonicalization_version"] == "legacy_unknown"
        assert actual["semantic_text"] is None
        assert "canonicalization_version" not in collection.find_one()

    def test_state_only_update_cannot_change_stored_semantics(self):
        collection = mongomock.MongoClient().db.facts
        original = {"user_id": "owner", "concept_key": "legacy_coffee", "label": "Coffee", "active": True}
        collection.insert_one(original)
        with patch.object(store, "PREFERENCE_FACTS", collection):
            with pytest.raises(PreferenceTextError, match="identity_change_requires_correction"):
                store.set_preference_fact_state("owner", "legacy_coffee", active=False, label="Whisky")
        assert collection.find_one()["active"] is True
        assert collection.find_one()["label"] == "Coffee"

    def test_normalization_expansion_is_whole_operation_rejection(self):
        # NFKC expands the ligature to three letters, exceeding semantic 500.
        payload = self.extraction("Coffee", "ﬃ" * 167)
        # Keep grounded evidence short; the semantic source is independently bounded.
        data = json.loads(payload.content)
        data["memories"][1]["evidence_span"] = "ﬃ"
        payload.content = json.dumps(data)
        with patch.object(skills, "generate_chat_completion", return_value=payload):
            decision = skills.analyze_profile_message("我喜歡Coffee 和 ﬃ")
        assert decision["memories"] == []
        assert decision["memory_codes"] == ["preference_text_too_long"]

    def test_legacy_outbox_is_terminal_not_automatic_backfill(self):
        record = {"_id": "synthetic", "user_id": "owner", "lease_token": "lease",
                  "attempt_count": 1, "memories": [self.proposal("Coffee")], "surface": "feedback"}
        with patch.object(outbox, "_claim_one", side_effect=[record, None]), \
                patch.object(memory, "apply_profile_memory_proposals") as apply, \
                patch.object(outbox.MEMORY_OUTBOX, "update_one") as update:
            result = outbox.process_memory_outbox_once(2)
        assert result["failed"] == 1
        assert update.call_args.args[1]["$set"]["status"] == "failed"
        assert update.call_args.args[1]["$set"]["last_error_code"] == "legacy_unverified_memory_proposal"
        apply.assert_not_called()

    def test_registration_overlimit_never_projects_graph(self):
        with patch.object(registration, "read_registration_profile", return_value={"name": "Synthetic", "interest": "x" * 121}), \
                patch.object(registration.requests, "post") as post:
            with pytest.raises(memory.MemoryWriteError) as raised:
                registration.process_registration_job({"user_id": "owner"})
        assert not raised.value.retryable
        post.assert_not_called()

    def test_registration_legacy_prepared_data_never_rekeys_or_projects(self):
        with patch.object(registration, "read_registration_profile", return_value={"name": "Synthetic", "interest": "Coffee"}), \
                patch.object(registration.requests, "post") as post:
            with pytest.raises(memory.MemoryWriteError, match="legacy_unverified_memory_proposal"):
                registration.process_registration_job({"user_id": "owner", "prepared_memories": [self.proposal("Coffee")]})
        post.assert_not_called()

    def test_registration_extraction_budget_is_not_a_prefix(self):
        with patch.object(skills, "analyze_profile_message") as analyze:
            with pytest.raises(PreferenceTextError):
                registration.registration_memories("x" * 121)
        analyze.assert_not_called()

    def test_registration_rejects_overlimit_extraction_as_permanent_failure(self):
        with patch.object(skills, "analyze_profile_message", return_value={
            "contract": {"memories": []}, "memories": [],
            "memory_codes": ["memory_limit_exceeded"],
        }):
            with pytest.raises(memory.MemoryWriteError) as raised:
                registration.registration_memories("Coffee")
        assert not raised.value.retryable
        assert raised.value.error_code == "memory_limit_exceeded"

    def test_registration_evidence_rejects_oversized_original(self):
        assert registration.registration_evidence("a" * 160 + " tail", "a" * 120) == ""

    def test_correction_overlimit_has_no_http_or_cache_effects(self):
        with patch.object(memory.requests, "post") as post, \
                patch.object(memory, "_invalidate_memory_projection") as invalidate:
            with pytest.raises(memory.MemoryWriteError) as raised:
                memory.apply_memory_action("owner", "legacy_key", "correct", "x" * 501)
        assert not raised.value.retryable
        post.assert_not_called()
        invalidate.assert_not_called()

    def test_owner_lookup_overlimit_is_invalid_not_prefix_or_cache_success(self):
        with patch.object(memory.requests, "get") as get:
            result = memory.search_owner_memory("owner", "x" * 121, {
                "profile_memory_preview": [{"key": "legacy", "label": "Coffee", "stance": "like"}],
            })
        get.assert_not_called()
        assert result == {"preferences": [], "summary": "", "status": "invalid_query",
                          "error_code": "invalid_query", "source": "none", "truncated": False}

    def test_nine_atom_enumeration_never_becomes_partial_or_compound_memory(self):
        label = "Coffee、Tea、Running、Walking、Swimming、Climbing、Reading、Chess、Music"
        with patch.object(skills, "generate_chat_completion", return_value=self.extraction(label)):
            decision = skills.analyze_profile_message("我喜歡 " + label)
        assert decision["memories"] == []
        assert decision["memory_codes"] == ["memory_limit_exceeded"]

    def test_configured_cap_rejects_whole_distinct_candidate_batch(self, monkeypatch):
        monkeypatch.setenv("DURABLE_MEMORY_MAX_CANDIDATES_PER_MESSAGE", "2")
        with patch.object(skills, "generate_chat_completion", return_value=self.extraction("Coffee", "Reading", "Running")):
            decision = skills.analyze_profile_message("Coffee Reading Running 是我喜歡的事")
        assert decision["memories"] == []
        assert decision["memory_codes"] == ["memory_limit_exceeded"]
