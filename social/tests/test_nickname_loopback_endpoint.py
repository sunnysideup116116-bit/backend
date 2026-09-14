from unittest.mock import MagicMock
from collections import OrderedDict

import pytest

from services import public_nickname_service as names


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1/v1", "http://127.0.0.1:80/v1", "http://localhost:80/v1", "http://[::1]/v1"])
def test_known_loopback_redirect_uses_fixed_https_without_redirect_or_insecure_tls(monkeypatch, endpoint):
    monkeypatch.setattr(names, "_ENDPOINT", endpoint)
    monkeypatch.setattr(names, "_PROJECT_ID", "test-project")
    monkeypatch.setattr(names, "_API_KEY", "test-key")
    monkeypatch.setattr(names, "_cache", OrderedDict())
    get = MagicMock(return_value=MagicMock(status_code=200, json=lambda: {"name": "kkk"}))
    monkeypatch.setattr(names.requests, "get", get)
    assert names.proposal_display_name("owner", fallback_lookup=lambda _: "") == "kkk"
    assert get.call_args.args[0].startswith("https://appwrite.misproject.us.ci/v1/")
    assert get.call_args.kwargs["allow_redirects"] is False
    assert get.call_args.kwargs.get("verify", True) is True


@pytest.mark.parametrize("endpoint", ["http://localhost:8080/v1", "https://appwrite.example.test/v1"])
def test_other_configured_endpoints_are_not_replaced(monkeypatch, endpoint):
    monkeypatch.setattr(names, "_ENDPOINT", endpoint)
    assert names._nickname_endpoint() == endpoint


@pytest.mark.parametrize("endpoint", ["http://user:password@127.0.0.1/v1", "http://127.0.0.1/v1?host=evil.test", "http://127.0.0.1/v1#fragment"])
def test_unsafe_loopback_does_not_become_a_valid_endpoint(monkeypatch, endpoint):
    monkeypatch.setattr(names, "_ENDPOINT", endpoint)
    assert names._nickname_endpoint() == endpoint
