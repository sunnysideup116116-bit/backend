from app.core.appwrite_config import (
    DEFAULT_APPWRITE_ENDPOINT,
    _normalize_appwrite_endpoint,
    get_appwrite_config,
)


def test_local_port_80_redirect_is_replaced_with_canonical_https(monkeypatch):
    monkeypatch.setenv("APPWRITE_ENDPOINT", "http://127.0.0.1:80/v1")

    assert get_appwrite_config().endpoint == "https://127.0.0.1/v1"


def test_localhost_port_80_is_normalized_but_other_dev_ports_are_preserved():
    assert _normalize_appwrite_endpoint("http://localhost:80/v1") == (
        "https://localhost/v1"
    )
    assert _normalize_appwrite_endpoint("http://localhost:8080/v1") == (
        "http://localhost:8080/v1"
    )


def test_public_https_endpoint_is_unchanged(monkeypatch):
    monkeypatch.delenv("APPWRITE_ENDPOINT", raising=False)

    assert _normalize_appwrite_endpoint(DEFAULT_APPWRITE_ENDPOINT) == (
        DEFAULT_APPWRITE_ENDPOINT
    )
