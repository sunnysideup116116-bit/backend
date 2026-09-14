from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


SERVER_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(SERVER_ROOT / ".env", override=False)
load_dotenv(SERVER_ROOT / "social" / ".env", override=False)

_TRUE = {"1", "true", "on", "yes"}
_PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
_WEATHER_URL = "https://weather.googleapis.com/v1/currentConditions:lookup"
_AIR_QUALITY_URL = (
    "https://airquality.googleapis.com/v1/currentConditions:lookup"
)


class VoiceWeatherError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class VoiceWeatherSettings:
    places_api_key: str
    weather_api_key: str
    air_quality_api_key: str
    timeout_seconds: float = 10

    @classmethod
    def from_env_or_none(cls) -> "VoiceWeatherSettings | None":
        enabled = os.getenv("VOICE_WEATHER_ENABLED", "on").strip().lower()
        if enabled not in _TRUE:
            return None
        shared_key = os.getenv("GOOGLE_PLACES_SERVER_API_KEY", "").strip()
        places_key = (
            os.getenv("GOOGLE_WEATHER_GEOCODING_API_KEY", "").strip()
            or shared_key
        )
        weather_key = (
            os.getenv("GOOGLE_WEATHER_API_KEY", "").strip()
            or shared_key
        )
        air_key = (
            os.getenv("GOOGLE_AIR_QUALITY_API_KEY", "").strip()
            or shared_key
        )
        if not places_key or not weather_key or not air_key:
            return None
        try:
            timeout = float(os.getenv("VOICE_WEATHER_TIMEOUT_SECONDS", "10"))
        except (TypeError, ValueError):
            timeout = 10
        return cls(
            places_api_key=places_key,
            weather_api_key=weather_key,
            air_quality_api_key=air_key,
            timeout_seconds=max(3, min(timeout, 20)),
        )


def _clean(value: Any, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


async def resolve_weather_location(
    explicit_location: Any,
    user_id: str,
    provider: Any | None,
    *,
    timeout_seconds: float = 1.0,
) -> str:
    explicit = _clean(explicit_location, 120)
    if explicit or provider is None:
        return explicit
    try:
        saved = await asyncio.wait_for(
            asyncio.to_thread(provider, user_id),
            timeout=max(0.1, min(float(timeout_seconds), 2.0)),
        )
    except Exception:
        return ""
    return _clean(saved, 120)


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 1)


def _format_number(value: Any) -> str:
    number = _number(value)
    if number is None:
        return ""
    return str(int(number)) if float(number).is_integer() else str(number)


class GoogleVoiceWeatherService:
    """Server-only Google Weather + Air Quality current-condition adapter."""

    def __init__(
        self,
        settings: VoiceWeatherSettings,
        *,
        http_session: Any = requests,
    ):
        self.settings = settings
        self.http = http_session

    @classmethod
    def from_env_or_none(cls) -> "GoogleVoiceWeatherService | None":
        settings = VoiceWeatherSettings.from_env_or_none()
        return cls(settings) if settings is not None else None

    def _request_json(
        self,
        source: str,
        method: str,
        url: str,
        *,
        api_key: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_headers = dict(headers or {})
        request_params = dict(params or {})
        if source == "places":
            request_headers["X-Goog-Api-Key"] = api_key
        else:
            request_params["key"] = api_key
        try:
            response = self.http.request(
                method,
                url,
                headers=request_headers,
                params=request_params,
                json=data,
                timeout=(3, self.settings.timeout_seconds),
                allow_redirects=False,
            )
        except requests.Timeout as error:
            raise VoiceWeatherError(f"google_{source}_timeout") from error
        except requests.RequestException as error:
            raise VoiceWeatherError(f"google_{source}_unavailable") from error
        if response.status_code in {401, 403}:
            raise VoiceWeatherError(f"google_{source}_access_denied")
        if response.status_code == 429:
            raise VoiceWeatherError(f"google_{source}_rate_limited")
        if response.status_code < 200 or response.status_code >= 300:
            raise VoiceWeatherError(f"google_{source}_unavailable")
        try:
            payload = response.json()
        except ValueError as error:
            raise VoiceWeatherError(f"google_{source}_invalid_response") from error
        if not isinstance(payload, dict):
            raise VoiceWeatherError(f"google_{source}_invalid_response")
        return payload

    def _resolve_location(self, location: str) -> dict[str, Any]:
        payload = self._request_json(
            "places",
            "POST",
            _PLACES_URL,
            api_key=self.settings.places_api_key,
            headers={
                "Content-Type": "application/json",
                "X-Goog-FieldMask": (
                    "places.displayName,places.formattedAddress,places.location"
                ),
            },
            data={
                "textQuery": location,
                "languageCode": "zh-TW",
                "maxResultCount": 1,
            },
        )
        places = payload.get("places")
        item = places[0] if isinstance(places, list) and places else None
        if not isinstance(item, dict):
            raise VoiceWeatherError("weather_location_not_found")
        coordinates = item.get("location")
        if not isinstance(coordinates, dict):
            raise VoiceWeatherError("weather_location_not_found")
        try:
            latitude = float(coordinates["latitude"])
            longitude = float(coordinates["longitude"])
        except (KeyError, TypeError, ValueError) as error:
            raise VoiceWeatherError("weather_location_not_found") from error
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise VoiceWeatherError("weather_location_not_found")
        display = item.get("displayName")
        name = _clean(
            display.get("text") if isinstance(display, dict) else display,
            120,
        )
        address = _clean(item.get("formattedAddress"), 180)
        return {
            "name": name or address or location,
            "address": address,
            "latitude": latitude,
            "longitude": longitude,
        }

    def _weather(self, latitude: float, longitude: float) -> dict[str, Any]:
        return self._request_json(
            "weather",
            "GET",
            _WEATHER_URL,
            api_key=self.settings.weather_api_key,
            params={
                "location.latitude": latitude,
                "location.longitude": longitude,
                "unitsSystem": "METRIC",
                "languageCode": "zh-TW",
            },
        )

    def _air_quality(self, latitude: float, longitude: float) -> dict[str, Any]:
        return self._request_json(
            "air_quality",
            "POST",
            _AIR_QUALITY_URL,
            api_key=self.settings.air_quality_api_key,
            headers={"Content-Type": "application/json"},
            data={
                "location": {
                    "latitude": latitude,
                    "longitude": longitude,
                },
                "universalAqi": True,
                "extraComputations": [
                    "LOCAL_AQI",
                    "HEALTH_RECOMMENDATIONS",
                ],
                "languageCode": "zh-TW",
            },
        )

    @staticmethod
    def _weather_projection(payload: dict[str, Any]) -> dict[str, Any]:
        condition = payload.get("weatherCondition")
        description = condition.get("description") if isinstance(condition, dict) else {}
        temperature = payload.get("temperature")
        feels_like = payload.get("feelsLikeTemperature")
        precipitation = payload.get("precipitation")
        probability = (
            precipitation.get("probability")
            if isinstance(precipitation, dict)
            else {}
        )
        wind = payload.get("wind")
        wind_speed = wind.get("speed") if isinstance(wind, dict) else {}
        result = {
            "condition": _clean(
                description.get("text") if isinstance(description, dict) else "",
                80,
            ),
            "temperature_c": _number(
                temperature.get("degrees") if isinstance(temperature, dict) else None
            ),
            "feels_like_c": _number(
                feels_like.get("degrees") if isinstance(feels_like, dict) else None
            ),
            "humidity_percent": _number(payload.get("relativeHumidity")),
            "precipitation_probability_percent": _number(
                probability.get("percent") if isinstance(probability, dict) else None
            ),
            "wind_kph": _number(
                wind_speed.get("value") if isinstance(wind_speed, dict) else None
            ),
            "uv_index": _number(payload.get("uvIndex")),
            "observed_at": _clean(payload.get("currentTime"), 50),
        }
        if not result["condition"] and result["temperature_c"] is None:
            raise VoiceWeatherError("google_weather_invalid_response")
        return result

    @staticmethod
    def _air_projection(payload: dict[str, Any]) -> dict[str, Any]:
        raw_indexes = payload.get("indexes")
        indexes = [
            item for item in (raw_indexes if isinstance(raw_indexes, list) else [])
            if isinstance(item, dict)
        ]
        selected = next(
            (item for item in indexes if str(item.get("code") or "") != "uaqi"),
            indexes[0] if indexes else None,
        )
        if not isinstance(selected, dict):
            raise VoiceWeatherError("google_air_quality_invalid_response")
        health = payload.get("healthRecommendations")
        return {
            "index_name": _clean(selected.get("displayName"), 80),
            "aqi": _number(selected.get("aqi")),
            "category": _clean(selected.get("category"), 80),
            "dominant_pollutant": _clean(selected.get("dominantPollutant"), 40),
            "general_health_advice": _clean(
                health.get("generalPopulation") if isinstance(health, dict) else "",
                300,
            ),
            "observed_at": _clean(payload.get("dateTime"), 50),
        }

    @staticmethod
    def _message(
        location: dict[str, Any],
        weather: dict[str, Any] | None,
        air: dict[str, Any] | None,
        errors: dict[str, str],
    ) -> str:
        label = _clean(location.get("name") or location.get("address"), 80)
        parts: list[str] = []
        if weather is not None:
            weather_bits = [f"{label}目前{weather['condition']}".strip()]
            temperature = _format_number(weather.get("temperature_c"))
            if temperature:
                weather_bits.append(f"{temperature}°C")
            feels = _format_number(weather.get("feels_like_c"))
            if feels:
                weather_bits.append(f"體感 {feels}°C")
            rain = _format_number(
                weather.get("precipitation_probability_percent"),
            )
            if rain:
                weather_bits.append(f"降雨機率 {rain}%")
            humidity = _format_number(weather.get("humidity_percent"))
            if humidity:
                weather_bits.append(f"濕度 {humidity}%")
            parts.append("，".join(bit for bit in weather_bits if bit))
        if air is not None:
            air_bits = ["空氣品質"]
            if air.get("index_name"):
                air_bits.append(str(air["index_name"]))
            aqi = _format_number(air.get("aqi"))
            if aqi:
                air_bits.append(aqi)
            if air.get("category"):
                air_bits.append(f"（{air['category']}）")
            parts.append(" ".join(air_bits))
            if air.get("general_health_advice"):
                parts.append(str(air["general_health_advice"]))
        if errors:
            missing = "與".join(
                "天氣" if source == "weather" else "空氣品質"
                for source in errors
            )
            parts.append(f"{missing}資料目前暫時不完整")
        rendered = "。".join(part.rstrip("。") for part in parts if part)[:900]
        return (
            rendered
            if rendered.endswith(("。", "！", "？", "!", "?"))
            else f"{rendered}。"
        )

    def query(self, location: str) -> dict[str, Any]:
        requested = _clean(location, 120)
        if len(requested) < 2:
            return {
                "status": "needs_input",
                "error_code": "weather_location_required",
                "message": "請告訴我城市或區域，例如「台北市信義區」。",
            }
        try:
            resolved = self._resolve_location(requested)
        except VoiceWeatherError as error:
            message = (
                "我找不到這個地點，請改說城市加區域。"
                if error.code == "weather_location_not_found"
                else "目前無法解析地點，請稍後再試。"
            )
            return {"status": "failed", "error_code": error.code, "message": message}

        latitude = float(resolved["latitude"])
        longitude = float(resolved["longitude"])
        payloads: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                "weather": pool.submit(self._weather, latitude, longitude),
                "air_quality": pool.submit(
                    self._air_quality, latitude, longitude,
                ),
            }
            for source, future in futures.items():
                try:
                    payloads[source] = future.result()
                except VoiceWeatherError as error:
                    errors[source] = error.code
                except Exception:
                    errors[source] = f"google_{source}_unavailable"

        weather: dict[str, Any] | None = None
        air: dict[str, Any] | None = None
        if "weather" in payloads:
            try:
                weather = self._weather_projection(payloads["weather"])
            except VoiceWeatherError as error:
                errors["weather"] = error.code
        if "air_quality" in payloads:
            try:
                air = self._air_projection(payloads["air_quality"])
            except VoiceWeatherError as error:
                errors["air_quality"] = error.code
        if weather is None and air is None:
            return {
                "status": "failed",
                "error_code": "google_weather_and_air_quality_unavailable",
                "message": "目前暫時查不到天氣與空氣品質，請稍後再試。",
                "sources_called": ["google_weather", "google_air_quality"],
            }
        return {
            "status": "partial" if errors else "ok",
            "location": {
                "name": resolved["name"],
                "address": resolved["address"],
            },
            "weather": weather,
            "air_quality": air,
            "source_errors": errors,
            "sources_called": ["google_weather", "google_air_quality"],
            "message": self._message(resolved, weather, air, errors),
        }
