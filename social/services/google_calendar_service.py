"""Server-owned, read-only Google Calendar OAuth and event projection."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from datetime import date, datetime, time as time_value, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from cryptography.fernet import Fernet, InvalidToken

from database import google_calendar_connections_coll, google_calendar_oauth_states_coll


GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events.owned.readonly"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
CANONICAL_REDIRECT_URI = (
    "https://service.misproject.us.ci/api/integrations/google-calendar/callback"
)
MAX_RANGE_DAYS = 92
MAX_EVENT_PAGES = 4
EVENTS_PER_PAGE = 250


class GoogleCalendarError(RuntimeError):
    def __init__(self, code: str, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class GoogleCalendarSettings:
    enabled: bool
    client_id: str
    client_secret: str
    token_key: str
    redirect_uri: str
    allowed_user_ids: frozenset[str]

    @classmethod
    def from_env(cls) -> "GoogleCalendarSettings":
        enabled = str(os.getenv("AYUE_GOOGLE_CALENDAR_ENABLED") or "off").strip().lower() in {
            "1", "true", "on", "yes",
        }
        redirect_uri = str(
            os.getenv("GOOGLE_CALENDAR_REDIRECT_URI") or CANONICAL_REDIRECT_URI
        ).strip()
        if redirect_uri != CANONICAL_REDIRECT_URI:
            redirect_uri = ""
        return cls(
            enabled=enabled,
            client_id=str(os.getenv("GOOGLE_CALENDAR_CLIENT_ID") or "").strip(),
            client_secret=str(os.getenv("GOOGLE_CALENDAR_CLIENT_SECRET") or "").strip(),
            token_key=str(os.getenv("GOOGLE_CALENDAR_TOKEN_KEY") or "").strip(),
            redirect_uri=redirect_uri,
            allowed_user_ids=frozenset(
                value.strip()
                for value in str(os.getenv("AYUE_GOOGLE_CALENDAR_ALLOWED_USER_IDS") or "").split(",")
                if value.strip()
            ),
        )

    @property
    def configured(self) -> bool:
        if not (
            self.enabled
            and self.client_id
            and self.client_secret
            and self.token_key
            and self.redirect_uri
        ):
            return False
        try:
            Fernet(self.token_key.encode("ascii"))
        except (ValueError, TypeError, UnicodeEncodeError):
            return False
        return True

    def allows(self, owner_id: str) -> bool:
        return not self.allowed_user_ids or owner_id in self.allowed_user_ids


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value else None


def _rfc3339(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _safe_zone(value: str | None) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(str(value or "Asia/Taipei"))
    except (ZoneInfoNotFoundError, ValueError):
        return timezone(timedelta(hours=8), name="Asia/Taipei")


def _clean_title(value: object) -> str:
    compact = " ".join(str(value or "").split())
    return compact[:160] or "Google 行程"


def _clean_location(value: object) -> str:
    return " ".join(str(value or "").split())[:300]


def _payload(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise GoogleCalendarError("google_invalid_response", 502) from exc
    if not isinstance(data, dict):
        raise GoogleCalendarError("google_invalid_response", 502)
    return data


def ensure_google_calendar_indexes() -> None:
    """Create only credential/state indexes; Google event bodies are never persisted."""
    try:
        google_calendar_connections_coll.create_index("owner_id", unique=True)
        google_calendar_oauth_states_coll.create_index("state_digest", unique=True)
        google_calendar_oauth_states_coll.create_index("expires_at", expireAfterSeconds=0)
        google_calendar_oauth_states_coll.create_index([("owner_id", 1), ("created_at", -1)])
    except Exception as exc:
        print(f"Google Calendar index setup skipped: {type(exc).__name__}")


class GoogleCalendarService:
    def __init__(
        self,
        *,
        settings: GoogleCalendarSettings | None = None,
        connections: Any = None,
        oauth_states: Any = None,
        http_session: requests.Session | None = None,
        now: Callable[[], datetime] = _utc_now,
    ):
        self.settings = settings or GoogleCalendarSettings.from_env()
        self.connections = (
            connections if connections is not None else google_calendar_connections_coll
        )
        self.oauth_states = (
            oauth_states if oauth_states is not None else google_calendar_oauth_states_coll
        )
        self.http = http_session or requests.Session()
        self.now = now

    def _require_available(self, owner_id: str) -> Fernet:
        if not self.settings.configured:
            raise GoogleCalendarError("google_calendar_unavailable", 503)
        if not self.settings.allows(owner_id):
            raise GoogleCalendarError("google_calendar_account_not_allowed", 403)
        return Fernet(self.settings.token_key.encode("ascii"))

    def _encrypt(self, value: str, cipher: Fernet) -> str:
        return cipher.encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt(self, value: object, cipher: Fernet) -> str:
        try:
            return cipher.decrypt(str(value or "").encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeError) as exc:
            raise GoogleCalendarError("google_calendar_credentials_invalid", 409) from exc

    def status(self, owner_id: str) -> dict[str, Any]:
        if not self.settings.configured or not self.settings.allows(owner_id):
            return {
                "state": "unavailable",
                "connected": False,
                "connected_at": None,
                "last_synced_at": None,
            }
        record = self.connections.find_one({"owner_id": owner_id}) or {}
        state = str(record.get("state") or "disconnected")
        if state not in {"connected", "reauth_required"}:
            state = "disconnected"
        return {
            "state": state,
            "connected": state == "connected",
            "connected_at": _iso(record.get("connected_at")),
            "last_synced_at": _iso(record.get("last_synced_at")),
        }

    def start_authorization(self, owner_id: str) -> dict[str, Any]:
        cipher = self._require_available(owner_id)
        now = self.now()
        expires_at = now + timedelta(minutes=10)
        state = secrets.token_urlsafe(48)
        state_digest = hashlib.sha256(state.encode("utf-8")).hexdigest()
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        self.oauth_states.delete_many({"owner_id": owner_id})
        self.oauth_states.insert_one({
            "state_digest": state_digest,
            "owner_id": owner_id,
            "code_verifier": self._encrypt(verifier, cipher),
            "created_at": now,
            "expires_at": expires_at,
        })
        authorization_url = f"{GOOGLE_AUTH_URL}?{urlencode({
            'client_id': self.settings.client_id,
            'redirect_uri': self.settings.redirect_uri,
            'response_type': 'code',
            'scope': GOOGLE_CALENDAR_SCOPE,
            'access_type': 'offline',
            'prompt': 'consent',
            'include_granted_scopes': 'false',
            'state': state,
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
        })}"
        return {"authorization_url": authorization_url, "expires_at": _iso(expires_at)}

    def complete_authorization(
        self,
        *,
        state: str,
        code: str | None = None,
        oauth_error: str | None = None,
    ) -> str:
        if not self.settings.configured:
            raise GoogleCalendarError("google_calendar_unavailable", 503)
        state_digest = hashlib.sha256(str(state or "").encode("utf-8")).hexdigest()
        record = self.oauth_states.find_one_and_delete({
            "state_digest": state_digest,
            "expires_at": {"$gt": self.now()},
        })
        if not record:
            raise GoogleCalendarError("oauth_state_invalid_or_expired", 400)
        owner_id = str(record.get("owner_id") or "").strip()
        cipher = self._require_available(owner_id)
        if oauth_error:
            return "cancelled"
        if not code:
            raise GoogleCalendarError("oauth_code_missing", 400)
        verifier = self._decrypt(record.get("code_verifier"), cipher)
        try:
            response = self.http.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.settings.client_id,
                    "client_secret": self.settings.client_secret,
                    "code": code,
                    "code_verifier": verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": self.settings.redirect_uri,
                },
                timeout=(3, 10),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise GoogleCalendarError("google_oauth_unavailable", 502) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise GoogleCalendarError("google_oauth_exchange_failed", 400)
        payload = _payload(response)
        access_token = str(payload.get("access_token") or "").strip()
        granted_scopes = frozenset(str(payload.get("scope") or "").split())
        if not access_token or granted_scopes != {GOOGLE_CALENDAR_SCOPE}:
            raise GoogleCalendarError("google_calendar_scope_not_granted", 400)
        existing = self.connections.find_one({"owner_id": owner_id}) or {}
        refresh_token = str(payload.get("refresh_token") or "").strip()
        encrypted_refresh = (
            self._encrypt(refresh_token, cipher)
            if refresh_token
            else str(existing.get("refresh_token") or "")
        )
        if not encrypted_refresh:
            raise GoogleCalendarError("google_refresh_token_missing", 400)
        try:
            expires_in = max(60, int(payload.get("expires_in") or 3600))
        except (TypeError, ValueError):
            expires_in = 3600
        now = self.now()
        self.connections.update_one(
            {"owner_id": owner_id},
            {
                "$set": {
                    "owner_id": owner_id,
                    "state": "connected",
                    "access_token": self._encrypt(access_token, cipher),
                    "refresh_token": encrypted_refresh,
                    "access_token_expires_at": now + timedelta(seconds=expires_in),
                    "granted_scopes": sorted(granted_scopes),
                    "connected_at": existing.get("connected_at") or now,
                    "updated_at": now,
                    "encryption_version": 1,
                },
                "$unset": {"last_error": ""},
            },
            upsert=True,
        )
        return "connected"

    def _mark_reauth_required(self, owner_id: str) -> None:
        self.connections.update_one(
            {"owner_id": owner_id},
            {
                "$set": {
                    "state": "reauth_required",
                    "updated_at": self.now(),
                    "last_error": "invalid_grant",
                },
                "$unset": {
                    "access_token": "",
                    "refresh_token": "",
                    "access_token_expires_at": "",
                },
            },
        )

    def _refresh_access_token(self, owner_id: str, record: dict, cipher: Fernet) -> str:
        refresh_token = self._decrypt(record.get("refresh_token"), cipher)
        try:
            response = self.http.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.settings.client_id,
                    "client_secret": self.settings.client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=(3, 10),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise GoogleCalendarError("google_oauth_unavailable", 502) from exc
        payload = _payload(response)
        if response.status_code < 200 or response.status_code >= 300:
            if str(payload.get("error") or "") == "invalid_grant":
                self._mark_reauth_required(owner_id)
                raise GoogleCalendarError("google_calendar_reauth_required", 409)
            raise GoogleCalendarError("google_oauth_refresh_failed", 502)
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise GoogleCalendarError("google_invalid_response", 502)
        try:
            expires_in = max(60, int(payload.get("expires_in") or 3600))
        except (TypeError, ValueError):
            expires_in = 3600
        now = self.now()
        self.connections.update_one(
            {"owner_id": owner_id},
            {"$set": {
                "state": "connected",
                "access_token": self._encrypt(access_token, cipher),
                "access_token_expires_at": now + timedelta(seconds=expires_in),
                "updated_at": now,
            }},
        )
        return access_token

    def _access_token(self, owner_id: str, record: dict, cipher: Fernet, *, force: bool = False) -> str:
        expires_at = record.get("access_token_expires_at")
        if not force and record.get("access_token") and isinstance(expires_at, datetime):
            if _as_utc(expires_at) > self.now() + timedelta(seconds=60):
                return self._decrypt(record.get("access_token"), cipher)
        return self._refresh_access_token(owner_id, record, cipher)

    def _usable_access_token(
        self,
        owner_id: str,
        record: dict,
        cipher: Fernet,
        *,
        force: bool = False,
    ) -> str:
        try:
            return self._access_token(owner_id, record, cipher, force=force)
        except GoogleCalendarError as exc:
            if exc.code != "google_calendar_credentials_invalid":
                raise
            self._mark_reauth_required(owner_id)
            raise GoogleCalendarError("google_calendar_reauth_required", 409) from exc

    def _event_interval(self, event: dict, calendar_timezone: str) -> tuple[datetime, datetime, bool, str] | None:
        start_data = event.get("start") if isinstance(event.get("start"), dict) else {}
        end_data = event.get("end") if isinstance(event.get("end"), dict) else {}
        if start_data.get("date") and end_data.get("date"):
            try:
                zone_name = str(start_data.get("timeZone") or calendar_timezone or "Asia/Taipei")
                zone = _safe_zone(zone_name)
                start = datetime.combine(date.fromisoformat(start_data["date"]), time_value.min, zone)
                end = datetime.combine(date.fromisoformat(end_data["date"]), time_value.min, zone)
            except (TypeError, ValueError):
                return None
            if end <= start:
                return None
            return start.astimezone(timezone.utc), end.astimezone(timezone.utc), True, zone_name
        try:
            start = datetime.fromisoformat(str(start_data.get("dateTime") or "").replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(end_data.get("dateTime") or "").replace("Z", "+00:00"))
        except ValueError:
            return None
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            return None
        zone_name = str(start_data.get("timeZone") or calendar_timezone or "Asia/Taipei")
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc), False, zone_name

    def _event_projection(self, owner_id: str, event: dict, calendar_timezone: str) -> dict | None:
        if str(event.get("status") or "") == "cancelled":
            return None
        interval = self._event_interval(event, calendar_timezone)
        provider_id = str(event.get("id") or "").strip()
        if not interval or not provider_id:
            return None
        start, end, all_day, zone_name = interval
        opaque_id = hashlib.sha256(f"{owner_id}\0{provider_id}".encode("utf-8")).hexdigest()[:32]
        status = "tentative" if event.get("status") == "tentative" else "confirmed"
        if all_day:
            zone = _safe_zone(zone_name)
            start_wire = start.astimezone(zone).date().isoformat()
            end_wire = end.astimezone(zone).date().isoformat()
        else:
            start_wire = _iso(start)
            end_wire = _iso(end)
        return {
            "event_id": f"google_{opaque_id}",
            "source_type": "google",
            "status": status,
            "title": _clean_title(event.get("summary")),
            "start_at": start_wire,
            "end_at": end_wire,
            "all_day": all_day,
            "timezone": zone_name,
            "location": _clean_location(event.get("location")),
            "notes": "",
            "participants": [],
            "activity": "",
            "budget": "",
            "revision": 0,
            "coordination_id": None,
            "pending_change": None,
        }

    def list_events(self, owner_id: str, start: datetime, end: datetime) -> dict[str, Any]:
        cipher = self._require_available(owner_id)
        start = _as_utc(start)
        end = _as_utc(end)
        if end <= start or end - start > timedelta(days=MAX_RANGE_DAYS):
            raise GoogleCalendarError("google_calendar_range_invalid", 400)
        record = self.connections.find_one({"owner_id": owner_id}) or {}
        if record.get("state") == "reauth_required":
            raise GoogleCalendarError("google_calendar_reauth_required", 409)
        if record.get("state") != "connected":
            raise GoogleCalendarError("google_calendar_not_connected", 409)
        if frozenset(record.get("granted_scopes") or ()) != {GOOGLE_CALENDAR_SCOPE}:
            self._mark_reauth_required(owner_id)
            raise GoogleCalendarError("google_calendar_reauth_required", 409)
        access_token = self._usable_access_token(owner_id, record, cipher)
        events: list[dict] = []
        page_token: str | None = None
        truncated = False
        calendar_timezone = "Asia/Taipei"
        for page_index in range(MAX_EVENT_PAGES):
            params = {
                "timeMin": _rfc3339(start),
                "timeMax": _rfc3339(end),
                "singleEvents": "true",
                "orderBy": "startTime",
                "showDeleted": "false",
                "maxResults": str(EVENTS_PER_PAGE),
                "fields": "nextPageToken,timeZone,items(id,status,summary,location,start,end)",
            }
            if page_token:
                params["pageToken"] = page_token
            response: requests.Response | None = None
            for attempt in range(2):
                try:
                    response = self.http.get(
                        f"{GOOGLE_CALENDAR_API}/calendars/{quote('primary', safe='')}/events",
                        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                        params=params,
                        timeout=(3, 12),
                        allow_redirects=False,
                    )
                except requests.RequestException as exc:
                    if attempt == 0:
                        time.sleep(0.2)
                        continue
                    raise GoogleCalendarError("google_calendar_unavailable", 502) from exc
                if response.status_code == 401 and attempt == 0:
                    latest = self.connections.find_one({"owner_id": owner_id}) or record
                    access_token = self._usable_access_token(
                        owner_id,
                        latest,
                        cipher,
                        force=True,
                    )
                    continue
                if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                    time.sleep(0.2)
                    continue
                break
            if response is None:
                raise GoogleCalendarError("google_calendar_unavailable", 502)
            if response.status_code in {401, 403}:
                self._mark_reauth_required(owner_id)
                raise GoogleCalendarError("google_calendar_reauth_required", 409)
            if response.status_code < 200 or response.status_code >= 300:
                raise GoogleCalendarError("google_calendar_fetch_failed", 502)
            payload = _payload(response)
            calendar_timezone = str(payload.get("timeZone") or calendar_timezone)
            rows = payload.get("items") if isinstance(payload.get("items"), list) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                projection = self._event_projection(owner_id, row, calendar_timezone)
                if projection:
                    events.append(projection)
            page_token = str(payload.get("nextPageToken") or "").strip() or None
            if not page_token:
                break
            if page_index == MAX_EVENT_PAGES - 1:
                truncated = True
        events.sort(key=lambda event: (str(event.get("start_at") or ""), event["event_id"]))
        synced_at = self.now()
        self.connections.update_one(
            {"owner_id": owner_id},
            {"$set": {"last_synced_at": synced_at, "updated_at": synced_at, "last_error": ""}},
        )
        return {"events": events, "fetched_at": _iso(synced_at), "truncated": truncated}

    def disconnect(self, owner_id: str) -> dict[str, Any]:
        record = self.connections.find_one_and_delete({"owner_id": owner_id})
        self.oauth_states.delete_many({"owner_id": owner_id})
        revoked = False
        if record and self.settings.configured:
            try:
                cipher = Fernet(self.settings.token_key.encode("ascii"))
                token = self._decrypt(record.get("refresh_token") or record.get("access_token"), cipher)
                response = self.http.post(
                    GOOGLE_REVOKE_URL,
                    data={"token": token},
                    timeout=(3, 8),
                    allow_redirects=False,
                )
                revoked = 200 <= response.status_code < 300
            except (GoogleCalendarError, requests.RequestException):
                revoked = False
        return {"state": "disconnected", "connected": False, "revocation_confirmed": revoked}


_DEFAULT_SERVICE: GoogleCalendarService | None = None


def get_google_calendar_service() -> GoogleCalendarService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = GoogleCalendarService()
    return _DEFAULT_SERVICE
