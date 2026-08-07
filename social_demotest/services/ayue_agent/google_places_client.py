"""Small, bounded Google Places adapter used only for optional place cards."""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from typing import Any
from urllib.parse import urlencode

import requests

import config


_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
# Field mask drives billing (migration plan §3.7.2). Text Search Pro SKU only:
#   places.photos is Pro (not Enterprise), so photo names ride along free.
#   rating/userRatingCount/currentOpeningHours are Enterprise ($35/1000) and
#   must never be requested here.
_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.types,places.googleMapsUri,places.photos"
)
_CATEGORY_QUERIES = {
    "restaurant": "restaurant",
    "cafe": "cafe",
    "bar": "bar",
    "attraction": "tourist attraction",
    "park": "park",
}

# Process-local TTL cache. Google Places is billed per call, so caching the
# typed projection (never the raw payload) keeps the planner-facing surface
# identical while protecting quota. See docs/google-maps-migration-plan.md §3.5.
TEXT_SEARCH_TTL_SECONDS = 15 * 60

_CACHE_LOCK = threading.Lock()
_MEMORY_CACHE: dict[str, tuple[float, Any]] = {}


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


def _cache_key(prefix: str, payload: dict[str, Any]) -> str:
    canonical = repr(sorted(payload.items())).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(canonical).hexdigest()}"


def _cache_get(cache_key: str) -> Any | None:
    now = time.time()
    with _CACHE_LOCK:
        cached = _MEMORY_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
        if cached:
            _MEMORY_CACHE.pop(cache_key, None)
    return None


def _cache_put(cache_key: str, data: Any, ttl_seconds: int) -> None:
    with _CACHE_LOCK:
        _MEMORY_CACHE[cache_key] = (time.time() + ttl_seconds, data)


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


def _clean_cuisine(value: Any) -> str:
    """Bound a free-text cuisine hint: strip control chars, collapse spaces, cap length."""
    return " ".join(str(value or "").split())[:30]


def _photo_url(item: dict[str, Any]) -> str:
    """Build a media URL from the first photo of a Text Search place.

    The photos field itself is Pro-tier (rides along with the existing mask at
    no extra SKU), but loading the media bytes bills the Place Details Photos
    SKU (GetPhotoMediaRequest). AYUE_GOOGLE_PLACE_PHOTOS_ENABLED must be on or
    no photo_url is produced at all, so a default-off deployment never touches
    the media endpoint.
    """
    if not getattr(config, "AYUE_GOOGLE_PLACE_PHOTOS_ENABLED", False):
        return ""
    photos = item.get("photos") or []
    if not isinstance(photos, list) or not photos:
        return ""
    first = photos[0] if isinstance(photos[0], dict) else {}
    name_attr = _clean(first.get("name"), 200)
    if not name_attr or "/" not in name_attr:
        return ""
    # Media is loaded by the browser, so only the explicitly browser-visible,
    # HTTP-referrer-restricted key may appear here.  The server key is reserved
    # for server-to-server request headers and must never cross the boundary.
    browser_key = str(getattr(config, "GOOGLE_MAPS_BROWSER_API_KEY", "") or "").strip()
    if not browser_key:
        return ""
    query = urlencode({"maxWidthPx": 400, "key": browser_key})
    return f"https://places.googleapis.com/v1/{name_attr}/media?{query}"


def search_nearby_places(
    anchor_label: str, lat: float, lon: float, categories: list[str], *, limit: int,
    cuisine: str = "",
) -> list[dict[str, Any]]:
    """Resolve a bounded set of public places; no raw Google payload escapes."""
    if not google_place_cards_enabled():
        raise GooglePlacesError("google_places_disabled")
    requested = [item for item in categories if item in _CATEGORY_QUERIES][:3]
    if not requested:
        raise GooglePlacesError("invalid_place_category")
    query = " or ".join(_CATEGORY_QUERIES[item] for item in requested)
    cuisine_clean = _clean_cuisine(cuisine)
    if cuisine_clean:
        query = f"{cuisine_clean} {query}"
    safe_limit = max(1, min(int(limit), 10))
    cache_key = _cache_key("g_nearby", {
        "label": anchor_label.lower(), "lat": round(lat, 5), "lon": round(lon, 5),
        "categories": tuple(requested), "cuisine": cuisine_clean, "limit": safe_limit,
    })
    cached = _cache_get(cache_key)
    if cached is not None:
        return list(cached)
    body = {
        "textQuery": f"{query} near {anchor_label}",
        "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lon}, "radius": 5000.0}},
        "maxResultCount": safe_limit,
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
            "photo_url": _photo_url(item),
        })
    places = sorted(places, key=lambda item: (item["distance_m"], item["name"]))[:safe_limit]
    _cache_put(cache_key, list(places), TEXT_SEARCH_TTL_SECONDS)
    return places


def resolve_place(query: str) -> dict[str, Any] | None:
    """Resolve one explicit public place name into a live Google Place ID."""
    if not google_place_cards_enabled():
        raise GooglePlacesError("google_places_disabled")
    cleaned = _clean(query, 160)
    if not cleaned:
        raise GooglePlacesError("location_required")
    cache_key = _cache_key("g_resolve", {"query": cleaned.lower()})
    cached = _cache_get(cache_key)
    if cached is not None:
        return dict(cached) if cached else None
    body = {"textQuery": cleaned, "maxResultCount": 1, "languageCode": "zh-TW"}
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
        _cache_put(cache_key, None, TEXT_SEARCH_TTL_SECONDS)
        return None
    item = rows[0] or {}
    place_id = _clean(item.get("id"), 180)
    display = item.get("displayName") or {}
    name = _clean(display.get("text") if isinstance(display, dict) else display, 80)
    map_url = _clean(item.get("googleMapsUri"), 500)
    if not place_id or not name or not map_url.startswith("https://"):
        _cache_put(cache_key, None, TEXT_SEARCH_TTL_SECONDS)
        return None
    types = [str(value) for value in (item.get("types") or [])]
    category = _category(types, ["restaurant", "cafe", "bar", "attraction", "park"])
    place = {
        "name": name, "category": category, "distance_m": 0,
        "address_summary": _clean(item.get("formattedAddress"), 120), "map_url": map_url,
        "provider": "google", "place_id": place_id, "photo_url": _photo_url(item),
    }
    _cache_put(cache_key, place, TEXT_SEARCH_TTL_SECONDS)
    return place


_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
_ROUTES_FIELD_MASK = "routes.distanceMeters,routes.duration,routes.routeLabels"
_ROUTES_TTL_SECONDS = 3600


def measure_distance_matrix(origin: str, destination: str) -> dict[str, Any] | None:
    """Resolve a real driving distance and duration via Routes API Compute Routes.

    Returns {distance_m, duration_text, distance_basis: "driving"} or None on
    failure. The caller falls back to OSM haversine when this returns None.
    See docs/google-maps-migration-plan.md §3.3 D.
    """
    if not google_place_cards_enabled():
        return None
    if not getattr(config, "AYUE_GOOGLE_DISTANCE_MATRIX_ENABLED", True):
        return None
    origin_clean = _clean(origin, 160)
    dest_clean = _clean(destination, 160)
    if not origin_clean or not dest_clean:
        return None
    cache_key = _cache_key("g_routes", {"o": origin_clean.lower(), "d": dest_clean.lower()})
    cached = _cache_get(cache_key)
    if cached is not None:
        return dict(cached) if cached else None
    body = {
        "origin": {"address": origin_clean},
        "destination": {"address": dest_clean},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_UNAWARE",
        "languageCode": "zh-TW",
    }
    try:
        response = requests.post(
            _ROUTES_URL,
            headers={
                "X-Goog-Api-Key": str(config.GOOGLE_PLACES_SERVER_API_KEY),
                "X-Goog-FieldMask": _ROUTES_FIELD_MASK,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=(3, 12),
        )
    except (requests.Timeout, requests.RequestException):
        return None
    if not response.ok:
        _cache_put(cache_key, None, _ROUTES_TTL_SECONDS)
        return None
    try:
        routes = (response.json() or {}).get("routes") or []
    except ValueError:
        return None
    if not routes:
        _cache_put(cache_key, None, _ROUTES_TTL_SECONDS)
        return None
    route = routes[0] or {}
    try:
        distance_m = int(route.get("distanceMeters"))
    except (TypeError, ValueError):
        return None
    duration_text = ""
    duration = route.get("duration")
    # Routes API v2 returns duration as a string like "888s" (protobuf Duration
    # JSON form), NOT an object. Handle both shapes defensively.
    seconds = 0
    if isinstance(duration, str):
        try:
            seconds = int(duration.rstrip("s"))
        except ValueError:
            seconds = 0
    elif isinstance(duration, dict):
        seconds_str = str(duration.get("seconds") or "")
        try:
            seconds = int(seconds_str.rstrip("s")) if seconds_str else 0
        except ValueError:
            seconds = 0
    if seconds > 0:
        if seconds < 60:
            duration_text = f"約 {seconds} 秒"
        elif seconds < 3600:
            duration_text = f"約 {seconds // 60} 分鐘"
        else:
            duration_text = f"約 {seconds // 3600} 小時 {(seconds % 3600) // 60} 分鐘"
    result = {
        "origin_label": origin_clean,
        "destination_label": dest_clean,
        "distance_m": distance_m,
        "duration_text": duration_text,
        "distance_basis": "driving",
        "attribution": "Google Maps",
        "attribution_url": "https://www.google.com/maps",
    }
    _cache_put(cache_key, result, _ROUTES_TTL_SECONDS)
    return result
