"""Synthetic owner-bound legacy compound bridge, never general atomic bypass."""
from copy import deepcopy
from unittest.mock import Mock, patch

import pytest

from matchmaker_agent import preference_bootstrap as core
from matchmaker_agent import preference_bootstrap_contract as contract
from matchmaker_agent.concept_identity import canonicalize_concept, stored_concept_identity
from services.memory_service import validate_memory_proposals, MemoryWriteError

TEXT = "Jazz、Mystery Novels"
OWNER = "synthetic-owner"


def snapshot(text=TEXT, relation="PREFERS"):
    return {"owner": OWNER, "revision": 3, "epoch": 0, "epoch_at": 0, "rows": [
        {"id": "synthetic-edge-1", "relation": relation, "properties": {"confidence": .8},
         "concept": {"key": "legacy_compound", "label": text}}]}


def normalize(snap=None, *, mode="complete_set", prefers=None, avoids=None, owner=OWNER):
    return contract.normalize_items([TEXT] if prefers is None else prefers,
        [] if avoids is None else avoids, mode=mode, legacy_snapshot=snap, owner=owner)


def plan(snap, items, mode="complete_set"):
    tx = Mock()
    with patch.object(core, "snapshot", return_value=(snap, None)), patch.object(core, "concepts_for", return_value={}):
        result = core.build_plan(tx, OWNER, mode, items)
    tx.run.assert_not_called()
    return result[0]


@pytest.mark.parametrize("relation", ["PREFERS", "AVOIDS"])
def test_complete_set_preserves_one_lossless_identity_and_one_retirement(relation):
    snap = snapshot(relation=relation)
    result = normalize(snap, prefers=[TEXT] if relation == "PREFERS" else [], avoids=[TEXT] if relation == "AVOIDS" else [])
    assert len(result) == 1
    item = result[0]
    assert item["semantic_text"] == TEXT and item["relation"] == relation
    assert item["canonicalization_version"] == "v2" and stored_concept_identity(item)
    assert item["key"] == canonicalize_concept(TEXT).key
    assert item["key"] not in {canonicalize_concept("Jazz").key, canonicalize_concept("Mystery Novels").key}
    source = item["legacy_compound_source"]
    assert source["relationship_id"] == snap["rows"][0]["id"]
    assert source["snapshot_hash"] == contract.digest(snap)
    actual = plan(snap, result)
    assert len(actual["create_concepts"]) == len(actual["create_edges"]) == len(actual["retire_edges"]) == 1
    assert actual["retire_edges"][0]["id"] == source["relationship_id"]


@pytest.mark.parametrize("change", ["missing", "other_owner", "other_text", "wrong_polarity", "inactive",
    "duplicate", "disabled", "v2", "forged_v2_key"])
def test_fresh_same_owner_active_unambiguous_legacy_source_required(change):
    snap = snapshot()
    if change == "missing": snap = None
    elif change == "other_owner": snap["owner"] = "other"
    elif change == "other_text": snap["rows"][0]["concept"]["label"] = "Jazz、Board Games"
    elif change == "wrong_polarity": snap["rows"][0]["relation"] = "AVOIDS"
    elif change == "inactive": snap["rows"][0]["properties"]["active"] = False
    elif change == "duplicate": snap["rows"].append({**deepcopy(snap["rows"][0]), "id": "another-edge"})
    elif change == "disabled": snap["rows"][0]["relation"] = "MEMORY_DISABLED"
    elif change == "v2": snap["rows"][0]["concept"] = canonicalize_concept(TEXT).as_dict()
    elif change == "forged_v2_key": snap["rows"][0]["concept"]["key"] = canonicalize_concept(TEXT).key
    with pytest.raises(contract.BootstrapError, match="atomic_item_required"):
        normalize(snap)


def test_add_only_and_no_snapshot_keep_atomic_rule():
    with pytest.raises(contract.BootstrapError, match="atomic_item_required"):
        normalize(snapshot(), mode="add_only")
    with pytest.raises(contract.BootstrapError, match="atomic_item_required"):
        contract.normalize_items([TEXT], [], mode="complete_set")


def test_literal_enumeration_is_one_item_not_an_ordinary_cap_increase():
    text = "、".join(f"Synthetic {i}" for i in range(9))
    assert len(normalize(snapshot(text), prefers=[text])) == 1
    with pytest.raises(contract.BootstrapError, match="atomic_item_required"):
        normalize(snapshot(text), prefers=[text], mode="add_only")


def test_client_cannot_supply_the_source_snapshot_or_exemption():
    from pydantic import ValidationError
    from social.routers.preference_bootstrap import PreviewInput
    from matchmaker_agent.preference_bootstrap_api import PreviewRequest
    for cls, extra in [(PreviewInput, {}), (PreviewRequest, {"owner":OWNER,"mongo_snapshot_hash":"0"*64})]:
        with pytest.raises(ValidationError):
            cls(mode="complete_set",prefers=[TEXT],avoids=[],legacy_snapshot=snapshot(),**extra)


@pytest.mark.parametrize("persisted", [False, True])
def test_ordinary_memory_cannot_borrow_bootstrap_source(persisted):
    item = normalize(snapshot())[0] if persisted else {"label": TEXT}
    with pytest.raises(MemoryWriteError, match="invalid_atomic_preference"):
        validate_memory_proposals([{**item, "stance": "like"}])


@pytest.mark.parametrize("text", ["Jazz，Mystery Novels", " Jazz、Mystery Novels ", "喜歡Jazz、Mystery Novels"])
def test_no_lossy_normalization_to_admit_legacy_compound(text):
    with pytest.raises(contract.BootstrapError, match="legacy_compound_text_not_lossless"):
        normalize(snapshot(text), prefers=[text])


@pytest.mark.parametrize("change", ["locator", "hash", "owner", "polarity", "missing_binding", "row_properties", "retirement_source"])
def test_build_plan_rechecks_proof_and_retirement_not_just_identity(change):
    snap = snapshot(); items = normalize(snap)
    if change == "locator": items[0]["legacy_compound_source"]["relationship_id"] = "wrong"
    elif change == "hash": items[0]["legacy_compound_source"]["snapshot_hash"] = "0"*64
    elif change == "owner": items[0]["legacy_compound_source"]["owner"] = "other"
    elif change == "polarity": items[0]["relation"] = "AVOIDS"
    elif change == "missing_binding": items[0].pop("legacy_compound_source")
    elif change == "row_properties": snap["rows"][0]["properties"]["confidence"] = .9
    elif change == "retirement_source": snap["rows"][0]["id"] = "replacement-edge"
    with pytest.raises(contract.BootstrapError):
        plan(snap, items)


def test_atomic_parity_and_no_unnecessary_legacy_proof():
    expected = contract.normalize_items(["Kpop", "Reading"], ["Smoke"], mode="complete_set")
    actual = normalize(snapshot(), prefers=["Kpop", "Reading"], avoids=["Smoke"])
    assert actual == expected
    assert all("legacy_compound_source" not in row for row in actual)


def test_mixed_polarity_is_not_exempted():
    text = "喜歡登山但不喜歡游泳、慢跑"
    with pytest.raises(contract.BootstrapError, match="mixed_polarity"):
        normalize(snapshot(text), prefers=[text])


def test_preview_has_no_mutation_authority_without_complete_set_consent(monkeypatch):
    from test_runtime_contract import receipt
    monkeypatch.setenv("APPWRITE_API_KEY", "synthetic-unit-only")
    payload = {**receipt(), "owner": OWNER, "mode": "complete_set", "items": normalize(snapshot())}
    token = contract.seal_preview(payload)
    graph = core.BootstrapGraph(Mock())
    with pytest.raises(contract.BootstrapError, match="complete_set_consent_required"):
        graph.commit(OWNER, token, {"confirm_items": True})
    graph.driver.session.assert_not_called()
