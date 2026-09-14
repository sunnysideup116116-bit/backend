"""Appwrite profile reads are bounded, name-only, and never profile writes."""

import json
from unittest.mock import MagicMock

import pytest
import requests

from services import public_nickname_service as names


@pytest.fixture(autouse=True)
def appwrite_stub(monkeypatch):
    monkeypatch.setattr(names, "_ENDPOINT", "https://appwrite.example.test/v1")
    monkeypatch.setattr(names, "_PROJECT_ID", "test-project")
    monkeypatch.setattr(names, "_API_KEY", "test-only-key")
    names._cache.clear()
    get = MagicMock(return_value=MagicMock(status_code=200))
    get.return_value.json.return_value = {"name": "小晴"}
    monkeypatch.setattr(names.requests, "get", get)
    yield get
    names._cache.clear()


def test_registered_nickname_uses_only_the_public_name_and_not_mongo(appwrite_stub):
    fallback = MagicMock(return_value="舊暱稱")
    assert names.proposal_display_name("user-17", fallback_lookup=fallback) == "小晴"
    fallback.assert_not_called()
    args, kwargs = appwrite_stub.call_args
    assert args[0] == "https://appwrite.example.test/v1/databases/dating_db/collections/user_profiles/documents/user-17"
    assert json.loads(kwargs["params"]["queries[]"]) == {"method": "select", "values": ["name"]}
    assert kwargs["timeout"] == (1.0, 2.0)
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize("response_name", [None, "", "user-17", "hello@example.test", "小晴\n第二行", {"name": "小晴"}])
def test_empty_or_unsafe_authoritative_name_does_not_restore_an_old_alias(appwrite_stub, response_name):
    appwrite_stub.return_value.json.return_value = {"name": response_name}
    fallback = MagicMock(return_value="舊暱稱")
    assert names.proposal_display_name("user-17", fallback_lookup=fallback) == ""
    fallback.assert_not_called()


@pytest.mark.parametrize("status", [302, 401, 403, 404, 429, 500])
def test_failed_appwrite_read_can_use_a_safe_seed_name(appwrite_stub, status):
    appwrite_stub.return_value.status_code = status
    assert names.proposal_display_name("seed_user_04", fallback_lookup=lambda _: "小晴") == "小晴"
    assert names.proposal_display_name("seed_user_04", fallback_lookup=lambda uid: uid) == ""
    assert appwrite_stub.call_count == 1


@pytest.mark.parametrize("failure", [requests.Timeout(), requests.ConnectionError(), ValueError("invalid JSON")])
def test_network_or_json_failure_preserves_the_safe_fallback(appwrite_stub, failure):
    appwrite_stub.side_effect = failure
    assert names.proposal_display_name("seed_user_04", fallback_lookup=lambda _: "小晴") == "小晴"


@pytest.mark.parametrize("endpoint", [
    "http://appwrite.example.test/v1", "file:///tmp/names", "https://user:password@appwrite.example.test/v1",
    "https://appwrite.example.test/v1?extra=1", "https://appwrite.example.test/v1#fragment",
])
def test_unsafe_endpoint_does_not_receive_server_credentials(monkeypatch, appwrite_stub, endpoint):
    monkeypatch.setattr(names, "_ENDPOINT", endpoint)
    assert names.proposal_display_name("user-17", fallback_lookup=lambda _: "小晴") == "小晴"
    appwrite_stub.assert_not_called()


@pytest.mark.parametrize("user_id", ["../another-user", "user/another", "user?query", "a" * 37])
def test_profile_id_cannot_change_the_appwrite_request_path(appwrite_stub, user_id):
    assert names.proposal_display_name(user_id, fallback_lookup=lambda _: "小晴") == "小晴"
    appwrite_stub.assert_not_called()


def test_missing_configuration_never_attempts_a_network_read(monkeypatch, appwrite_stub):
    monkeypatch.setattr(names, "_API_KEY", "")
    assert names.proposal_display_name("seed_user_04", fallback_lookup=lambda _: "小晴") == "小晴"
    appwrite_stub.assert_not_called()


def test_legacy_seed_id_placeholder_uses_its_mongo_public_name(appwrite_stub):
    appwrite_stub.return_value.json.return_value = {"name": "seed_user_04"}
    assert names.proposal_display_name("seed_user_04", fallback_lookup=lambda _: "小晴") == "小晴"
    assert names.proposal_display_name("seed_user_04", fallback_lookup=lambda uid: uid) == ""


def test_cache_is_bounded_and_refreshes_changed_nicknames(monkeypatch, appwrite_stub):
    now = [100.0]
    monkeypatch.setattr(names.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(names, "_CACHE_LIMIT", 2)
    fallback = MagicMock()
    assert names.proposal_display_name("alice", fallback_lookup=fallback) == "小晴"
    appwrite_stub.return_value.json.return_value = {"name": "新稱呼"}
    assert names.proposal_display_name("alice", fallback_lookup=fallback) == "小晴"
    assert appwrite_stub.call_count == 1
    now[0] += names._CACHE_TTL_SECONDS + 1
    assert names.proposal_display_name("alice", fallback_lookup=fallback) == "新稱呼"
    names.proposal_display_name("bob", fallback_lookup=fallback)
    names.proposal_display_name("carol", fallback_lookup=fallback)
    assert list(names._cache) == ["bob", "carol"]
    fallback.assert_not_called()


def test_all_profile_sources_unavailable_returns_no_account_identifier(appwrite_stub):
    appwrite_stub.side_effect = requests.Timeout()
    assert names.proposal_display_name("user-17", fallback_lookup=MagicMock(side_effect=RuntimeError())) == ""


def test_shared_status_projection_does_not_read_or_publish_appwrite_names(monkeypatch, appwrite_stub):
    from routers import match as routes
    from tests.test_proposal_nickname import document

    monkeypatch.setattr(routes, "public_display_name", lambda _: "對方")
    result = routes.build_status_proposal_card(document(), "alice")
    assert "counterparty_nickname" not in result
    assert result["other_label"] == "對方"
    appwrite_stub.assert_not_called()


@pytest.mark.parametrize("viewer,expected", [("alice", "小晴"), ("bob", "小葵")])
def test_state_reads_registered_name_even_when_mongo_has_no_name(monkeypatch, appwrite_stub, viewer, expected):
    from routers import match as routes
    from tests.match_flow_store import Collection
    from tests.test_proposal_nickname import MATCH_ID, document

    appwrite_stub.return_value.json.return_value = {"name": expected}
    monkeypatch.setattr(routes, "matches_coll", Collection([document()]))
    monkeypatch.setattr(routes, "public_display_name", lambda _: "對方")
    result = routes.get_single_match_state(viewer, str(MATCH_ID))
    assert result["counterparty_nickname"] == expected
    assert "other_id" not in result
    assert expected not in result["viewer_reason"]
    assert expected not in result["decline_reason_options"]
    assert appwrite_stub.call_args.args[0].endswith("/bob" if viewer == "alice" else "/alice")
