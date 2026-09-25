"""Mode-scoped bootstrap capacity. Synthetic, hermetic, no database I/O."""
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from matchmaker_agent import preference_bootstrap_contract as contract
from matchmaker_agent.preference_bootstrap_api import PreviewRequest
from matchmaker_agent.concept_identity import durable_memory_limit
from social.routers.preference_bootstrap import PreviewInput
from services.memory_service import MemoryWriteError, validate_memory_proposals


def items(count, prefix="Synthetic paper craft"):
    return [f"{prefix} {i}" for i in range(count)]


@pytest.fixture(autouse=True)
def fixed_normal_memory_limit(monkeypatch):
    monkeypatch.delenv("DURABLE_MEMORY_MAX_CANDIDATES_PER_MESSAGE", raising=False)


@pytest.mark.parametrize("prefers,avoids", [(5, 0), (5, 3), (6, 1), (7, 3), (10, 0), (0, 10)])
def test_complete_set_accepts_whole_combined_batch(prefers, avoids):
    positive, negative = items(prefers), items(avoids, "Synthetic loud venue")
    result = contract.normalize_items(positive, negative, mode="complete_set")
    assert len(result) == prefers + avoids
    assert sum(row["relation"] == "PREFERS" for row in result) == prefers
    assert sum(row["relation"] == "AVOIDS" for row in result) == avoids
    assert {row["semantic_text"] for row in result} == set(positive + negative)
    assert all(row["canonicalization_version"] == "v2" for row in result)


@pytest.mark.parametrize("prefers,avoids", [(11, 0), (0, 11), (6, 5), (10, 1), (5, 6), (0, 0)])
def test_complete_set_overflow_rejects_before_normalizing_any_item(prefers, avoids):
    with patch.object(contract, "normalize_fresh_preference_text") as normalize:
        with pytest.raises(contract.BootstrapError, match="bootstrap_item_limit"):
            contract.normalize_items(items(prefers), items(avoids, "Synthetic avoid"), mode="complete_set")
        normalize.assert_not_called()


@pytest.mark.parametrize("schema", [PreviewInput, PreviewRequest])
@pytest.mark.parametrize("prefers,avoids", [(5, 0), (5, 3), (6, 1), (7, 3), (10, 0), (0, 10)])
def test_both_http_contracts_accept_complete_set_totals(schema, prefers, avoids):
    extra = {"owner": "synthetic", "mongo_snapshot_hash": "0" * 64} if schema is PreviewRequest else {}
    model = schema(mode="complete_set", prefers=items(prefers), avoids=items(avoids, "Synthetic avoid"), **extra)
    assert len(model.prefers) + len(model.avoids) == prefers + avoids


@pytest.mark.parametrize("schema", [PreviewInput, PreviewRequest])
@pytest.mark.parametrize("mode,prefers,avoids", [
    ("complete_set", 11, 0), ("complete_set", 6, 5), ("complete_set", 0, 11),
    ("add_only", 6, 0), ("add_only", 5, 1), ("add_only", 0, 6),
])
def test_both_http_contracts_fail_closed_on_mode_specific_overflow(schema, mode, prefers, avoids):
    extra = {"owner": "synthetic", "mongo_snapshot_hash": "0" * 64} if schema is PreviewRequest else {}
    with pytest.raises(ValidationError):
        schema(mode=mode, prefers=items(prefers), avoids=items(avoids, "Synthetic avoid"), **extra)


def test_default_call_and_add_only_keep_previous_five_item_limit():
    assert len(contract.normalize_items(items(5), [])) == 5
    assert len(contract.normalize_items(items(5), [], mode="add_only")) == 5
    for kwargs in ({}, {"mode": "add_only"}):
        with pytest.raises(contract.BootstrapError, match="bootstrap_item_limit"):
            contract.normalize_items(items(6), [], **kwargs)


@pytest.mark.parametrize("limit", [1, 3, 5, 6, 8])
def test_complete_set_does_not_raise_ordinary_memory_or_add_only_cap(monkeypatch, limit):
    monkeypatch.setenv("DURABLE_MEMORY_MAX_CANDIDATES_PER_MESSAGE", str(limit))
    assert durable_memory_limit() == limit
    assert len(contract.normalize_items(items(10), [], mode="complete_set")) == 10
    with pytest.raises(contract.BootstrapError, match="bootstrap_item_limit"):
        contract.normalize_items(items(min(5, limit) + 1), [], mode="add_only")
    memories = [{"label": text, "stance": "like"} for text in items(limit + 1)]
    with pytest.raises(MemoryWriteError, match="too_many_preferences"):
        validate_memory_proposals(memories)
    assert len(validate_memory_proposals(memories[:limit])) == limit
    assert durable_memory_limit() == limit


@pytest.mark.parametrize("prefers,avoids", [(["Kpop", "K-pop"], []), (["Reading"], ["Reading"]),
    (["K-pop、J-pop"], []), (["喜歡爬山但不喜歡游泳"], [])])
def test_larger_complete_set_keeps_atomic_identity_and_polarity_rules(prefers, avoids):
    with pytest.raises(contract.BootstrapError):
        contract.normalize_items(prefers, avoids, mode="complete_set")


def test_unknown_mode_cannot_opt_into_larger_limit():
    with pytest.raises(contract.BootstrapError, match="invalid_bootstrap_mode"):
        contract.normalize_items(items(8), [], mode="other")
