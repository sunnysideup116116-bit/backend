"""Privacy-safe public projection for Event-grounded match cards."""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlparse


def _text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:limit]


def _timestamp(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _sessions(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    starts = list(snapshot.get("session_starts") or [])[:8]
    ends = list(snapshot.get("session_ends") or [])[:8]
    precisions = list(snapshot.get("session_precisions") or [])[:8]
    result = []
    for index, raw_start in enumerate(starts):
        starts_at = _timestamp(raw_start)
        if not starts_at:
            continue
        result.append({
            "starts_at": starts_at,
            "ends_at": _timestamp(ends[index]) if index < len(ends) else starts_at,
            "time_precision": (
                "datetime"
                if index < len(precisions) and str(precisions[index]) == "datetime"
                else "date"
            ),
        })
    return result


def _safe_source_url(value: Any) -> str:
    raw = str(value or "").strip()[:500]
    try:
        parsed = urlparse(raw)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not hostname:
            return ""
        if parsed.username or parsed.password or hostname == "localhost" or hostname.endswith(".local"):
            return ""
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address and not address.is_global:
            return ""
    except ValueError:
        return ""
    return raw


def public_event_card(match_doc: dict[str, Any]) -> dict[str, Any] | None:
    """Return only public Event details; never expose Event or participant IDs."""
    if str(match_doc.get("proposal_source") or "") != "event_opportunity":
        return None
    snapshot = match_doc.get("event_snapshot") or {}
    title = _text(snapshot.get("title"), 160)
    if not title:
        return None
    result: dict[str, Any] = {
        "title": title,
        "venue": _text(snapshot.get("venue"), 120),
        "region": _text(snapshot.get("region"), 40),
        "category": _text(snapshot.get("category"), 30),
        "starts_at": _timestamp(snapshot.get("starts_at")),
        "ends_at": _timestamp(snapshot.get("ends_at")),
        "time_precision": (
            "datetime" if str(snapshot.get("time_precision") or "") == "datetime" else "date"
        ),
        "source_url": _safe_source_url(snapshot.get("source_url")),
    }
    sessions = _sessions(snapshot)
    if len(sessions) > 1:
        result["sessions"] = sessions
    return result
