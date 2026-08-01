"""Server-owned, fail-soft Giphy reactions for durable system events.

This module is deliberately not an agent tool: callers choose from an internal
event allowlist and users cannot supply a search query or invoke an endpoint.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

import requests

import config
from services.mediator_event_service import queue_mediator_event


GIPHY_SEARCH_URL = "https://api.giphy.com/v1/gifs/search"
MATCH_CELEBRATION_QUERY = "kawaii cute celebration happy dance animated"
_REQUEST_TIMEOUT = (3, 8)
_CACHE_TTL_SECONDS = 15 * 60
_cache_lock = threading.Lock()
_reaction_cache: tuple[float, list[dict[str, Any]]] | None = None
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ayue-giphy")


def giphy_enabled() -> bool:
    return bool(getattr(config, "GIPHY_GIF_ENABLED", False) and getattr(config, "GIPHY_API_KEY", ""))


def _safe_giphy_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or not (host == "giphy.com" or host.endswith(".giphy.com")):
        return ""
    return url[:1200]


def _parse_reactions(payload: Any) -> list[dict[str, Any]]:
    reactions: list[dict[str, Any]] = []
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    for item in rows[:5]:
        images = item.get("images", {}) if isinstance(item, dict) else {}
        original = images.get("original", {}) if isinstance(images, dict) else {}
        preview = images.get("fixed_height_small", {}) if isinstance(images, dict) else {}
        url = _safe_giphy_url(original.get("url") if isinstance(original, dict) else "")
        preview_url = _safe_giphy_url(preview.get("url") if isinstance(preview, dict) else "") or url
        if not url or not preview_url:
            continue
        reactions.append({
            "provider": "giphy",
            "url": url,
            "preview_url": preview_url,
            "width": str(original.get("width") or "")[:8] if isinstance(original, dict) else "",
            "height": str(original.get("height") or "")[:8] if isinstance(original, dict) else "",
            "alt_text": "阿月替配對成功送上的慶祝 GIF",
        })
    return reactions


def _match_celebration_reactions() -> list[dict[str, Any]]:
    global _reaction_cache
    now = time.time()
    with _cache_lock:
        if _reaction_cache and _reaction_cache[0] > now:
            return list(_reaction_cache[1])
    if not giphy_enabled():
        return []
    try:
        response = requests.get(
            GIPHY_SEARCH_URL,
            params={
                "api_key": config.GIPHY_API_KEY,
                "q": MATCH_CELEBRATION_QUERY,
                "limit": 5,
                "rating": "g",
                "lang": "en",
                "bundle": "messaging_non_clips",
            },
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        reactions = _parse_reactions(response.json())
    except (requests.RequestException, ValueError, TypeError):
        return []
    if reactions:
        with _cache_lock:
            _reaction_cache = (now + _CACHE_TTL_SECONDS, list(reactions))
    return reactions


def _pick_reaction(match_id: str) -> dict[str, Any] | None:
    reactions = _match_celebration_reactions()
    if not reactions:
        return None
    # Giphy ranks search results by relevance. Keep the most relevant result
    # instead of pseudo-randomly selecting a weaker/odd result from the page.
    return reactions[0]


def write_match_celebration_gifs(
    participants: tuple[tuple[str, str], ...], match_id: str,
) -> bool:
    """Best-effort private-mediator reactions. A GIF failure never affects a match."""
    reaction = _pick_reaction(match_id)
    if not reaction:
        return False
    delivered = False
    for user_id, other_id in dict.fromkeys(participants):
        try:
            queued = queue_mediator_event(
                user_id,
                "太好了～快去和對方聊聊吧！",
                "match_connected_gif",
                event_key=f"match:{match_id}:celebration:gif:{user_id}",
                match_id=match_id,
                other_id=other_id,
                media=reaction,
            )
            delivered = bool(queued) or delivered
        except Exception:
            # Each private delivery is independent and the match is already committed.
            continue
    return delivered


def schedule_match_celebration_gifs(
    participants: tuple[tuple[str, str], ...], match_id: str, schedule_task=None,
) -> None:
    """Queue reaction delivery without delaying an accepted state transition."""
    task = lambda: write_match_celebration_gifs(participants, match_id)
    if schedule_task is not None:
        schedule_task(task)
    else:
        _executor.submit(task)
