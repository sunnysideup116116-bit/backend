"""Small, bounded Google Places adapter used only for optional place cards."""

from __future__ import annotations

import math
from typing import Any

import requests

import config


_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.types,places.googleMapsUri"
)
_CATEGORY_QUERIES = {
    "restaurant": "restaurant",
    "cafe": "cafe",
    "bar": "bar",
    "attraction": "tourist attraction",
    "park": "park",
}


class GooglePlacesError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def google_place_cards_enabled() -> bool:
    return bool(
        getattr(config, "AYUE_GOOGLE_PLACE_CARDS_ENABLED", False)
        and getattr(config, "GOOGLE_PLACES_SERVER_API_KEY", "")
        and getattr(config, "GOOGLE_MAPS_BROWSER_API_KEY", "")
    )


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _distance_m(left_lat: float, left_lon: float, right_lat: float, right_lon: float) -> int:
    radius = 6_371_000.0
    lat_delta = math.radians(right_lat - left_lat)
    lon_delta = math.radians(right_lon - left_lon)
    a = math.sin(lat_delta / 2) ** 2 + math.cos(math.radians(left_lat)) * math.cos(math.radians(right_lat)) * math.sin(lon_delta / 2) ** 2
    return int(round(2 * radius * math.asin(math.sqrt(a))))


def _category(types: list[Any], requested: list[str]) -> str:
    values = {str(item) for item in types}
    for candidate in requested:
        if candidate == "attraction" and values & {"tourist_attraction", "museum", "art_gallery"}:
            return candidate
        if candidate in values:
            return candidate
    return requested[0]


def search_nearby_places(
    anchor_label: str, lat: float, lon: float, categories: list[str], *, limit: int,
) -> list[dict[str, Any]]:
    """Resolve a bounded set of public places; no raw Google payload escapes."""
    if not google_place_cards_enabled():
        raise GooglePlacesError("google_places_disabled")
    requested = [item for item in categories if item in _CATEGORY_QUERIES][:3]
    if not requested:
        raise GooglePlacesError("invalid_place_category")
    query = " or ".join(_CATEGORY_QUERIES[item] for item in requested)
    body = {
        "textQuery": f"{query} near {anchor_label}",
        "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lon}, "radius": 5000.0}},
        "maxResultCount": max(1, min(int(limit), 10)),
        "languageCode": "zh-TW",
    }
    try:
        response = requests.post(
            _TEXT_SEARCH_URL,
            headers={
                "X-Goog-Api-Key": str(config.GOOGLE_PLACES_SERVER_API_KEY),
                "X-Goog-FieldMask": _FIELD_MASK,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=(3, 12),
        )
    except requests.Timeout as exc:
        raise GooglePlacesError("google_places_timeout") from exc
    except requests.RequestException as exc:
        raise GooglePlacesError("google_places_unavailable") from exc
    if not response.ok:
        if response.status_code in {401, 403}:
            raise GooglePlacesError("google_places_access_denied")
        if response.status_code == 429:
            raise GooglePlacesError("google_places_rate_limited")
        raise GooglePlacesError("google_places_unavailable")
    try:
        raw_places = response.json().get("places") or []
    except ValueError as exc:
        raise GooglePlacesError("google_places_invalid_response") from exc
    places: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_places[:10]:
        place_id = _clean(item.get("id"), 180)
        display = item.get("displayName") or {}
        name = _clean(display.get("text") if isinstance(display, dict) else display, 80)
        location = item.get("location") or {}
        try:
            item_lat, item_lon = float(location["latitude"]), float(location["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not place_id or not name or place_id in seen:
            continue
        seen.add(place_id)
        map_url = _clean(item.get("googleMapsUri"), 500)
        if not map_url.startswith("https://"):
            continue
        places.append({
            "name": name,
            "category": _category(item.get("types") or [], requested),
            "distance_m": _distance_m(lat, lon, item_lat, item_lon),
            "address_summary": _clean(item.get("formattedAddress"), 120),
            "map_url": map_url,
            "provider": "google",
            "place_id": place_id,
        })
    return sorted(places, key=lambda item: (item["distance_m"], item["name"]))[:max(1, min(int(limit), 10))]


def resolve_place(query: str) -> dict[str, Any] | None:
    """Resolve one explicit public place name into a live Google Place ID."""
    if not google_place_cards_enabled():
        raise GooglePlacesError("google_places_disabled")
    body = {"textQuery": _clean(query, 160), "maxResultCount": 1, "languageCode": "zh-TW"}
    if not body["textQuery"]:
        raise GooglePlacesError("location_required")
    try:
        response = requests.post(
            _TEXT_SEARCH_URL,
            headers={
                "X-Goog-Api-Key": str(config.GOOGLE_PLACES_SERVER_API_KEY),
                "X-Goog-FieldMask": _FIELD_MASK,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=(3, 12),
        )
    except requests.Timeout as exc:
        raise GooglePlacesError("google_places_timeout") from exc
    except requests.RequestException as exc:
        raise GooglePlacesError("google_places_unavailable") from exc
    if not response.ok:
        if response.status_code in {401, 403}:
            raise GooglePlacesError("google_places_access_denied")
        if response.status_code == 429:
            raise GooglePlacesError("google_places_rate_limited")
        raise GooglePlacesError("google_places_unavailable")
    try:
        rows = response.json().get("places") or []
    except ValueError as exc:
        raise GooglePlacesError("google_places_invalid_response") from exc
    if not rows:
        return None
    item = rows[0] or {}
    place_id = _clean(item.get("id"), 180)
    display = item.get("displayName") or {}
    name = _clean(display.get("text") if isinstance(display, dict) else display, 80)
    map_url = _clean(item.get("googleMapsUri"), 500)
    if not place_id or not name or not map_url.startswith("https://"):
        return None
    types = [str(value) for value in (item.get("types") or [])]
    category = _category(types, ["restaurant", "cafe", "bar", "attraction", "park"])
    return {
        "name": name, "category": category, "distance_m": 0,
        "address_summary": _clean(item.get("formattedAddress"), 120), "map_url": map_url,
        "provider": "google", "place_id": place_id,
    }
