"""Owner authentication for APIs that handle external account credentials."""

from __future__ import annotations

import ipaddress
import hmac
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests


_DEFAULT_ENDPOINT = "https://appwrite.misproject.us.ci/v1"


class AppwriteIdentityError(RuntimeError):
    def __init__(self, code: str, status_code: int):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class AppwriteIdentitySettings:
    endpoint: str
    project_id: str
    verify_tls: bool

    @classmethod
    def from_env(cls) -> "AppwriteIdentitySettings":
        raw = (
            os.getenv("APPWRITE_INTERNAL_ENDPOINT")
            or os.getenv("APPWRITE_ENDPOINT")
            or _DEFAULT_ENDPOINT
        )
        endpoint, verify_tls = _validated_endpoint(raw)
        project_id = str(os.getenv("APPWRITE_PROJECT_ID") or "").strip()
        if not project_id:
            raise AppwriteIdentityError("appwrite_identity_unconfigured", 503)
        return cls(endpoint=endpoint, project_id=project_id, verify_tls=verify_tls)


def _validated_endpoint(value: str) -> tuple[str, bool]:
    endpoint = str(value or "").strip().rstrip("/")
    parsed = urlsplit(endpoint)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise AppwriteIdentityError("appwrite_identity_endpoint_invalid", 503)
    internal = host in {"localhost", "::1"}
    try:
        address = ipaddress.ip_address(host)
        internal = internal or address.is_loopback or address.is_private
    except ValueError:
        internal = internal or "." not in host
    if parsed.scheme == "http" and not internal:
        raise AppwriteIdentityError("appwrite_identity_https_required", 503)
    if parsed.path.rstrip("/") != "/v1":
        raise AppwriteIdentityError("appwrite_identity_endpoint_must_end_v1", 503)
    verify_tls = parsed.scheme == "https" and host not in {"localhost", "127.0.0.1", "::1"}
    return endpoint, verify_tls


def bearer_token(authorization: str | None) -> str:
    value = str(authorization or "").strip()
    scheme, separator, token = value.partition(" ")
    token = token.strip()
    if separator != " " or scheme.lower() != "bearer" or not token or len(token) > 4096:
        raise AppwriteIdentityError("appwrite_jwt_required", 401)
    return token


def authenticate_owner(
    authorization: str | None,
    *,
    http_session: requests.Session | None = None,
    settings: AppwriteIdentitySettings | None = None,
) -> str:
    token = bearer_token(authorization)
    configured = settings or AppwriteIdentitySettings.from_env()
    client = http_session or requests.Session()
    try:
        response = client.get(
            f"{configured.endpoint}/account",
            headers={
                "X-Appwrite-Project": configured.project_id,
                "X-Appwrite-JWT": token,
                "Accept": "application/json",
            },
            timeout=(2, 5),
            verify=configured.verify_tls,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise AppwriteIdentityError("appwrite_identity_unavailable", 503) from exc
    if 300 <= response.status_code < 400:
        raise AppwriteIdentityError("appwrite_identity_redirect_rejected", 503)
    if response.status_code in {401, 403}:
        raise AppwriteIdentityError("appwrite_authentication_failed", 401)
    if response.status_code < 200 or response.status_code >= 300:
        raise AppwriteIdentityError("appwrite_identity_unavailable", 503)
    try:
        payload = response.json()
    except ValueError as exc:
        raise AppwriteIdentityError("appwrite_identity_invalid_response", 503) from exc
    owner_id = str(payload.get("$id") or "").strip() if isinstance(payload, dict) else ""
    if not owner_id:
        raise AppwriteIdentityError("appwrite_identity_invalid_response", 503)
    return owner_id


def authenticated_owner_matches(
    authorization: str | None,
    claimed_owner_id: str,
) -> bool:
    """Best-effort capability gate for optional sensitive integrations.

    Existing chat endpoints retain their legacy behavior when no JWT is
    supplied, but external Google data is enabled only for a verified owner.
    """
    claimed = str(claimed_owner_id or "").strip()
    if not claimed:
        return False
    try:
        verified = authenticate_owner(authorization)
    except Exception:
        return False
    return hmac.compare_digest(verified, claimed)
