"""Offline provider/worker/context spies: complete preference text, no network."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from matchmaker_agent.concept_identity import PreferenceTextError, canonicalize_concept
from services import ai_service, concept_embedding_service as worker
from services.owner_memory_projection import preference_wording


LONG_TEXTS = [
    "Walking along riverside routes with accessible wheelchair access and step-free paths",
    "Visiting historical castles in quiet surroundings with no guided tours",
    "Sharing an evening meal with vegetarian food and alcohol-free drinks rather than whisky",
    "Enjoying an afternoon of watching basketball rather than playing basketball",
    "Spending quiet evenings reading mystery novels rather than writing mystery novels",
]


@pytest.fixture
def provider_spy(monkeypatch):
    provider = Mock(side_effect=lambda **kwargs: {
        "embedding": [[1.0] + [0.0] * 767 for _ in kwargs["content"]],
    })
    pool = Mock(side_effect=lambda operation: operation("synthetic-not-a-key"))
    monkeypatch.setattr(ai_service.genai, "embed_content", provider)
    monkeypatch.setattr(ai_service.google_key_pool, "execute", pool)
    return provider, pool


@pytest.mark.parametrize("model", ["models/gemini-embedding-2", "models/gemini-embedding-001"])
def test_provider_receives_complete_qualifiers_with_existing_model_contract(monkeypatch, provider_spy, model):
    provider, _ = provider_spy
    monkeypatch.setattr(ai_service, "GOOGLE_EMBEDDING_MODEL", model)
    result = ai_service.get_embeddings(LONG_TEXTS, task_type="semantic_similarity", output_dimensionality=768)
    request = provider.call_args.kwargs
    prefix = "task: sentence similarity | query: " if model.endswith("-2") else ""
    assert request["content"] == [prefix + text for text in LONG_TEXTS]
    assert request["model"] == model and request["output_dimensionality"] == 768
    assert len(result) == len(LONG_TEXTS)
    if not prefix:
        assert request["task_type"] == "semantic_similarity"


@pytest.mark.parametrize("bad_batch", [
    ["K-pop", "x" * 501], ["K-pop", "ﬃ" * 200], ["K-pop", None],
    ["K-pop", ""], ["K-pop"] * 21, "K-pop",
])
def test_whole_embedding_batch_validation_precedes_provider(provider_spy, bad_batch):
    provider, pool = provider_spy
    with pytest.raises(PreferenceTextError):
        ai_service.get_embeddings(bad_batch, task_type="semantic_similarity", output_dimensionality=768)
    pool.assert_not_called()
    provider.assert_not_called()


@pytest.fixture
def worker_spies(monkeypatch):
    worker._projection_cache.clear()
    read = Mock()
    read.return_value.raise_for_status.return_value = None
    write = Mock()
    write.return_value.raise_for_status.return_value = None
    write.return_value.json.return_value = {"status": "success", "embedded_count": 1, "pending_count": 0}
    embedding = Mock(side_effect=lambda texts, **kwargs: [[1.0] + [0.0] * 767 for _ in texts])
    mongo = Mock(return_value={})
    monkeypatch.setattr(worker.requests, "get", read)
    monkeypatch.setattr(worker.requests, "post", write)
    monkeypatch.setattr(worker, "get_embeddings", embedding)
    monkeypatch.setattr(worker, "_mongo_kind_overrides", mongo)
    yield read, write, embedding, mongo
    worker._projection_cache.clear()


def test_worker_embeds_verified_semantic_not_shortened_display(worker_spies):
    read, write, embedding, _ = worker_spies
    identity = canonicalize_concept(LONG_TEXTS[0])
    item = {**identity.as_dict(), "label": "stale short display", "display_label": "display only", "suggested_kind": "interest"}
    read.return_value.json.return_value = {"status": "success", "concepts": [item]}
    result = worker.process_pending_concept_embeddings()
    assert result["embedded_count"] == 1
    assert embedding.call_args.args[0] == [identity.semantic_text]
    projected = write.call_args.kwargs["json"]["concepts"][0]
    assert projected["semantic_text"] == identity.semantic_text
    assert projected["label"] == identity.semantic_text
    assert projected["key"] == identity.key
    assert projected["semantic_input_hash"] == identity.semantic_input_hash
    assert projected["canonicalization_version"] == "v2"
    assert projected["embedding_model"] == ai_service.GOOGLE_EMBEDDING_MODEL
    assert projected["embedding_task"] == "semantic_similarity"


@pytest.mark.parametrize("bad", [
    {"key": "legacy", "label": "x" * 501},
    {"key": "legacy", "label": "ﬃ" * 200},
    {"key": "k" * 101, "label": "K-pop"},
    {"key": "v2_" + "a" * 48, "label": "stale projection"},
    {"key": "bad", "canonicalization_version": "v2", "semantic_text": "K-pop"},
    None,
])
def test_invalid_pending_item_rejects_whole_batch_before_mongo_provider_write(worker_spies, bad):
    read, write, embedding, mongo = worker_spies
    valid = canonicalize_concept(LONG_TEXTS[0]).as_dict()
    read.return_value.json.return_value = {"status": "success", "concepts": [valid, bad]}
    result = worker.process_pending_concept_embeddings()
    assert result["status"] == "stalled" and result["retryable"] is False
    mongo.assert_not_called()
    embedding.assert_not_called()
    write.assert_not_called()


def test_pending_fanout_over_batch_limit_rejects_instead_of_dropping_tail(worker_spies):
    read, write, embedding, mongo = worker_spies
    read.return_value.json.return_value = {"status": "success", "concepts": [
        {"key": "legacy", "label": "K-pop"} for _ in range(21)
    ]}
    assert worker.process_pending_concept_embeddings()["status"] == "stalled"
    mongo.assert_not_called()
    embedding.assert_not_called()
    write.assert_not_called()


def test_legacy_event_source_is_complete_without_rekey_or_identity_promotion(worker_spies):
    read, write, embedding, _ = worker_spies
    read.return_value.json.return_value = {"status": "success", "concepts": [{
        "key": "existing_event_key", "label": LONG_TEXTS[1], "suggested_kind": "activity",
    }]}
    assert worker.process_pending_concept_embeddings()["status"] == "success"
    assert embedding.call_args.args[0] == [LONG_TEXTS[1]]
    projected = write.call_args.kwargs["json"]["concepts"][0]
    assert projected["key"] == "existing_event_key"
    assert projected["canonicalization_version"] == "legacy_unknown"
    assert projected.get("fidelity_status") != "complete"


def test_failed_projection_cache_is_invalidated_by_source_or_model_change(worker_spies, monkeypatch):
    read, write, embedding, _ = worker_spies
    write.return_value.json.return_value = {"status": "success", "embedded_count": 0, "pending_count": 1}
    item = {"key": "legacy", "label": LONG_TEXTS[0], "suggested_kind": "interest"}
    read.return_value.json.side_effect = [
        {"status": "success", "concepts": [item]},
        {"status": "success", "concepts": [item]},
        {"status": "success", "concepts": [{**item, "label": LONG_TEXTS[1]}]},
        {"status": "success", "concepts": [{**item, "label": LONG_TEXTS[1]}]},
    ]
    worker.process_pending_concept_embeddings()
    worker.process_pending_concept_embeddings()
    assert embedding.call_count == 1
    worker.process_pending_concept_embeddings()
    assert embedding.call_count == 2
    monkeypatch.setattr(ai_service, "GOOGLE_EMBEDDING_MODEL", "models/synthetic-other-contract")
    worker.process_pending_concept_embeddings()
    assert embedding.call_count == 3


def test_owner_v2_context_uses_complete_verified_text_and_enforces_owner_and_polarity():
    item = {**canonicalize_concept(LONG_TEXTS[2]).as_dict(), "owner_user_id": "owner", "stance": "avoid", "active": True}
    before = deepcopy(item)
    assert preference_wording([item], owner_id="owner") == ["避免：" + LONG_TEXTS[2]]
    assert preference_wording([item], owner_id="other") == []
    assert preference_wording([{**item, "active": False}], owner_id="owner") == []
    assert preference_wording([{**item, "semantic_text": "shortened"}], owner_id="owner") == []
    assert item == before


def test_owner_full_text_protection_cannot_be_bypassed_after_old_sixty_char_boundary():
    protected = "a" * 65 + " seed_user_99"
    item = {**canonicalize_concept(protected).as_dict(), "owner_user_id": "owner", "stance": "like"}
    assert preference_wording([item], owner_id="owner") == []
    legacy = {"key": "old", "label": protected, "stance": "like"}
    assert preference_wording([legacy], owner_id="owner") == []


def test_owner_legacy_read_does_not_mutate_identity_and_rejects_oversize():
    legacy = {"key": "old", "label": LONG_TEXTS[4], "stance": "like"}
    before = deepcopy(legacy)
    assert preference_wording([legacy], owner_id="owner") == ["喜歡：" + LONG_TEXTS[4]]
    assert legacy == before
    assert preference_wording([{**legacy, "label": "x" * 501}], owner_id="owner") == []


def test_legacy_slug_starting_v2_is_not_a_versioned_digest(worker_spies):
    read, write, embedding, _ = worker_spies
    legacy = {"key": "v2_rocket_modeling", "label": "V2 Rocket Modeling", "stance": "like"}
    read.return_value.json.return_value = {"status": "success", "concepts": [legacy]}
    assert worker.process_pending_concept_embeddings()["status"] == "success"
    assert embedding.call_args.args[0] == [legacy["label"]]
    projected = write.call_args.kwargs["json"]["concepts"][0]
    assert projected["key"] == legacy["key"]
    assert projected["canonicalization_version"] == "legacy_unknown"
    assert preference_wording([legacy], owner_id="owner") == ["喜歡：V2 Rocket Modeling"]
