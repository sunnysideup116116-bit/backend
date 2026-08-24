"""Small, bounded Google Places adapter used only for optional place cards."""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from typing import Any, Iterable
from urllib.parse import urlencode, urlsplit

import requests

import config


_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
# Field mask drives billing. Keep the ordinary projection bounded. The optional
# rating/userRatingCount/currentOpeningHours/price fields are optional fields
# and are appended only for an explicitly requested enrichment.
_BASE_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.types,places.googleMapsUri,places.photos"
)
_FIELD_MASK = _BASE_FIELD_MASK
_SUPPORTED_ENRICHMENTS = frozenset({"rating", "hours", "price", "walking"})
_PLACE_FIELD_ENRICHMENTS = frozenset({"price", "rating", "hours"})
_PRICE_LEVELS = {
    "PRICE_LEVEL_FREE": "free",
    "PRICE_LEVEL_INEXPENSIVE": "inexpensive",
    "PRICE_LEVEL_MODERATE": "moderate",
    "PRICE_LEVEL_EXPENSIVE": "expensive",
    "PRICE_LEVEL_VERY_EXPENSIVE": "very_expensive",
}
_CATEGORY_QUERIES = {
    "restaurant": "restaurant",
    "cafe": "cafe",
    "bar": "bar",
    "attraction": "tourist attraction",
    "park": "park",
}

# Process-local TTL cache. Google Places is billed per call, so caching the
# typed projection (never the raw payload) keeps the planner-facing surface
# identical while protecting quota.
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


def google_routes_enabled() -> bool:
    """Routes is server-to-server and does not depend on the browser map key."""
    return bool(
        getattr(config, "AYUE_GOOGLE_DISTANCE_MATRIX_ENABLED", True)
        and getattr(config, "GOOGLE_PLACES_SERVER_API_KEY", "")
    )


def google_place_enrichments_enabled() -> bool:
    return bool(getattr(config, "AYUE_GOOGLE_PLACE_ENRICHMENTS_ENABLED", False))


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _normalize_enrichments(
    enrichments: Iterable[str] | None,
    *,
    allowed: frozenset[str] = _SUPPORTED_ENRICHMENTS,
) -> tuple[str, ...]:
    values = [str(item or "").strip() for item in (enrichments or [])]
    unsupported = {value for value in values if value not in allowed}
    if unsupported:
        raise GooglePlacesError("invalid_place_enrichment")
    normalized = tuple(sorted(set(values)))
    if not google_place_enrichments_enabled():
        return tuple(item for item in normalized if item not in _PLACE_FIELD_ENRICHMENTS)
    return normalized


def _places_field_mask(enrichments: tuple[str, ...]) -> str:
    fields = [_BASE_FIELD_MASK]
    if "rating" in enrichments:
        fields.extend(("places.rating", "places.userRatingCount"))
    if "hours" in enrichments:
        fields.append("places.currentOpeningHours")
    if "price" in enrichments:
        fields.extend(("places.priceLevel", "places.priceRange"))
    return ",".join(fields)


def _duration_seconds(value: Any) -> int | None:
    if isinstance(value, str):
        raw = value.strip()
        try:
            seconds = float(raw[:-1] if raw.endswith("s") else raw)
        except ValueError:
            return None
    elif isinstance(value, dict):
        try:
            seconds = float(value.get("seconds") or 0)
            seconds += float(value.get("nanos") or 0) / 1_000_000_000
        except (TypeError, ValueError):
            return None
    else:
        return None
    if seconds < 0:
        return None
    return int(round(seconds))


def _opening_hours_projection(item: dict[str, Any]) -> dict[str, Any] | None:
    raw = item.get("currentOpeningHours")
    if not isinstance(raw, dict):
        return None
    open_now = raw.get("openNow")
    if not isinstance(open_now, bool):
        open_now = None
    descriptions = raw.get("weekdayDescriptions") or []
    if not isinstance(descriptions, list):
        descriptions = []
    return {
        "open_now": open_now,
        "next_open_time": _clean(raw.get("nextOpenTime"), 48) or None,
        "next_close_time": _clean(raw.get("nextCloseTime"), 48) or None,
        "weekday_descriptions": [
            _clean(value, 160) for value in descriptions[:7] if _clean(value, 160)
        ],
    }


def _money_projection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    currency_code = value.get("currencyCode")
    if not isinstance(currency_code, str) or not re.fullmatch(r"[A-Z]{3}", currency_code):
        return None
    raw_units = value.get("units")
    if isinstance(raw_units, bool) or not isinstance(raw_units, (int, str)):
        return None
    if isinstance(raw_units, str) and not re.fullmatch(r"-?\d+", raw_units):
        return None
    try:
        units = int(raw_units)
    except (TypeError, ValueError):
        return None
    if not -(2**63) <= units <= (2**63 - 1):
        return None
    if "nanos" not in value:
        nanos = 0
    else:
        nanos = value.get("nanos")
        if isinstance(nanos, bool) or not isinstance(nanos, int):
            return None
    if not -999_999_999 <= nanos <= 999_999_999:
        return None
    if (units > 0 and nanos < 0) or (units < 0 and nanos > 0):
        return None
    return {"currency_code": currency_code, "units": units, "nanos": nanos}


def _price_range_projection(item: dict[str, Any]) -> dict[str, Any] | None:
    raw = item.get("priceRange")
    if not isinstance(raw, dict):
        return None
    projected: dict[str, Any] = {}
    for source_key, output_key in (("startPrice", "start_price"), ("endPrice", "end_price")):
        money = _money_projection(raw.get(source_key))
        if money is not None:
            projected[output_key] = money
    return projected or None


def _place_enrichment(item: dict[str, Any], enrichments: tuple[str, ...]) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if "rating" in enrichments:
        rating = item.get("rating")
        if isinstance(rating, (int, float)) and not isinstance(rating, bool) and 0 <= rating <= 5:
            extra["rating"] = float(rating)
        count = item.get("userRatingCount")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            extra["user_rating_count"] = count
    if "hours" in enrichments:
        hours = _opening_hours_projection(item)
        if hours is not None:
            extra["opening_hours"] = hours
    if "price" in enrichments:
        raw_price_level = item.get("priceLevel")
        price_level = _PRICE_LEVELS.get(raw_price_level) if isinstance(raw_price_level, str) else None
        if price_level is not None:
            extra["price_level"] = price_level
        price_range = _price_range_projection(item)
        if price_range is not None:
            extra["price_range"] = price_range
    return extra


def _safe_google_maps_url(value: Any) -> str:
    url = _clean(value, 500)
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    host = str(parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return ""
    if host not in {"google.com", "www.google.com", "maps.google.com", "maps.app.goo.gl"}:
        return ""
    return url


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
    cuisine: str = "", radius_m: int = 1500,
    enrichments: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Resolve a bounded set of public places; no raw Google payload escapes."""
    if not google_place_cards_enabled():
        raise GooglePlacesError("google_places_disabled")
    requested_enrichments = _normalize_enrichments(enrichments)
    place_enrichments = tuple(
        item for item in requested_enrichments if item in _PLACE_FIELD_ENRICHMENTS
    )
    walking_requested = "walking" in requested_enrichments
    requested = [item for item in categories if item in _CATEGORY_QUERIES][:3]
    if not requested:
        raise GooglePlacesError("invalid_place_category")
    query = " or ".join(_CATEGORY_QUERIES[item] for item in requested)
    cuisine_clean = _clean_cuisine(cuisine)
    if cuisine_clean:
        query = f"{cuisine_clean} {query}"
    safe_limit = max(1, min(int(limit), 10))
    safe_radius = max(300, min(int(radius_m), 5_000))
    cache_key = _cache_key("g_nearby", {
        "label": anchor_label.lower(), "lat": round(lat, 5), "lon": round(lon, 5),
        "categories": tuple(requested), "cuisine": cuisine_clean, "limit": safe_limit,
        "radius_m": safe_radius,
        # Walking is a post-search Routes enrichment and must not force a
        # second Places request when the base candidate pool is cached.
        "enrichments": place_enrichments,
    })
    cached = _cache_get(cache_key)
    if cached is not None:
        places = [dict(item) for item in cached]
    else:
        body = {
            "textQuery": f"{query} near {anchor_label}",
            "locationBias": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lon},
                    "radius": float(safe_radius),
                }
            },
            "maxResultCount": safe_limit,
            "languageCode": "zh-TW",
        }
        try:
            response = requests.post(
                _TEXT_SEARCH_URL,
                headers={
                    "X-Goog-Api-Key": str(config.GOOGLE_PLACES_SERVER_API_KEY),
                    "X-Goog-FieldMask": _places_field_mask(place_enrichments),
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
        places = []
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
            map_url = _safe_google_maps_url(item.get("googleMapsUri"))
            if not map_url:
                continue
            distance_m = _distance_m(lat, lon, item_lat, item_lon)
            # Text Search locationBias is not a hard restriction.  Enforce the
            # planner-approved radius on the typed projection before any result can
            # become a candidate card or Web research subject.
            if distance_m > safe_radius:
                continue
            places.append({
                "name": name,
                "category": _category(item.get("types") or [], requested),
                "distance_m": distance_m,
                "address_summary": _clean(item.get("formattedAddress"), 120),
                "map_url": map_url,
                "provider": "google",
                "place_id": place_id,
                "photo_url": _photo_url(item),
                **_place_enrichment(item, place_enrichments),
            })
        places = sorted(places, key=lambda item: (item["distance_m"], item["name"]))[:safe_limit]
        # Cache the Places projection before adding walking fields so a later
        # walking-only request can reuse this base/enriched candidate pool.
        _cache_put(cache_key, [dict(item) for item in places], TEXT_SEARCH_TTL_SECONDS)
    if walking_requested and places:
        walking = measure_walking_matrix(lat, lon, places)
        for place in places:
            route = walking.get(str(place.get("place_id") or ""))
            if route:
                place.update(route)
    return places


def resolve_place(
    query: str, *, enrichments: Iterable[str] | None = None,
) -> dict[str, Any] | None:
    """Resolve one explicit public place name into a live Google Place ID."""
    if not google_place_cards_enabled():
        raise GooglePlacesError("google_places_disabled")
    place_enrichments = _normalize_enrichments(
        enrichments, allowed=_PLACE_FIELD_ENRICHMENTS,
    )
    cleaned = _clean(query, 160)
    if not cleaned:
        raise GooglePlacesError("location_required")
    cache_key = _cache_key(
        "g_resolve", {"query": cleaned.lower(), "enrichments": place_enrichments},
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return dict(cached) if cached else None
    body = {"textQuery": cleaned, "maxResultCount": 1, "languageCode": "zh-TW"}
    try:
        response = requests.post(
            _TEXT_SEARCH_URL,
            headers={
                "X-Goog-Api-Key": str(config.GOOGLE_PLACES_SERVER_API_KEY),
                "X-Goog-FieldMask": _places_field_mask(place_enrichments),
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
    map_url = _safe_google_maps_url(item.get("googleMapsUri"))
    if not place_id or not name or not map_url:
        _cache_put(cache_key, None, TEXT_SEARCH_TTL_SECONDS)
        return None
    types = [str(value) for value in (item.get("types") or [])]
    category = _category(types, ["restaurant", "cafe", "bar", "attraction", "park"])
    place = {
        "name": name, "category": category, "distance_m": 0,
        "address_summary": _clean(item.get("formattedAddress"), 120), "map_url": map_url,
        "provider": "google", "place_id": place_id, "photo_url": _photo_url(item),
        **_place_enrichment(item, place_enrichments),
    }
    _cache_put(cache_key, place, TEXT_SEARCH_TTL_SECONDS)
    return place


_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
_ROUTES_FIELD_MASK = "routes.distanceMeters,routes.duration"
_ROUTE_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
_ROUTE_MATRIX_FIELD_MASK = (
    "originIndex,destinationIndex,status,condition,distanceMeters,duration"
)
_ROUTES_TTL_SECONDS = 3600


def measure_distance_matrix(
    origin: str, destination: str, *, travel_mode: str = "DRIVE",
) -> dict[str, Any] | None:
    """Resolve one route via Routes API Compute Routes.

    ``DRIVE`` preserves the existing behavior. ``WALK`` is supported for
    explicit single-destination distance requests; nearby candidate pools use
    ``measure_walking_matrix`` below so they do not issue one request per place.
    """
    if not google_routes_enabled():
        return None
    mode = str(travel_mode or "DRIVE").strip().upper()
    if mode not in {"DRIVE", "WALK"}:
        return None
    origin_clean = _clean(origin, 160)
    dest_clean = _clean(destination, 160)
    if not origin_clean or not dest_clean:
        return None
    cache_key = _cache_key(
        "g_routes", {"o": origin_clean.lower(), "d": dest_clean.lower(), "mode": mode},
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return dict(cached) if cached else None
    body: dict[str, Any] = {
        "origin": {"address": origin_clean},
        "destination": {"address": dest_clean},
        "travelMode": mode,
        "languageCode": "zh-TW",
    }
    if mode == "DRIVE":
        body["routingPreference"] = "TRAFFIC_UNAWARE"
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
    seconds = _duration_seconds(route.get("duration")) or 0
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
        "duration_seconds": seconds or None,
        "distance_basis": "walking" if mode == "WALK" else "driving",
        "travel_mode": mode,
        "attribution": "Google Maps",
        "attribution_url": "https://www.google.com/maps",
    }
    _cache_put(cache_key, result, _ROUTES_TTL_SECONDS)
    return result


def measure_walking_matrix(
    origin_lat: float, origin_lon: float, places: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    """Resolve WALK distance/duration for a bounded candidate pool.

    Routes API REST returns one matrix element per origin/destination pair as
    an unordered array. Each element is keyed by destinationIndex, and a bad
    element is independent from the rest of the response. Only successful
    elements are returned to the caller.
    """
    if not google_routes_enabled():
        return {}
    candidates: list[tuple[str, dict[str, Any]]] = []
    seen_ids: set[str] = set()
    for place in places:
        place_id = str(place.get("place_id") or "")
        if not place_id or place_id in seen_ids:
            continue
        seen_ids.add(place_id)
        candidates.append((place_id, place))
        if len(candidates) >= 8:
            break
    if not candidates:
        return {}
    destination_ids = tuple(place_id for place_id, _place in candidates)
    cache_key = _cache_key(
        "g_route_matrix",
        {
            "mode": "WALK",
            "origin": (round(float(origin_lat), 5), round(float(origin_lon), 5)),
            "destinations": tuple(sorted(destination_ids)),
        },
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return {str(key): dict(value) for key, value in (cached or {}).items()}
    body = {
        "origins": [{
            "waypoint": {
                "location": {
                    "latLng": {"latitude": float(origin_lat), "longitude": float(origin_lon)},
                },
            },
        }],
        "destinations": [
            {"waypoint": {"placeId": place_id}} for place_id in destination_ids
        ],
        "travelMode": "WALK",
        "languageCode": "zh-TW",
        "units": "METRIC",
    }
    try:
        response = requests.post(
            _ROUTE_MATRIX_URL,
            headers={
                "X-Goog-Api-Key": str(config.GOOGLE_PLACES_SERVER_API_KEY),
                "X-Goog-FieldMask": _ROUTE_MATRIX_FIELD_MASK,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=(3, 12),
        )
    except (requests.Timeout, requests.RequestException):
        return {}
    if not response.ok:
        _cache_put(cache_key, {}, _ROUTES_TTL_SECONDS)
        return {}
    try:
        payload = response.json()
    except ValueError:
        return {}
    if isinstance(payload, list):
        elements = payload
    elif isinstance(payload, dict):
        # The REST response is normally a JSON array. Keep a narrow defensive
        # compatibility path for test doubles/proxies that wrap elements.
        elements = payload.get("elements") or []
    else:
        elements = []
    result: dict[str, dict[str, int]] = {}
    for element in elements:
        if not isinstance(element, dict):
            continue
        try:
            if int(element.get("originIndex")) != 0:
                continue
        except (TypeError, ValueError):
            continue
        status = element.get("status") or {}
        if isinstance(status, dict) and status.get("code") not in {None, 0}:
            continue
        if element.get("condition") != "ROUTE_EXISTS":
            continue
        try:
            destination_index = int(element.get("destinationIndex"))
            distance_m = int(element.get("distanceMeters"))
        except (TypeError, ValueError):
            continue
        if not (0 <= destination_index < len(destination_ids)) or distance_m < 0:
            continue
        duration_seconds = _duration_seconds(element.get("duration"))
        if duration_seconds is None:
            continue
        result[destination_ids[destination_index]] = {
            "walking_distance_m": distance_m,
            "walking_duration_seconds": duration_seconds,
        }
    _cache_put(cache_key, dict(result), _ROUTES_TTL_SECONDS)
    return result
