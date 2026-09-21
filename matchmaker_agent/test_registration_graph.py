from unittest.mock import MagicMock

import pytest

from concept_identity import canonicalize_concept
from registration_graph import RegistrationProjection, project_identity, seed_registration, registration_message_id


def tx(allowed=True):
    transaction = MagicMock()
    transaction.run.return_value.single.return_value = {"seed_allowed": allowed}
    return transaction


def memory(**overrides):
    return {"key": "reading", "label": "閱讀", "stance": "like", "category": "activity",
            "confidence": .99, "evidence_span": "閱讀", **overrides}


def test_identity_keeps_account_id_and_uses_revisioned_name():
    transaction = tx()
    result = project_identity(transaction, RegistrationProjection(user_id="account1", name="小宇", observed_at=12))
    query = transaction.run.call_args.args[0]
    assert "MERGE (u:User {id:$user_id})" in query
    assert "u.name=$name" in query and "name_updated_at" in query
    assert "MEMORY_DISABLED" in query
    assert result["registration_seed_allowed"]


def test_seed_has_atomic_marker_provenance_and_event_eligible_kind():
    transaction = tx()
    result = seed_registration(transaction, "account1", registration_message_id("account1"), [memory()])
    query = transaction.run.call_args.args[0]
    assert result[0]["category"] == "interest"
    assert "registration_seed_finished_at" in query and "MemoryObservation" in query
    assert "r.source='registration_interest'" in query
    assert "c.kind='interest'" in query
    assert "DELETE" not in query


@pytest.mark.parametrize("label", ["喜歡爬山", "偏好：爬山", "興趣:爬山"])
def test_seed_uses_plain_concept_label(label):
    transaction = tx()
    result = seed_registration(transaction, "account1", registration_message_id("account1"), [memory(label=label)])
    assert result[0]["label"] == "爬山"


def test_registration_seed_splits_clear_interests_into_atomic_concepts():
    transaction = tx()
    result = seed_registration(
        transaction, "account1", registration_message_id("account1"),
        [memory(label="K-pop、J-pop、西洋音樂", evidence_span="K-pop、J-pop、西洋音樂")],
    )
    assert [(item["key"], item["label"]) for item in result] == [
        ("k_pop", "K-pop"),
        ("j_pop", "J-pop"),
        (canonicalize_concept("西洋音樂").key, "西洋音樂"),
    ]
    assert len({item["key"] for item in result}) == 3


def test_existing_or_disabled_memory_blocks_all_bootstrap_edges():
    transaction = tx(False)
    assert seed_registration(transaction, "account1", registration_message_id("account1"), [memory()]) == []
    transaction.run.assert_called_once()


def test_wrong_owner_observation_is_rejected_before_write():
    transaction = tx()
    with pytest.raises(ValueError):
        seed_registration(transaction, "account1", registration_message_id("other"), [memory()])
    transaction.run.assert_not_called()


@pytest.mark.parametrize("overrides", [{"stance": "avoid"}, {"confidence": .8},
    {"confidence": float("nan")}, {"evidence_span": ""}, {"category": "personality"}, {"label": "宗教"}])
def test_invalid_registration_preferences_never_write(overrides):
    transaction = tx()
    assert seed_registration(transaction, "account1", registration_message_id("account1"), [memory(**overrides)]) == []
    transaction.run.assert_called_once()
