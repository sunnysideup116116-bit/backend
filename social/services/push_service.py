"""Fire-and-forget Appwrite Messaging delivery for prepared chat events."""

import os
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from services.notification_service import (
    NotificationEvent,
    prepare_message_notifications,
)

SERVER_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(dotenv_path=SERVER_ROOT / ".env", override=False)

def _validated_endpoint(value: str) -> str:
    endpoint = value.strip().rstrip("/")
    parsed = urlparse(endpoint)
    if parsed.scheme == "https":
        return endpoint
    if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return endpoint
    raise ValueError("APPWRITE_ENDPOINT must use HTTPS outside loopback development")


_CONFIG_ERROR = ""
try:
    _ENDPOINT = _validated_endpoint(
        os.getenv("APPWRITE_ENDPOINT") or "https://appwrite.misproject.us.ci/v1"
    )
except ValueError as exc:
    _ENDPOINT = ""
    _CONFIG_ERROR = str(exc)
_PROJECT_ID = os.getenv("APPWRITE_PROJECT_ID") or ""
_API_KEY = os.getenv("APPWRITE_API_KEY") or ""
_FCM_PROVIDER_ID = os.getenv("APPWRITE_FCM_PROVIDER_ID") or "6a81bf000036a6eaf5e0"
_ENABLED = bool(
    os.getenv("APPWRITE_PUSH_ENABLED", "false").strip().lower() == "true"
) and bool(_ENDPOINT and _PROJECT_ID and _API_KEY)

_HEADERS = {
    "X-Appwrite-Project": _PROJECT_ID,
    "X-Appwrite-Key": _API_KEY,
    "Content-Type": "application/json",
}

def _valid_push_target_ids(user_id: str) -> list[str]:
    """Resolve non-expired FCM targets without exposing their identifiers."""
    try:
        response = requests.get(
            f"{_ENDPOINT}/users/{user_id}", headers=_HEADERS, timeout=10,
        )
    except Exception as exc:
        print(f"[push_service] target lookup failed: {type(exc).__name__}: {exc}")
        return []
    if response.status_code >= 300:
        print(
            f"[push_service] target lookup failed: HTTP {response.status_code}: "
            f"{response.text[:160]}"
        )
        return []
    try:
        targets = response.json().get("targets") or []
    except Exception:
        return []
    return [
        str(item.get("$id"))
        for item in targets
        if item.get("$id")
        and str(item.get("providerType") or "") == "push"
        and not bool(item.get("expired"))
        and (
            not _FCM_PROVIDER_ID
            or not item.get("providerId")
            or str(item.get("providerId")) == _FCM_PROVIDER_ID
        )
    ]


def _send_prepared_event(event: NotificationEvent) -> None:
    if not _ENABLED:
        return
    target_ids = _valid_push_target_ids(event.recipient_id)
    if not target_ids:
        print(
            f"[push_service] skipped: no valid push target for "
            f"user={event.recipient_id} surface={event.surface}"
        )
        return
    payload = {
        "messageId": uuid.uuid4().hex,
        "title": event.title,
        "body": event.body,
        "targets": target_ids,
        "data": {key: str(value) for key, value in event.data.items()},
        "tag": event.tag,
        "priority": "high",
    }
    try:
        response = requests.post(
            f"{_ENDPOINT}/messaging/messages/push",
            headers=_HEADERS,
            json=payload,
            timeout=10,
        )
        if response.status_code >= 300:
            print(
                f"[push_service] push dispatch failed: HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
    except Exception as exc:
        print(f"[push_service] push dispatch failed: {type(exc).__name__}: {exc}")


def _dispatch_prepared(events: list[NotificationEvent]) -> None:
    for event in events:
        _send_prepared_event(event)


def send_push_notification(msg: dict) -> None:
    """Record unread state and synchronously dispatch eligible notifications."""
    events = prepare_message_notifications(msg)
    if _ENABLED:
        _dispatch_prepared(events)


def queue_push_notification(msg: dict) -> None:
    """Record unread state, then queue best-effort Appwrite delivery."""
    events = prepare_message_notifications(msg)
    if not _ENABLED or not events:
        return
    threading.Thread(
        target=_dispatch_prepared,
        args=(events,),
        name="push-dispatch",
        daemon=True,
    ).start()
