from services.preference_store import _clean_item


def test_mongo_preference_fact_uses_the_same_server_canonical_identity():
    item = _clean_item({
        "key": "model_specific_key", "label": "Kpop",
        "stance": "like", "category": "activity", "confidence": .95,
    })
    assert item["concept_key"] == "k_pop"
    assert item["label"] == "K-pop"


def test_mongo_projection_rejects_a_non_atomic_compound_fact():
    assert _clean_item({
        "key": "bundle", "label": "K-pop、J-pop、西洋音樂",
        "stance": "like", "category": "activity", "confidence": .95,
    }) is None
