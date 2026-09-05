"""Read public Appwrite nicknames for proposal UI, never for model context.

Appwrite owns registered profile names; Mongo display names remain a fallback
for seed/legacy accounts. This adapter does not synchronize or mutate profiles.
"""

from collections import OrderedDict
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Callable
from urllib.parse import urlparse

from dotenv import load_dotenv
import requests

from services.match_card_projection import safe_proposal_nickname


if os.getenv("AYUE_SKIP_DOTENV", "").strip().lower() not in {"1", "true", "on"}:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

_ENDPOINT = (os.getenv("APPWRITE_ENDPOINT") or "https://appwrite.misproject.us.ci/v1").strip().rstrip("/")
_PROJECT_ID = os.getenv("APPWRITE_PROJECT_ID") or ""
_API_KEY = os.getenv("APPWRITE_API_KEY") or ""
_CACHE_TTL_SECONDS = 30.0
_CACHE_LIMIT = 256
_cache: OrderedDict[str, tuple[float, str | None]] = OrderedDict()
_cache_lock = threading.Lock()


def _read_appwrite_nickname(user_id: str) -> str | None:
    """None means unavailable; an empty name must not revive a stale alias."""
    endpoint = urlparse(_ENDPOINT)
    if (
        not _PROJECT_ID or not _API_KEY or not endpoint.hostname
        or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
        or not (
            endpoint.scheme == "https"
            or (endpoint.scheme == "http" and endpoint.hostname in {"localhost", "127.0.0.1", "::1"})
        )
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,35}", user_id)
    ):
        return None
    with _cache_lock:
        cached = _cache.get(user_id)
        if cached is not None and cached[0] > time.monotonic():
            _cache.move_to_end(user_id)
            return cached[1]
    name = None
    try:
        response = requests.get(
            f"{_ENDPOINT}/databases/dating_db/collections/user_profiles/documents/{user_id}",
            headers={"X-Appwrite-Project": _PROJECT_ID, "X-Appwrite-Key": _API_KEY},
            params={"queries[]": json.dumps({"method": "select", "values": ["name"]})},
            timeout=(1.0, 2.0),
            # Never forward the server credential to a redirected host.
            allow_redirects=False,
        )
        if response.status_code == 200:
            profile = response.json()
            if isinstance(profile, dict):
                raw_name = profile.get("name")
                # Older seed profiles use their document ID as a placeholder;
                # their human-facing name lives in Mongo. This exception does
                # not revive stale aliases for empty/unsafe registered names.
                is_seed_placeholder = (
                    user_id.startswith("seed_user_")
                    and isinstance(raw_name, str) and raw_name.strip() == user_id
                )
                if not is_seed_placeholder:
                    name = safe_proposal_nickname(raw_name, user_id)
    except (requests.RequestException, ValueError):
        # No response bodies, profile fields, credentials or URLs in logs.
        pass
    with _cache_lock:
        _cache[user_id] = (time.monotonic() + _CACHE_TTL_SECONDS, name)
        _cache.move_to_end(user_id)
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)
    return name


def proposal_display_name(user_id: str, *, fallback_lookup: Callable[[str], str]) -> str:
    """Resolve one UI-only name without failing the proposal on lookup errors."""
    if not isinstance(user_id, str) or not user_id:
        return ""
    try:
        value = _read_appwrite_nickname(user_id)
        if value is None:
            value = fallback_lookup(user_id)
        return safe_proposal_nickname(value, user_id)
    except Exception:
        return ""
