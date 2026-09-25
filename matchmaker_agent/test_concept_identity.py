import os
from unittest.mock import patch

import pytest

from concept_identity import (
    canonical_query_provenance,
    canonicalize_concept,
    canonicalize_concept_v1,
    durable_memory_limit,
    has_mixed_preference_polarity,
    split_compound_concept_label,
    split_explicit_preference_enumeration,
)


@pytest.mark.parametrize("value", ["Kpop", "K-pop", "K pop", "k-pop"])
def test_kpop_aliases_share_one_canonical_identity(value):
    concept = canonicalize_concept(value, suggested_key="model_can_be_wrong")
    assert (concept.key, concept.label) == (canonicalize_concept("K-pop").key, "K-pop")
    assert concept.canonicalization_version == "v2"
    assert canonicalize_concept_v1(value).key == "k_pop"


def test_alias_is_query_provenance_not_a_different_identity():
    canonical = canonicalize_concept("K-pop")
    alias = canonicalize_concept("Kpop")
    assert canonical.key == alias.key
    assert canonical.key.startswith("v2_") and len(canonical.key) == 51
    assert canonical_query_provenance("K-pop", canonical) == "exact_canonical"
    assert canonical_query_provenance("k-pop", canonical) == "exact_canonical"
    assert canonical_query_provenance("Kpop", alias) == "deterministic_alias"
    assert canonical_query_provenance("K pop", alias) == "deterministic_alias"


def test_explicit_list_splits_but_descriptive_phrase_does_not():
    assert split_explicit_preference_enumeration(
        "我喜歡 K-pop、J-pop、西洋音樂",
    ) == ["K-pop", "J-pop", "西洋音樂"]
    assert split_explicit_preference_enumeration(
        "我喜歡適合讀書的安靜咖啡廳",
    ) == []
    assert split_explicit_preference_enumeration(
        "我喜歡適合讀書、安靜又不吵的咖啡廳",
    ) == []
    assert split_explicit_preference_enumeration(
        "我喜歡 K-pop、不喜歡吵鬧的音樂",
    ) == []
    assert split_compound_concept_label(
        "K-pop、J-pop、西洋音樂",
    ) == ["K-pop", "J-pop", "西洋音樂"]


def test_memory_limit_is_configurable_but_hard_bounded():
    with patch.dict(os.environ, {"DURABLE_MEMORY_MAX_CANDIDATES_PER_MESSAGE": "7"}):
        assert durable_memory_limit() == 7
    with patch.dict(os.environ, {"DURABLE_MEMORY_MAX_CANDIDATES_PER_MESSAGE": "100"}):
        assert durable_memory_limit() == 8


def test_mixed_preference_polarity_is_detected_without_flagging_one_stance():
    assert has_mixed_preference_polarity("我喜歡 K-pop，但不喜歡吵鬧的音樂")
    assert has_mixed_preference_polarity("我偏好爵士，也避免重金屬")
    assert not has_mixed_preference_polarity("我不太喜歡吵鬧的酒吧")
    assert not has_mixed_preference_polarity("我喜歡不太吵的咖啡廳")


def test_semantic_synonyms_are_not_silently_merged_in_p0():
    assert canonicalize_concept("韓國流行音樂").key != canonicalize_concept(
        "韓流音樂",
    ).key
