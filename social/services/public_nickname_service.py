"""Canonical public names; callers retain owner/accepted-relation boundaries.

Appwrite owns registered profile names; Mongo display names remain a fallback
for synchronized, seed, and legacy projections. This adapter does not
synchronize or mutate profiles.
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


def _nickname_endpoint() -> str:
    """Resolve this deployment's loopback HTTP redirect before credentials.

    Port 80 redirects to loopback HTTPS, whose certificate is issued for the
    App's canonical hostname. Use that existing fixed origin directly, never
    a response-controlled Location and never disable TLS verification.
    Explicit alternative development ports/endpoints remain unchanged.
    """
    try:
        parsed = urlparse(_ENDPOINT)
        if (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and parsed.port in {None, 80} and parsed.path.rstrip("/") == "/v1"
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
        ):
            return "https://appwrite.misproject.us.ci/v1"
    except ValueError:
        return ""
    return _ENDPOINT


def _read_appwrite_nickname(user_id: str) -> str | None:
    """None means unavailable; an empty name must not revive a stale alias."""
    base_url = _nickname_endpoint()
    endpoint = urlparse(base_url)
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
            f"{base_url}/databases/dating_db/collections/user_profiles/documents/{user_id}",
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
    """Resolve a bounded public name; the caller owns authorization/scope."""
    if not isinstance(user_id, str) or not user_id:
        return ""
    try:
        value = _read_appwrite_nickname(user_id)
        if value is None:
            value = fallback_lookup(user_id)
        label = safe_proposal_nickname(value, user_id)
        return "" if label in {"(對方)", "（對方）"} else label
    except Exception:
        return ""


def contact_display_name(user_id: str | None, profile: dict | None = None) -> str:
    """Appwrite first, safe Mongo fallback; empty means unavailable, not a name."""
    if not isinstance(user_id, str) or not user_id:
        return ""

    def fallback(_user_id: str) -> str:
        source = profile
        if source is None:
            from database import profiles_coll
            source = profiles_coll.find_one(
                {"user_id": user_id}, {"_id": 0, "display_name": 1, "nickname": 1, "name": 1},
            ) or {}
        for field in ("name", "display_name", "nickname"):
            label = safe_proposal_nickname(source.get(field), user_id)
            if label and label not in {"(對方)", "（對方）"}:
                return label
        return ""

    return proposal_display_name(user_id, fallback_lookup=fallback)


def warm_public_nicknames(user_ids) -> None:
    """Batch only already-authorized IDs; name-only reads, ≤50 per request.

    Negative cache entries also prevent an outage from becoming one request
    per contact. Canonical empty names must never revive an old Mongo alias.
    """
    if not _PROJECT_ID or not _API_KEY:
        return
    base_url = _nickname_endpoint()
    endpoint = urlparse(base_url)
    if (
        not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
        or not (endpoint.scheme == "https" or (endpoint.scheme == "http" and endpoint.hostname in {"localhost", "127.0.0.1", "::1"}))
    ):
        return
    ids = list(dict.fromkeys(uid for uid in user_ids if isinstance(uid, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,35}", uid)))
    now = time.monotonic()
    with _cache_lock:
        missing = [uid for uid in ids if uid not in _cache or _cache[uid][0] <= now]
    for start in range(0, len(missing), 50):
        batch = missing[start:start + 50]
        found = {uid: None for uid in batch}
        try:
            response = requests.get(
                f"{base_url}/databases/dating_db/collections/user_profiles/documents",
                headers={"X-Appwrite-Project": _PROJECT_ID, "X-Appwrite-Key": _API_KEY},
                params={"queries[]": [json.dumps(q) for q in (
                    {"method": "equal", "attribute": "$id", "values": batch},
                    {"method": "select", "values": ["$id", "name"]},
                    {"method": "limit", "values": [len(batch)]},
                )]}, timeout=(1.0, 2.0), allow_redirects=False,
            )
            payload = response.json() if response.status_code == 200 else {}
            rows = payload.get("documents", []) if isinstance(payload, dict) else []
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict) or row.get("$id") not in found:
                    continue
                uid, raw = row["$id"], row.get("name")
                if not (uid.startswith("seed_user_") and isinstance(raw, str) and raw.strip() == uid):
                    found[uid] = safe_proposal_nickname(raw, uid)
        except (requests.RequestException, ValueError):
            pass
        with _cache_lock:
            for uid, value in found.items():
                # Do not overwrite a newer single-name read completed while
                # this batch was in flight.
                if uid in _cache and _cache[uid][0] > now:
                    continue
                _cache[uid] = (time.monotonic() + _CACHE_TTL_SECONDS, value)
                _cache.move_to_end(uid)
            while len(_cache) > _CACHE_LIMIT:
                _cache.popitem(last=False)
