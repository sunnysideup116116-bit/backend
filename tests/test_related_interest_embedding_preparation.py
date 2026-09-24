from unittest.mock import Mock
import pytest
from matchmaker_agent.concept_identity import canonicalize_concept
from matchmaker_agent.related_interest_contract import embedding_fingerprint, embedding_manifest
from scripts.prepare_related_interest_embeddings import prepare_batch


def test_only_verified_full_v2_is_embedded_with_existing_gemini_contract():
    text = "Watching live football with friends near home and wheelchair accessible seating"
    identity = canonicalize_concept(text)
    embed = Mock(return_value=[[3.,4.]+[0.]*766])
    records = [identity.as_dict(), {"key":"legacy_short", "label":text[:40]},
        {**identity.as_dict(), "semantic_text": text+" altered"},
        {**identity.as_dict(), "fidelity_status": "legacy_unknown"}]
    before = repr(records)
    batch = prepare_batch(records, embed, "models/gemini-embedding-2")
    embed.assert_called_once_with([text], task_type="semantic_similarity", output_dimensionality=768,
        request_timeout_seconds=10.0)
    assert len(batch) == 1 and batch[0]["key"] == identity.key
    assert batch[0]["source_hash"] == identity.semantic_input_hash
    assert batch[0]["vector"][:2] == [.6,.8]
    assert repr(records) == before
    assert embedding_manifest("gemini-embedding-2")["prefix"] == "task: sentence similarity | query: "
    assert batch[0]["fingerprint"] == embedding_fingerprint("gemini-embedding-2")


@pytest.mark.parametrize("vectors", [[], [[0.]*768], [[1.]*767], [[float('nan')]+[0.]*767]])
def test_bad_embedding_batch_fails_closed(vectors):
    with pytest.raises(ValueError):
        prepare_batch([canonicalize_concept("Cooking").as_dict()], Mock(return_value=vectors), "gemini-embedding-2")


def test_empty_unverified_batch_makes_no_provider_call():
    embed = Mock(side_effect=AssertionError("must not embed legacy"))
    assert prepare_batch([{"key":"legacy", "label":"short prefix"}], embed, "gemini-embedding-2") == []
    embed.assert_not_called()


def test_provider_and_preprocessing_are_fingerprinted_not_dimension_only():
    assert embedding_fingerprint("gemini-embedding-2") != embedding_fingerprint("gemini-embedding-001")
    with pytest.raises(ValueError):
        embedding_fingerprint("deepseek-v4.1-flash:cloud")
