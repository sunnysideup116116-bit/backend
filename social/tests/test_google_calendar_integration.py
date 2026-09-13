from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import mongomock
import pytest
from cryptography.fernet import Fernet
from fastapi import Response as FastApiResponse

from routers import google_calendar as google_calendar_router

from services.appwrite_identity_service import (
    AppwriteIdentityError,
    AppwriteIdentitySettings,
    authenticate_owner,
)
from services.google_calendar_service import (
    CANONICAL_REDIRECT_URI,
    GOOGLE_CALENDAR_SCOPE,
    GoogleCalendarError,
    GoogleCalendarService,
    GoogleCalendarSettings,
)


NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


class Response:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class Http:
    def __init__(self, *, posts=None, gets=None):
        self.posts = list(posts or [])
        self.gets = list(gets or [])
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.posts.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.gets.pop(0)


def settings(*, enabled: bool = True) -> GoogleCalendarSettings:
    return GoogleCalendarSettings(
        enabled=enabled,
        client_id="client-id",
        client_secret="client-secret",
        token_key=Fernet.generate_key().decode("ascii"),
        redirect_uri=CANONICAL_REDIRECT_URI,
        allowed_user_ids=frozenset({"owner"}),
    )


def service(*, http=None, configured=None):
    db = mongomock.MongoClient().db
    return GoogleCalendarService(
        settings=configured or settings(),
        connections=db.google_calendar_connections,
        oauth_states=db.google_calendar_oauth_states,
        http_session=http or Http(),
        now=lambda: NOW,
    )


def authorize(service: GoogleCalendarService) -> str:
    result = service.start_authorization("owner")
    query = parse_qs(urlsplit(result["authorization_url"]).query)
    assert query["scope"] == [GOOGLE_CALENDAR_SCOPE]
    assert query["access_type"] == ["offline"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["include_granted_scopes"] == ["false"]
    assert query["prompt"] == ["consent"]
    assert query["redirect_uri"] == [CANONICAL_REDIRECT_URI]
    return query["state"][0]


def test_appwrite_bearer_is_verified_against_account_endpoint():
    http = Http(gets=[Response(200, {"$id": "owner"})])
    owner = authenticate_owner(
        "Bearer jwt-value",
        settings=AppwriteIdentitySettings(
            endpoint="https://appwrite.example/v1",
            project_id="project",
            verify_tls=True,
        ),
        http_session=http,
    )
    assert owner == "owner"
    headers = http.calls[0][2]["headers"]
    assert headers["X-Appwrite-JWT"] == "jwt-value"
    assert "X-Appwrite-Key" not in headers


def test_appwrite_bearer_rejects_missing_or_failed_credentials():
    with pytest.raises(AppwriteIdentityError, match="appwrite_jwt_required"):
        authenticate_owner("")
    with pytest.raises(AppwriteIdentityError) as exc:
        authenticate_owner(
            "Bearer invalid",
            settings=AppwriteIdentitySettings(
                endpoint="https://appwrite.example/v1",
                project_id="project",
                verify_tls=True,
            ),
            http_session=Http(gets=[Response(401, {})]),
        )
    assert exc.value.status_code == 401


def test_authenticated_status_is_explicitly_non_cacheable(monkeypatch):
    integration = service(configured=settings(enabled=False))
    monkeypatch.setattr(
        google_calendar_router,
        "get_google_calendar_service",
        lambda: integration,
    )
    response = FastApiResponse()

    payload = google_calendar_router.connection_status(
        response=response,
        owner_id="owner",
    )

    assert payload["state"] == "unavailable"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_oauth_state_is_single_use_and_tokens_are_encrypted():
    http = Http(posts=[Response(200, {
        "access_token": "plain-access",
        "refresh_token": "plain-refresh",
        "expires_in": 3600,
        "scope": GOOGLE_CALENDAR_SCOPE,
    })])
    integration = service(http=http)
    state = authorize(integration)
    pending = integration.oauth_states.find_one({})
    assert pending["state_digest"] != state
    assert state not in repr(pending)
    assert "code_verifier" in pending

    assert integration.complete_authorization(state=state, code="code") == "connected"
    stored = integration.connections.find_one({"owner_id": "owner"})
    assert stored["state"] == "connected"
    assert stored["access_token"] != "plain-access"
    assert stored["refresh_token"] != "plain-refresh"
    assert "plain-access" not in repr(stored)
    with pytest.raises(GoogleCalendarError) as exc:
        integration.complete_authorization(state=state, code="code")
    assert exc.value.code == "oauth_state_invalid_or_expired"


def test_read_only_event_projection_handles_timed_and_all_day_events():
    http = Http(
        posts=[Response(200, {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 3600,
            "scope": GOOGLE_CALENDAR_SCOPE,
        })],
        gets=[Response(200, {
            "timeZone": "Asia/Taipei",
            "items": [
                {
                    "id": "timed",
                    "status": "confirmed",
                    "summary": "  晚餐   約會  ",
                    "location": "  台北 101\n信義路  ",
                    "start": {"dateTime": "2026-09-14T19:00:00+08:00"},
                    "end": {"dateTime": "2026-09-14T20:00:00+08:00"},
                },
                {
                    "id": "all-day",
                    "status": "tentative",
                    "summary": "旅行",
                    "start": {"date": "2026-09-15"},
                    "end": {"date": "2026-09-17"},
                },
            ],
        })],
    )
    integration = service(http=http)
    state = authorize(integration)
    integration.complete_authorization(state=state, code="code")

    result = integration.list_events("owner", NOW, NOW + timedelta(days=7))
    assert len(result["events"]) == 2
    timed, all_day = result["events"]
    assert timed["title"] == "晚餐 約會"
    assert timed["source_type"] == "google"
    assert timed["revision"] == 0
    assert timed["participants"] == []
    assert timed["location"] == "台北 101 信義路"
    assert all_day["all_day"] is True
    assert all_day["status"] == "tentative"
    assert all_day["start_at"] == "2026-09-15"
    assert all_day["end_at"] == "2026-09-17"
    assert result["truncated"] is False
    calendar_calls = [call for call in http.calls if "/calendar/v3/" in call[1]]
    assert len(calendar_calls) == 1
    assert calendar_calls[0][0] == "GET"
    assert calendar_calls[0][1].endswith("/calendars/primary/events")
    assert "location" in calendar_calls[0][2]["params"]["fields"]
    stored = integration.connections.find_one({"owner_id": "owner"})
    assert "晚餐 約會" not in repr(stored)
    assert "台北 101" not in repr(stored)
    assert "旅行" not in repr(stored)


def test_redirect_uri_must_match_the_public_callback_exactly(monkeypatch):
    monkeypatch.setenv("AYUE_GOOGLE_CALENDAR_ENABLED", "on")
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "client")
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_CALENDAR_TOKEN_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv(
        "GOOGLE_CALENDAR_REDIRECT_URI",
        f"{CANONICAL_REDIRECT_URI}?unexpected=1",
    )

    configured = GoogleCalendarSettings.from_env()

    assert configured.redirect_uri == ""
    assert configured.configured is False


def test_oauth_rejects_a_token_with_any_broader_scope():
    http = Http(posts=[Response(200, {
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_in": 3600,
        "scope": f"{GOOGLE_CALENDAR_SCOPE} https://www.googleapis.com/auth/calendar.events",
    })])
    integration = service(http=http)
    state = authorize(integration)

    with pytest.raises(GoogleCalendarError) as exc:
        integration.complete_authorization(state=state, code="code")

    assert exc.value.code == "google_calendar_scope_not_granted"
    assert integration.connections.count_documents({}) == 0


def test_invalid_grant_removes_tokens_and_requires_reauthorization():
    integration = service(http=Http(posts=[Response(400, {"error": "invalid_grant"})]))
    cipher = Fernet(integration.settings.token_key.encode("ascii"))
    integration.connections.insert_one({
        "owner_id": "owner",
        "state": "connected",
        "granted_scopes": [GOOGLE_CALENDAR_SCOPE],
        "refresh_token": cipher.encrypt(b"refresh").decode("ascii"),
        "access_token": cipher.encrypt(b"access").decode("ascii"),
        "access_token_expires_at": NOW - timedelta(minutes=1),
    })

    with pytest.raises(GoogleCalendarError) as exc:
        integration.list_events("owner", NOW, NOW + timedelta(days=1))
    assert exc.value.code == "google_calendar_reauth_required"
    stored = integration.connections.find_one({"owner_id": "owner"})
    assert stored["state"] == "reauth_required"
    assert "refresh_token" not in stored
    assert "access_token" not in stored


def test_unreadable_encrypted_credentials_require_reauthorization():
    integration = service()
    integration.connections.insert_one({
        "owner_id": "owner",
        "state": "connected",
        "granted_scopes": [GOOGLE_CALENDAR_SCOPE],
        "refresh_token": "not-a-fernet-token",
        "access_token_expires_at": NOW - timedelta(minutes=1),
    })

    with pytest.raises(GoogleCalendarError) as exc:
        integration.list_events("owner", NOW, NOW + timedelta(days=1))

    assert exc.value.code == "google_calendar_reauth_required"
    assert integration.status("owner")["state"] == "reauth_required"


def test_disconnect_deletes_credentials_even_when_revocation_is_unconfirmed():
    integration = service(http=Http(posts=[Response(503, {})]))
    cipher = Fernet(integration.settings.token_key.encode("ascii"))
    integration.connections.insert_one({
        "owner_id": "owner",
        "state": "connected",
        "granted_scopes": [GOOGLE_CALENDAR_SCOPE],
        "refresh_token": cipher.encrypt(b"refresh").decode("ascii"),
    })
    integration.oauth_states.insert_one({"owner_id": "owner", "state_digest": "pending"})

    result = integration.disconnect("owner")
    assert result == {
        "state": "disconnected",
        "connected": False,
        "revocation_confirmed": False,
    }
    assert integration.connections.count_documents({}) == 0
    assert integration.oauth_states.count_documents({}) == 0
