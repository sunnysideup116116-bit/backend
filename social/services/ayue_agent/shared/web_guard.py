"""Authorization checks for public web extraction."""

from __future__ import annotations

import re
from typing import Any, Sequence

from services.ayue_agent.web_tools import is_safe_public_url


def web_extract_urls_allowed(
    turn_ctx: Any,
    results: Sequence[dict[str, Any]],
    urls: Sequence[str],
) -> bool:
    """Bind extraction to a search result or a URL supplied by the owner."""
    allowed: set[str] = set()
    for result in results:
        if not isinstance(result, dict) or result.get("tool") != "web.search":
            continue
        for item in ((result.get("result") or {}).get("results") or []):
            url = str((item or {}).get("url") or "")
            if is_safe_public_url(url):
                allowed.add(url)
    for raw in re.findall(
        r"https?://[^\s<>\]\[\"']+",
        str(getattr(turn_ctx, "message", "") or ""),
    ):
        url = raw.rstrip(".,，。!?！？:：;；)")
        if is_safe_public_url(url):
            allowed.add(url)
    return bool(urls) and all(str(url) in allowed for url in urls)
