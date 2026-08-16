"""Coarse, owner-controlled location projection for public profile features."""

from __future__ import annotations

import re
from typing import Any


_UNSAFE_RE = re.compile(r"[\x00-\x1f<>`{}$]|(?:seed_user|demo_user|mongodb|https?://)", re.IGNORECASE)


def _clean_part(value: Any, limit: int = 20) -> str:
    text = re.sub(r"\s+", "", str(value or "")).strip("，,。.")[:limit]
    return "" if _UNSAFE_RE.search(text) else text


def normalize_profile_location(city: Any, district: Any) -> dict[str, str]:
    """Return the only public location shape; never accepts exact addresses."""
    city_value = _clean_part(city)
    district_value = _clean_part(district)
    if not city_value and not district_value:
        return {}
    display_name = f"{city_value}{district_value}"[:40]
    return {
        "country": "台灣",
        "city": city_value,
        "district": district_value,
        "display_name": display_name,
    }


def safe_profile_location(profile: dict[str, Any] | None) -> dict[str, str]:
    raw = (profile or {}).get("profile_location") or {}
    if not isinstance(raw, dict):
        return {}
    return normalize_profile_location(raw.get("city"), raw.get("district"))
