"""Server-owned, fail-soft Giphy reactions for durable system events.

This module is deliberately not an agent tool: callers choose from an internal
event allowlist and users cannot supply a search query or invoke an endpoint.
"""

from __future__ import annotations

from collections import deque
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

import requests

import config
from services.mediator_event_service import queue_mediator_event


GIPHY_SEARCH_URL = "https://api.giphy.com/v1/gifs/search"
MATCH_CELEBRATION_QUERIES = (
    "funny reaction laugh",
    "funny excited dance",
    "funny happy fail",
    "funny animal celebration",
    "funny meme reaction",
)
_REQUEST_TIMEOUT = (3, 8)
_CACHE_TTL_SECONDS = 15 * 60
_RECENT_REACTION_LIMIT = 80
_cache_lock = threading.Lock()
_reaction_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_recent_reaction_urls: deque[str] = deque(maxlen=_RECENT_REACTION_LIMIT)
_last_query = ""
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
    for item in rows[:25]:
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
            "alt_text": "阿月送上的搞笑 GIF",
        })
    return reactions


def _next_celebration_query() -> str:
    global _last_query
    with _cache_lock:
        choices = [query for query in MATCH_CELEBRATION_QUERIES if query != _last_query]
        query = random.choice(choices or MATCH_CELEBRATION_QUERIES)
        _last_query = query
    return query


def _match_celebration_reactions(query: str) -> list[dict[str, Any]]:
    now = time.time()
    with _cache_lock:
        cached = _reaction_cache.get(query)
        if cached and cached[0] > now:
            return list(cached[1])
    if not giphy_enabled():
        return []
    try:
        response = requests.get(
            GIPHY_SEARCH_URL,
            params={
                "api_key": config.GIPHY_API_KEY,
                "q": query,
                "limit": 25,
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
            _reaction_cache[query] = (now + _CACHE_TTL_SECONDS, list(reactions))
    return reactions


def _pick_reaction() -> dict[str, Any] | None:
    reactions = _match_celebration_reactions(_next_celebration_query())
    if not reactions:
        return None
    with _cache_lock:
        recent_urls = set(_recent_reaction_urls)
        unused = [reaction for reaction in reactions if reaction["url"] not in recent_urls]
        reaction = random.choice(unused or reactions)
        # Reserve it immediately so two concurrent acceptance effects cannot
        # choose the same GIF. It is bounded and never persisted as user data.
        _recent_reaction_urls.append(reaction["url"])
    return reaction


def write_match_celebration_gifs(
    participants: tuple[tuple[str, str], ...], match_id: str,
) -> bool:
    """Best-effort private-mediator reactions. A GIF failure never affects a match."""
    delivered = False
    for user_id, other_id in dict.fromkeys(participants):
        reaction = _pick_reaction()
        if not reaction:
            continue
        try:
            queued = queue_mediator_event(
                user_id,
                "這張有夠好笑，丟給你 😂",
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
