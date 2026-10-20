"""Synthetic Appwrite/identity eligibility contract for Event V2."""
from unittest.mock import Mock

import pytest

from services.semantic_user_eligibility import (
    EnabledUserChecks,
    SemanticEligibilityUnavailable,
    lookup_enabled_account,
)


def appwrite_store(monkeypatch):
    store = Mock(endpoint="https://appwrite.invalid/v1", headers={}, verify=True)
    monkeypatch.setattr("agent_quota.service.AppwriteStore", lambda: store)
    return store


@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (404, {}, False),
        (200, {"$id": "synthetic-owner", "status": True}, True),
    ],
)
def test_appwrite_404_is_an_explicit_ineligible_result(monkeypatch, status_code, payload, expected):
    appwrite_store(monkeypatch)
    http = Mock()
    http.get.return_value = Mock(status_code=status_code, json=Mock(return_value=payload))

    result = lookup_enabled_account(
        "synthetic-owner", deadline=5, clock=lambda: 0, http=http,
    )

    assert result is expected
    http.get.assert_called_once()


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_appwrite_transient_http_failure_is_unavailable(monkeypatch, status_code):
    appwrite_store(monkeypatch)
    http = Mock()
    http.get.return_value = Mock(status_code=status_code)

    with pytest.raises(SemanticEligibilityUnavailable):
        lookup_enabled_account("synthetic-owner", deadline=5, clock=lambda: 0, http=http)


def test_enabled_user_checks_distinguishes_404_from_identity_transport_failure():
    session = Mock()
    profiles = Mock()
    rejected = EnabledUserChecks(
        session, deadline=5, profiles=profiles,
        account_lookup=lambda *_args, **_kwargs: False, clock=lambda: 0,
    )
    assert rejected.check("synthetic-owner") is False
    assert rejected.unavailable is False
    session.run.assert_not_called()

    def unavailable(*_args, **_kwargs):
        raise SemanticEligibilityUnavailable("semantic_eligibility_unavailable")

    failed = EnabledUserChecks(
        session, deadline=5, profiles=profiles,
        account_lookup=unavailable, clock=lambda: 0,
    )
    assert failed.check("synthetic-owner") is False
    assert failed.unavailable is True
    session.run.assert_not_called()
