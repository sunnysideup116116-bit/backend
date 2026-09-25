"""Hermetic bootstrap runtime contracts; no database/provider connections."""
from contextlib import nullcontext
import time
from unittest.mock import MagicMock, patch
import uuid

import pytest
from neo4j.exceptions import TransientError

from matchmaker_agent import preference_bootstrap_contract as contract
from matchmaker_agent.preference_bootstrap import BootstrapGraph, _consent
from matchmaker_agent.preference_write_fence import lock_preferences, PreferenceFenceError
from matchmaker_agent.concept_identity import canonicalize_fresh_concept, PreferenceTextError


@pytest.fixture(autouse=True)
def synthetic_configuration(monkeypatch):
    monkeypatch.setenv("APPWRITE_API_KEY", "synthetic-unit-only")
    monkeypatch.setenv("PREFERENCE_BOOTSTRAP_ENABLED", "on")


def receipt():
    return {"owner": "synthetic_owner", "policy": contract.POLICY,
            "normalization_policy": contract.FRESH_PREFERENCE_NORMALIZATION_POLICY,
            "preview_id": str(uuid.uuid4()), "expires_at": time.time() + 600}


def test_preview_signature_owner_expiry_and_no_secret():
    payload = receipt()
    token = contract.seal_preview(payload)
    assert contract.open_preview(token, "synthetic_owner") == payload
    assert "synthetic-unit-only" not in token
    for owner, value in [("other", token), ("synthetic_owner", token[:-1] + ("a" if token[-1] != "a" else "b"))]:
        with pytest.raises(contract.BootstrapError, match="invalid_preview"):
            contract.open_preview(value, owner)
    payload["expires_at"] = 0
    expired = contract.seal_preview(payload)
    with pytest.raises(contract.BootstrapError, match="preview_expired"):
        contract.open_preview(expired, "synthetic_owner")
    assert contract.open_preview(expired, "synthetic_owner", allow_expired=True) == payload


@pytest.mark.parametrize("change", ["owner", "path", "body", "expired", "missing"])
def test_internal_signature_rejects_modified_scope(change):
    path, body = "/api/v2/preferences/bootstrap/commit", {"owner": "synthetic_owner"}
    header = contract.internal_headers(path, body)["X-Preference-Bootstrap"]
    if change == "owner": body = {"owner": "other"}
    if change == "path": path += "-rollback"
    if change == "body": body = {**body, "consent": True}
    if change == "expired": header = contract.internal_headers(path, body, now=0)["X-Preference-Bootstrap"]
    if change == "missing": header = ""
    with pytest.raises(contract.BootstrapError, match="invalid_bootstrap_context"):
        contract.verify_internal(path, body, header)


def test_valid_internal_signature():
    path, body = "/api/v2/preferences/bootstrap/preview", {"owner": "synthetic_owner"}
    contract.verify_internal(path, body, contract.internal_headers(path, body)["X-Preference-Bootstrap"])


@pytest.mark.parametrize("size", [41, 499, 500])
def test_whole_input_boundary_preserves_unicode(size):
    text = "山" * (size - 1) + "🌳"
    item = contract.normalize_items([text], [])[0]
    assert item["semantic_text"] == text
    assert item["key"] == canonicalize_fresh_concept(text).key


def test_501_rejects_whole_batch_and_prefix_collision_does_not_exist():
    with pytest.raises(PreferenceTextError):
        contract.normalize_items(["Reading", "山" * 501], [])
    a, b = "山" * 40 + "安靜", "山" * 40 + "熱鬧"
    assert len({x["key"] for x in contract.normalize_items([a, b], [])}) == 2


@pytest.mark.parametrize("prefers,avoids", [(["Kpop", "K-pop"], []), (["Reading"], ["Reading"]),
    (["K-pop、J-pop"], []), (["喜歡爬山但不喜歡游泳"], []), ([], []), (["Reading"] * 6, []), (["Reading"], None)])
def test_atomic_both_polarity_whole_admission(prefers, avoids):
    with pytest.raises(contract.BootstrapError):
        contract.normalize_items(prefers, avoids)


@pytest.mark.parametrize("omitted", ["confirm_items", "complete_set", "reviewed_prefers", "reviewed_avoids", "retire_legacy"])
def test_complete_set_requires_every_explicit_consent(omitted):
    consent = dict.fromkeys(["confirm_items", "complete_set", "reviewed_prefers", "reviewed_avoids", "retire_legacy"], True)
    consent.pop(omitted)
    with pytest.raises(contract.BootstrapError):
        _consent("complete_set", consent)


def test_add_only_consent_and_mode_off_no_graph_call(monkeypatch):
    _consent("add_only", {"confirm_items": True})
    monkeypatch.delenv("PREFERENCE_BOOTSTRAP_ENABLED")
    driver = MagicMock()
    with pytest.raises(contract.BootstrapError, match="preference_bootstrap_disabled"):
        BootstrapGraph(driver).preview("synthetic_owner", "add_only", ["Reading"], [])
    driver.session.assert_not_called()


@pytest.mark.parametrize("source", [None, 0, 99, 100, float("nan"), float("inf"), True])
def test_source_epoch_fails_closed(source):
    tx = MagicMock()
    tx.run.return_value.single.return_value = {"revision": 3, "epoch": 1, "epoch_at": 100, "pending": None}
    with pytest.raises(PreferenceFenceError, match="preference_source_superseded"):
        lock_preferences(tx, "synthetic_owner", source_created_at=source)


def test_owner_pending_revision_and_fresh_source_fence():
    tx = MagicMock()
    tx.run.return_value.single.return_value = {"revision": 3, "epoch": 1, "epoch_at": 100, "pending": "operation"}
    with pytest.raises(PreferenceFenceError, match="preference_projection_pending"):
        lock_preferences(tx, "synthetic_owner", source_created_at=101)
    assert lock_preferences(tx, "synthetic_owner", bootstrap_operation="operation")["revision"] == 3
    with pytest.raises(PreferenceFenceError, match="stale_preview"):
        lock_preferences(tx, "synthetic_owner", expected_revision=2, bootstrap_operation="operation")
    tx.run.return_value.single.return_value["pending"] = None
    assert lock_preferences(tx, "synthetic_owner", source_created_at=101)["revision"] == 3


def test_deadlock_retry_once_shares_deadline_not_ambiguous_commit():
    deadlock = TransientError("synthetic aborted transaction")
    deadlock.code = "Neo.TransientError.Transaction.DeadlockDetected"
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    tx = session.begin_transaction.return_value.__enter__.return_value
    calls = MagicMock(side_effect=[deadlock, "ok"])
    with patch("matchmaker_agent.preference_bootstrap.time.monotonic", side_effect=[10, 10, 12]):
        assert BootstrapGraph(driver).write(calls) == "ok"
    assert [c.kwargs["timeout"] for c in session.begin_transaction.call_args_list] == [8, 6]
    assert calls.call_count == 2
    calls = MagicMock(side_effect=deadlock)
    with pytest.raises(TransientError): BootstrapGraph(driver).write(calls)
    assert calls.call_count == 2
    tx.commit.side_effect = OSError("synthetic transport lost after commit")
    calls = MagicMock(return_value="unknown")
    with pytest.raises(OSError): BootstrapGraph(driver).write(calls)
    assert calls.call_count == 1


def test_public_owner_only_schema_and_strict_consent():
    from pydantic import ValidationError
    from social.routers.preference_bootstrap import PreviewInput, CommitInput
    with pytest.raises(ValidationError): PreviewInput(mode="complete_set", prefers=["Reading"])
    with pytest.raises(ValidationError): PreviewInput(mode="add_only", prefers=["Reading"], avoids=[], owner="other")
    with pytest.raises(ValidationError): CommitInput(preview_token="receipt", consent={"confirm_items": "true"})
    assert PreviewInput(mode="complete_set", prefers=["Reading"], avoids=[]).avoids == []
