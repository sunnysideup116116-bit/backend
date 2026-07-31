"""Bounded OpenStreetMap adapter for Public Ayue's typed place tools.

The planner never receives coordinates, OSM identifiers, or an arbitrary query
language.  This adapter owns geocoding, bounded Overpass QL, cache and the
straight-line distance calculation.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from typing import Any
from urllib.parse import quote

import requests

import config
NOMINATIM_TTL_SECONDS = 7 * 86400
NEARBY_TTL_SECONDS = 15 * 60
NOMINATIM_MIN_INTERVAL_SECONDS = 1.0
MAX_RADIUS_METERS = 5_000
MAX_RESULTS = 10

_RATE_LOCK = threading.Lock()
_LAST_NOMINATIM_AT = 0.0
_CACHE_LOCK = threading.Lock()
_MEMORY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

_CATEGORY_TAGS: dict[str, tuple[tuple[str, str], ...]] = {
    "restaurant": (("amenity", "restaurant"),),
    "cafe": (("amenity", "cafe"),),
    "bar": (("amenity", "bar"), ("amenity", "pub")),
    "attraction": (("tourism", "attraction"), ("tourism", "museum"), ("tourism", "gallery"), ("tourism", "viewpoint")),
    "park": (("leisure", "park"),),
}


class MapClientError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def maps_enabled() -> bool:
    return bool(getattr(config, "AYUE_MAPS_ENABLED", False))


def _persistent_cache_enabled() -> bool:
    """Never make a public map request wait on an unavailable Atlas cluster."""
    return bool(getattr(config, "AYUE_MAPS_MONGO_CACHE", False))


def _map_cache():
    # Keep this import lazy: a map lookup must still work in a local UI demo
    # when the optional Atlas-backed persistent cache is disabled.
    from database import db
    return db["ayue_map_cache"]


def ensure_map_cache_indexes() -> None:
    if not _persistent_cache_enabled():
        return
    try:
        cache = _map_cache()
        cache.create_index("cache_key", unique=True)
        cache.create_index("expires_at", expireAfterSeconds=0)
    except Exception as exc:
        print(f"Map cache index setup skipped: {type(exc).__name__}")


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def _cache_key(prefix: str, payload: dict[str, Any]) -> str:
    canonical = repr(sorted(payload.items())).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(canonical).hexdigest()}"


def _cache_get(cache_key: str) -> dict[str, Any] | None:
    now = time.time()
    with _CACHE_LOCK:
        cached = _MEMORY_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return dict(cached[1])
        if cached:
            _MEMORY_CACHE.pop(cache_key, None)
    if not _persistent_cache_enabled():
        return None
    try:
        record = _map_cache().find_one({"cache_key": cache_key, "expires_at": {"$gt": now}}, {"_id": 0, "data": 1}) or {}
        return record.get("data") if isinstance(record.get("data"), dict) else None
    except Exception:
        return None


def _cache_put(cache_key: str, data: dict[str, Any], ttl_seconds: int) -> None:
    expires_at = time.time() + ttl_seconds
    with _CACHE_LOCK:
        _MEMORY_CACHE[cache_key] = (expires_at, dict(data))
    if not _persistent_cache_enabled():
        return
    try:
        _map_cache().replace_one(
            {"cache_key": cache_key},
            {"cache_key": cache_key, "data": data, "expires_at": expires_at},
            upsert=True,
        )
    except Exception:
        pass


def _nominatim_wait() -> None:
    global _LAST_NOMINATIM_AT
    with _RATE_LOCK:
        remaining = NOMINATIM_MIN_INTERVAL_SECONDS - (time.monotonic() - _LAST_NOMINATIM_AT)
        if remaining > 0:
            time.sleep(remaining)
        _LAST_NOMINATIM_AT = time.monotonic()


def _request_error_code(response: requests.Response) -> str:
    if response.status_code == 429:
        return "map_rate_limited"
    if response.status_code in {401, 403}:
        return "map_access_denied"
    return "map_unavailable"


def nominatim_search(query: str) -> dict[str, Any]:
    """Resolve one human place name to a public, coarse map point."""
    if not maps_enabled():
        raise MapClientError("maps_disabled")
    query = _clean(query, 160)
    if not query:
        raise MapClientError("location_required")
    key = _cache_key("geocode", {"query": query.lower()})
    cached = _cache_get(key)
    if cached:
        return cached
    _nominatim_wait()
    try:
        response = requests.get(
            str(getattr(config, "OSM_NOMINATIM_URL", "")),
            params={"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 0},
            headers={"User-Agent": str(getattr(config, "OSM_USER_AGENT", "AyueDatingDemo/1.0")), "Accept-Language": "zh-TW"},
            timeout=(3, 12),
        )
    except requests.Timeout as exc:
        raise MapClientError("map_timeout") from exc
    except requests.RequestException as exc:
        raise MapClientError("map_unavailable") from exc
    if not response.ok:
        raise MapClientError(_request_error_code(response))
    try:
        rows = response.json()
    except ValueError as exc:
        raise MapClientError("map_invalid_response") from exc
    if not isinstance(rows, list) or not rows:
        raise MapClientError("location_not_found")
    item = rows[0] or {}
    try:
        point = {
            "label": _clean(item.get("display_name"), 140) or query,
            "lat": float(item["lat"]),
            "lon": float(item["lon"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise MapClientError("location_not_found") from exc
    _cache_put(key, point, NOMINATIM_TTL_SECONDS)
    return point


def build_overpass_nearby(categories: list[str], lat: float, lon: float, radius_m: int) -> str:
    """Build fixed, injection-free Overpass QL from allowlisted category tags."""
    radius = max(300, min(int(radius_m), MAX_RADIUS_METERS))
    clauses: list[str] = []
    for category in categories:
        for key, value in _CATEGORY_TAGS.get(category, ()):
            selector = f'["{key}"="{value}"]'
            for element_type in ("node", "way", "relation"):
                clauses.append(f'{element_type}(around:{radius},{lat:.6f},{lon:.6f}){selector};')
    if not clauses:
        raise MapClientError("invalid_place_category")
    return "[out:json][timeout:10];(" + "".join(clauses) + ");out center tags 50;"


def overpass_query(query: str) -> dict[str, Any]:
    primary = str(getattr(config, "OSM_OVERPASS_URL", "")).strip()
    fallback = str(getattr(config, "OSM_OVERPASS_FALLBACK_URL", "")).strip()
    endpoints = [item for item in (primary, fallback) if item]
    last_error = "map_unavailable"
    for index, endpoint in enumerate(endpoints):
        try:
            response = requests.post(
                endpoint, data={"data": query},
                headers={"User-Agent": str(getattr(config, "OSM_USER_AGENT", "AyueDatingDemo/1.0"))},
                timeout=(3, 15),
            )
        except requests.Timeout:
            last_error = "map_timeout"
            continue
        except requests.RequestException:
            last_error = "map_unavailable"
            continue
        if not response.ok:
            last_error = _request_error_code(response)
            # A rate-limit or access denial must not be bypassed by shifting
            # the same request onto another public service.
            if last_error in {"map_rate_limited", "map_access_denied"}:
                break
            continue
        try:
            data = response.json()
        except ValueError:
            last_error = "map_invalid_response"
            continue
        if isinstance(data, dict):
            return data
        last_error = "map_invalid_response"
    raise MapClientError(last_error)


def haversine_m(left_lat: float, left_lon: float, right_lat: float, right_lon: float) -> int:
    radius = 6_371_000.0
    lat_delta = math.radians(right_lat - left_lat)
    lon_delta = math.radians(right_lon - left_lon)
    a = math.sin(lat_delta / 2) ** 2 + math.cos(math.radians(left_lat)) * math.cos(math.radians(right_lat)) * math.sin(lon_delta / 2) ** 2
    return int(round(2 * radius * math.asin(math.sqrt(a))))


def _category_for_tags(tags: dict[str, Any], requested: list[str]) -> str:
    for category in requested:
        for key, value in _CATEGORY_TAGS.get(category, ()):
            if tags.get(key) == value:
                return category
    return requested[0]


def parse_overpass_elements(raw_elements: list[dict[str, Any]], *, ref_lat: float, ref_lon: float, requested_categories: list[str]) -> list[dict[str, Any]]:
    places: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for element in raw_elements[:50]:
        tags = element.get("tags") or {}
        if not isinstance(tags, dict):
            continue
        center = element.get("center") or element
        try:
            lat, lon = float(center["lat"]), float(center["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        name = _clean(tags.get("name") or tags.get("name:zh") or "", 80)
        if not name:
            continue
        key = (name, round(lat, 5), round(lon, 5))
        if key in seen:
            continue
        seen.add(key)
        address_parts = [tags.get("addr:district"), tags.get("addr:street"), tags.get("addr:housenumber")]
        address = _clean("".join(str(part or "") for part in address_parts), 100)
        places.append({
            "name": name,
            "category": _category_for_tags(tags, requested_categories),
            "distance_m": haversine_m(ref_lat, ref_lon, lat, lon),
            "address_summary": address,
            "map_url": f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=18/{lat:.6f}/{lon:.6f}",
        })
    return sorted(places, key=lambda item: (item["distance_m"], item["name"]))


def nearby_places(anchor: str, categories: list[str], *, radius_m: int = 1500, limit: int = 8) -> dict[str, Any]:
    anchor_point = nominatim_search(anchor)
    categories = [category for category in categories if category in _CATEGORY_TAGS][:3]
    if not categories:
        raise MapClientError("invalid_place_category")
    radius = max(300, min(int(radius_m), MAX_RADIUS_METERS))
    safe_limit = max(1, min(int(limit), MAX_RESULTS))
    key = _cache_key("nearby", {"lat": round(anchor_point["lat"], 5), "lon": round(anchor_point["lon"], 5), "categories": tuple(categories), "radius": radius})
    cached = _cache_get(key)
    if cached:
        return {**cached, "places": list(cached.get("places") or [])[:safe_limit]}
    raw = overpass_query(build_overpass_nearby(categories, anchor_point["lat"], anchor_point["lon"], radius))
    data = {
        "anchor_label": anchor_point["label"],
        "distance_basis": "straight_line",
        "attribution": "© OpenStreetMap contributors",
        "attribution_url": "https://www.openstreetmap.org/copyright",
        "places": parse_overpass_elements(raw.get("elements") or [], ref_lat=anchor_point["lat"], ref_lon=anchor_point["lon"], requested_categories=categories)[:MAX_RESULTS],
    }
    _cache_put(key, data, NEARBY_TTL_SECONDS)
    return {**data, "places": data["places"][:safe_limit]}


def measure_distance(origin: str, destination: str) -> dict[str, Any]:
    left, right = nominatim_search(origin), nominatim_search(destination)
    return {
        "origin_label": left["label"],
        "destination_label": right["label"],
        "distance_m": haversine_m(left["lat"], left["lon"], right["lat"], right["lon"]),
        "distance_basis": "straight_line",
        "attribution": "© OpenStreetMap contributors",
        "attribution_url": "https://www.openstreetmap.org/copyright",
    }
