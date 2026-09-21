from scripts.audit_canonical_preferences import proposal_for_row


def test_audit_proposes_only_clear_compounds_and_aliases():
    compound = proposal_for_row({
        "key": "old-bundle", "label": "K-pop、J-pop、西洋音樂",
        "users": 2, "edges": 3,
    })
    assert [item["key"] for item in compound["proposed_atomic_concepts"]] == [
        "k_pop", "j_pop", compound["proposed_atomic_concepts"][2]["key"],
    ]
    assert compound["affected_user_count"] == 2
    assert compound["affected_edges"] == 3
    assert compound["auto_proposable"]

    alias = proposal_for_row({"key": "kpop", "label": "Kpop", "users": 1, "edges": 1})
    assert alias["alias_normalization"] == {"key": "k_pop", "label": "K-pop"}

    ambiguous = proposal_for_row({
        "key": "phrase", "label": "適合讀書、安靜又不吵的咖啡廳",
        "users": 1, "edges": 1,
    })
    assert ambiguous["proposed_atomic_concepts"] == []
    assert not ambiguous["auto_proposable"]
