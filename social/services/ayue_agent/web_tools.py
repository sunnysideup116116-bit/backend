"""Bounded Tavily adapter used only by Public Ayue's typed read tools."""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

import requests
import config


TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
MAX_SEARCH_RESULTS = 5
MAX_EXTRACT_URLS = 2
MAX_EXTRACT_CHARS_PER_PAGE = 8_000


import threading

_KEY_LOCK = threading.Lock()
_CURRENT_KEY_INDEX = 0


def get_tavily_api_keys() -> list[str]:
    raw = getattr(config, "TAVILY_API_KEYS", None) or getattr(config, "TAVILY_API_KEY", None)
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(k).strip() for k in raw if str(k).strip()]
    return [k.strip() for k in str(raw).split(",") if k.strip()]


def web_enabled() -> bool:
    return bool(get_tavily_api_keys())


def _clean_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _headers_for_key(key: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    project = str(getattr(config, "TAVILY_PROJECT", "") or "").strip()
    if project:
        headers["X-Project-ID"] = project
    return headers


def _headers() -> dict[str, str]:
    keys = get_tavily_api_keys()
    key = keys[_CURRENT_KEY_INDEX % len(keys)] if keys else ""
    return _headers_for_key(key)


def is_safe_public_url(value: Any) -> bool:
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return False
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return True
        return not (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_unspecified)
    except Exception:
        return False


def _error_code(response: requests.Response) -> str:
    if response.status_code in {401, 403}:
        return "web_auth_error"
    if response.status_code == 429:
        return "web_rate_limited"
    if 400 <= response.status_code < 500:
        return "web_bad_request"
    if response.status_code >= 500:
        return "web_provider_error"
    return "web_unavailable"


def _post_tavily_with_rotation(url: str, payload: dict[str, Any], timeout: tuple[int, int]) -> tuple[requests.Response | None, str | None]:
    global _CURRENT_KEY_INDEX
    keys = get_tavily_api_keys()
    if not keys:
        return None, "web_not_configured"

    attempts = 0
    max_attempts = len(keys)
    last_error_code = "web_unavailable"

    while attempts < max_attempts:
        with _KEY_LOCK:
            key_idx = _CURRENT_KEY_INDEX % len(keys)
            key = keys[key_idx]

        headers = _headers_for_key(key)
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.Timeout:
            return None, "web_timeout"
        except requests.RequestException:
            return None, "web_network_error"

        if response.ok:
            return response, None

        err_code = _error_code(response)
        last_error_code = err_code

        if err_code in {"web_auth_error", "web_rate_limited", "web_bad_request"} or response.status_code in {400, 401, 402, 403, 429}:
            with _KEY_LOCK:
                if _CURRENT_KEY_INDEX % len(keys) == key_idx:
                    _CURRENT_KEY_INDEX = (_CURRENT_KEY_INDEX + 1) % len(keys)
            attempts += 1
            continue

        return None, err_code

    return None, last_error_code


def search_web(query: str, *, recency: str = "none", location: str = "") -> tuple[dict[str, Any] | None, str | None]:
    if not web_enabled():
        return None, "web_not_configured"
    text = _clean_text(query, 300)
    if not text:
        return None, "invalid_web_query"
    if location:
        text = f"{text} {location}"[:340]
    payload: dict[str, Any] = {
        "query": text,
        "topic": "general",
        "search_depth": "advanced",
        "max_results": MAX_SEARCH_RESULTS,
        "include_answer": False,
        "include_raw_content": False,
    }
    if recency and recency != "none":
        payload["time_range"] = recency
    response, error_code = _post_tavily_with_rotation(TAVILY_SEARCH_URL, payload, (3, 15))
    if error_code or response is None:
        return None, error_code
    try:
        raw_results = response.json().get("results") or []
    except ValueError:
        return None, "web_invalid_response"
    results = []
    for item in raw_results[:MAX_SEARCH_RESULTS]:
        url = str(item.get("url") or "")
        if not is_safe_public_url(url):
            continue
        title = _clean_text(item.get("title"), 140) or urlsplit(url).hostname or "來源"
        snippet = _clean_text(item.get("content"), 900)
        results.append({"title": title, "url": url, "snippet": snippet, "published_date": _clean_text(item.get("published_date"), 32)})
    return {"results": results}, None


def extract_web(urls: list[str], *, query: str = "") -> tuple[dict[str, Any] | None, str | None]:
    if not web_enabled():
        return None, "web_not_configured"
    valid_urls = [str(url) for url in urls if is_safe_public_url(url)][:MAX_EXTRACT_URLS]
    if not valid_urls:
        return None, "invalid_web_url"
    payload: dict[str, Any] = {
        "urls": valid_urls,
        "extract_depth": "basic",
        "format": "markdown",
        "include_images": False,
        "include_favicon": False,
    }
    if query:
        payload["query"] = _clean_text(query, 300)
        payload["chunks_per_source"] = 3
    response, error_code = _post_tavily_with_rotation(TAVILY_EXTRACT_URL, payload, (3, 20))
    if error_code or response is None:
        return None, error_code
    try:
        raw_results = response.json().get("results") or []
    except ValueError:
        return None, "web_invalid_response"
    pages = []
    for item in raw_results[:MAX_EXTRACT_URLS]:
        url = str(item.get("url") or "")
        if not is_safe_public_url(url):
            continue
        content = str(item.get("raw_content") or "")
        pages.append({"url": url, "content": content[:MAX_EXTRACT_CHARS_PER_PAGE], "truncated": len(content) > MAX_EXTRACT_CHARS_PER_PAGE})
    return {"pages": pages}, None
