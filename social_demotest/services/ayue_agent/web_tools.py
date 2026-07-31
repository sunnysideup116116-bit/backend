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
MAX_EXTRACT_CHARS_PER_PAGE = 4_000


def web_enabled() -> bool:
    return bool(getattr(config, "TAVILY_API_KEY", None))


def _clean_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _headers() -> dict[str, str]:
    headers = {"Authorization": f"Bearer {getattr(config, 'TAVILY_API_KEY', '')}", "Content-Type": "application/json"}
    project = str(getattr(config, "TAVILY_PROJECT", "") or "").strip()
    if project:
        headers["X-Project-ID"] = project
    return headers


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
    return "web_unavailable"


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
        "search_depth": "basic",
        "max_results": MAX_SEARCH_RESULTS,
        "include_answer": False,
        "include_raw_content": False,
    }
    if recency and recency != "none":
        payload["time_range"] = recency
    try:
        response = requests.post(TAVILY_SEARCH_URL, headers=_headers(), json=payload, timeout=(3, 15))
    except requests.Timeout:
        return None, "web_timeout"
    except requests.RequestException:
        return None, "web_unavailable"
    if not response.ok:
        return None, _error_code(response)
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
    try:
        response = requests.post(TAVILY_EXTRACT_URL, headers=_headers(), json=payload, timeout=(3, 20))
    except requests.Timeout:
        return None, "web_timeout"
    except requests.RequestException:
        return None, "web_unavailable"
    if not response.ok:
        return None, _error_code(response)
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
