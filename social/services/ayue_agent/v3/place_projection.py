"""Bounded Places projections shared by presentation and Web candidate binding."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import config
from services.ayue_agent.google_places_client import _public_display_name
from services.ayue_agent.web_tools import is_safe_public_url

from .contracts import SubTaskStatus


_PLACE_CATEGORIES = ("restaurant", "cafe", "bar", "attraction", "park")
MAX_PLACE_CARDS = 8
MAX_PLACE_CARDS_PER_CATEGORY = 5


def _osm_embed_url(map_url: str) -> str:
    """Build a bounded OSM embed URL only from our typed public map link."""
    try:
        parsed = urlsplit(map_url)
        if parsed.scheme != "https" or parsed.hostname != "www.openstreetmap.org":
            return ""
        query = parse_qs(parsed.query)
        lat = float((query.get("mlat") or [""])[0])
        lon = float((query.get("mlon") or [""])[0])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return ""
    except (TypeError, ValueError):
        return ""
    params = urlencode({
        "bbox": f"{lon - .004:.6f},{lat - .003:.6f},{lon + .004:.6f},{lat + .003:.6f}",
        "layer": "mapnik",
        "marker": f"{lat:.6f},{lon:.6f}",
    })
    return f"https://www.openstreetmap.org/export/embed.html?{params}"


def _google_embed_url(place_id: str) -> str:
    """Build a Google Maps Embed URL for one validated Google Place ID."""
    place_id = str(place_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,180}", place_id):
        return ""
    browser_key = str(getattr(config, "GOOGLE_MAPS_BROWSER_API_KEY", "") or "")
    if not browser_key:
        return ""
    params = urlencode({"key": browser_key, "q": f"place_id:{place_id}"})
    return f"https://www.google.com/maps/embed/v1/place?{params}"


def _distance_label(value: Any) -> str:
    try:
        distance = max(0, int(value))
    except (TypeError, ValueError):
        return ""
    if distance <= 0:
        return ""
    if distance < 1_000:
        return f"約 {distance} 公尺"
    return f"約 {distance / 1_000:.1f} 公里"


def _place_candidate_ref(run_id: str, unique_key: str) -> str:
    digest = hashlib.sha256(f"{run_id}\0{unique_key}".encode("utf-8")).hexdigest()[:16]
    return f"place_candidate_{digest}"


def public_place_cards(
    task_results: Any,
    *,
    run_id: str | None = None,
    include_internal: bool = False,
) -> list[dict[str, Any]]:
    """Project verified places observations into bounded provider-neutral cards.

    V3-specific projection: collects cards from ALL places observations (no
    early return), caps each category at MAX_PLACE_CARDS_PER_CATEGORY, then
    round-robins across categories up to MAX_PLACE_CARDS total. This keeps a
    mixed request (e.g. 牛排 + 冰) balanced instead of letting the first
    category fill the whole budget. Validation rules mirror the V2 projection
    (provider allowlist, place_id/map-url safety, category allowlist, dedup).
    """
    cards_by_category: dict[str, list[dict[str, Any]]] = {c: [] for c in _PLACE_CATEGORIES}
    seen: set[str] = set()
    for result in task_results:
        if isinstance(result, dict):
            status_ok = result.get("status", "ok") in {"ok", SubTaskStatus.OK, SubTaskStatus.OK.value}
            tool_name = result.get("tool") or result.get("tool_name")
            observation = result.get("result") or result.get("observation")
        else:
            status_ok = result.status is SubTaskStatus.OK
            tool_name = result.tool_name
            observation = result.observation
        if not status_ok or not observation:
            continue
        if tool_name == "places.search_nearby":
            places = (observation or {}).get("places") or []
        elif tool_name == "places.resolve_place":
            place = (observation or {}).get("place") or {}
            places = [place] if place else []
        else:
            continue
        attribution = re.sub(r"\s+", " ", str((observation or {}).get("attribution") or "")).strip()[:80]
        attribution_url = str((observation or {}).get("attribution_url") or "")
        if not is_safe_public_url(attribution_url):
            attribution_url = ""
        for item in places:
            provider = str(item.get("provider") or "openstreetmap")
            if provider not in {"openstreetmap", "google"}:
                continue
            place_id = str(item.get("place_id") or "").strip()
            map_url = str(item.get("map_url") or "").strip()
            if map_url and not is_safe_public_url(map_url):
                map_url = ""
            if provider == "google":
                if not re.fullmatch(r"[A-Za-z0-9_-]{3,180}", place_id):
                    continue
                unique_key = f"google:{place_id}"
            else:
                if not map_url:
                    continue
                unique_key = f"openstreetmap:{map_url}"
            if unique_key in seen:
                continue
            seen.add(unique_key)
            category = str(item.get("category") or "attraction")
            if category not in _PLACE_CATEGORIES:
                category = "attraction"
            if len(cards_by_category[category]) >= MAX_PLACE_CARDS_PER_CATEGORY:
                continue
            card = {
                "provider": provider,
                "place_id": place_id if provider == "google" else "",
                # Keep a final public-boundary check even when a provider
                # double or cached observation bypasses the Google adapter.
                "name": _public_display_name(item.get("name")) or "地點",
                "category": category,
                "address_summary": re.sub(r"\s+", " ", str(item.get("address_summary") or "")).strip()[:180],
                "distance_label": _distance_label(item.get("distance_m")),
                "map_url": map_url,
                "embed_url": (_google_embed_url(place_id) if provider == "google" else _osm_embed_url(map_url)),
                "attribution": attribution or ("Google Maps" if provider == "google" else "© OpenStreetMap contributors"),
                "attribution_url": attribution_url,
            }
            if include_internal:
                card["candidate_ref"] = _place_candidate_ref(run_id or "preview", unique_key)
                try:
                    card["distance_m"] = max(0, int(item.get("distance_m")))
                except (TypeError, ValueError):
                    card["distance_m"] = None
            if provider == "google":
                photo_url = str(item.get("photo_url") or "")
                if photo_url:
                    try:
                        parsed_photo = urlsplit(photo_url)
                        if (
                            parsed_photo.scheme == "https"
                            and parsed_photo.hostname.lower() == "places.googleapis.com"
                            and parsed_photo.path.startswith("/v1/places/")
                            and parsed_photo.path.endswith("/media")
                        ):
                            card["photo_url"] = photo_url
                    except (TypeError, ValueError):
                        pass
            cards_by_category[category].append(card)

    balanced: list[dict[str, Any]] = []
    active = [c for c in _PLACE_CATEGORIES if cards_by_category[c]]
    while active and len(balanced) < MAX_PLACE_CARDS:
        next_active: list[str] = []
        for category in active:
            if len(balanced) >= MAX_PLACE_CARDS:
                break
            balanced.append(cards_by_category[category].pop(0))
            if cards_by_category[category]:
                next_active.append(category)
        active = next_active
    return balanced
