import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

import app_voice_assistant.weather as weather_module
from app_voice_assistant.contracts import (
    deterministic_proposal,
    requires_confirmation,
    validate_proposal,
)
from app_voice_assistant.duplex_runtime import run_duplex_session
from app_voice_assistant.router import (
    AppVoiceRuntime,
    AppVoiceSessionRequest,
    _saved_weather_location,
    create_router,
)
from app_voice_assistant.weather import (
    GoogleVoiceWeatherService,
    VoiceWeatherSettings,
    resolve_weather_location,
)
from .test_duplex_runtime import (
    FakeLimiter,
    FakeLive,
    FakeProvider,
    FakeWebSocket,
    _append,
    message,
    wait_until,
)
from .test_api import FakeProvider as LegacyFakeProvider
from .test_api import FakeWebSocket as LegacyFakeWebSocket
from .test_api import settings as app_voice_settings


class InlineFuture:
    def __init__(self, function, *args):
        try:
            self.value = function(*args)
            self.error = None
        except Exception as error:
            self.value = None
            self.error = error

    def result(self):
        if self.error is not None:
            raise self.error
        return self.value


class InlineExecutor:
    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def submit(self, function, *args):
        return InlineFuture(function, *args)


@pytest.fixture(autouse=True)
def run_threads_inline(monkeypatch):
    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(asyncio, "to_thread", run_inline)
    monkeypatch.setattr(weather_module, "ThreadPoolExecutor", InlineExecutor)


class Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload


class WeatherHttp:
    def __init__(self, *, weather_status=200, air_status=200):
        self.calls = []
        self.weather_status = weather_status
        self.air_status = air_status

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if "places.googleapis.com" in url:
            return Response(200, {
                "places": [{
                    "displayName": {"text": "臺北市信義區"},
                    "formattedAddress": "台灣臺北市信義區",
                    "location": {"latitude": 25.033, "longitude": 121.5654},
                }],
            })
        if "weather.googleapis.com" in url:
            return Response(self.weather_status, {
                "currentTime": "2026-09-13T01:00:00Z",
                "weatherCondition": {"description": {"text": "多雲時晴"}},
                "temperature": {"degrees": 29.5},
                "feelsLikeTemperature": {"degrees": 34.8},
                "relativeHumidity": 68,
                "precipitation": {"probability": {"percent": 20}},
                "wind": {"speed": {"value": 13}},
                "uvIndex": 5,
            })
        if "airquality.googleapis.com" in url:
            return Response(self.air_status, {
                "dateTime": "2026-09-13T01:00:00Z",
                "indexes": [
                    {"code": "uaqi", "displayName": "Universal AQI", "aqi": 82},
                    {
                        "code": "twn_epa",
                        "displayName": "AQI (TW)",
                        "aqi": 33,
                        "category": "良好",
                        "dominantPollutant": "no2",
                    },
                ],
                "healthRecommendations": {"generalPopulation": "可以正常戶外活動。"},
            })
        raise AssertionError(url)


def settings():
    return VoiceWeatherSettings(
        places_api_key="places-key",
        weather_api_key="weather-key",
        air_quality_api_key="air-key",
        timeout_seconds=5,
    )


def test_each_weather_query_calls_google_weather_and_air_quality_once():
    http = WeatherHttp()
    service = GoogleVoiceWeatherService(settings(), http_session=http)

    result = service.query("台北市信義區")

    assert result["status"] == "ok"
    assert result["weather"]["temperature_c"] == 29.5
    assert result["air_quality"]["aqi"] == 33
    assert result["sources_called"] == ["google_weather", "google_air_quality"]
    assert "多雲時晴" in result["message"]
    assert "AQI (TW) 33" in result["message"]
    assert not result["message"].endswith("！。")
    urls = [url for _, url, _ in http.calls]
    assert sum("weather.googleapis.com" in url for url in urls) == 1
    assert sum("airquality.googleapis.com" in url for url in urls) == 1
    air_call = next(call for call in http.calls if "airquality" in call[1])
    assert air_call[2]["json"]["extraComputations"] == [
        "LOCAL_AQI", "HEALTH_RECOMMENDATIONS",
    ]


def test_one_google_source_failure_still_calls_and_returns_the_other():
    http = WeatherHttp(weather_status=500)
    service = GoogleVoiceWeatherService(settings(), http_session=http)

    result = service.query("台北")

    assert result["status"] == "partial"
    assert result["weather"] is None
    assert result["air_quality"]["aqi"] == 33
    assert set(result["source_errors"]) == {"weather"}
    urls = [url for _, url, _ in http.calls]
    assert any("weather.googleapis.com" in url for url in urls)
    assert any("airquality.googleapis.com" in url for url in urls)


def test_weather_requires_a_location_before_any_google_call():
    http = WeatherHttp()
    service = GoogleVoiceWeatherService(settings(), http_session=http)

    result = service.query("")

    assert result["status"] == "needs_input"
    assert result["error_code"] == "weather_location_required"
    assert http.calls == []


def test_weather_intent_is_narrow_and_never_requires_confirmation():
    proposal = deterministic_proposal("幫我查台北今天的天氣如何", context={})
    assert proposal.intent == "weather.query"
    assert proposal.arguments == {"location": "台北"}
    assert not requires_confirmation(proposal)

    current = deterministic_proposal("今天天氣如何", context={})
    assert current.intent == "weather.query"
    assert current.arguments == {"location": ""}
    assert deterministic_proposal("我喜歡這種天氣", context={}) is None
    assert deterministic_proposal("今天天氣很好", context={}) is None
    post = deterministic_proposal(
        "幫我寫一篇天氣很好的貼文",
        context={},
        generated_caption="今天陽光很好。",
    )
    assert post.intent == "post.open_draft"

    validated = validate_proposal({
        "intent": "weather.query",
        "arguments": {"location": "高雄", "api_key": "forged"},
    }, base_revision=3)
    assert validated.arguments == {"location": "高雄"}


class FakeWeather:
    def __init__(self):
        self.locations = []

    def query(self, location):
        self.locations.append(location)
        return {
            "status": "ok",
            "sources_called": ["google_weather", "google_air_quality"],
            "weather": {"temperature_c": 29},
            "air_quality": {"aqi": 33},
            "message": "台北目前 29°C，空氣品質良好。",
        }


def test_saved_weather_location_prefers_manual_profile_location(monkeypatch):
    class Profiles:
        def find_one(self, query, projection):
            assert query == {"user_id": "owner"}
            assert projection == {"_id": 0, "profile_location": 1}
            return {
                "profile_location": {"city": "高雄市", "district": "鹽埕區"},
            }

    monkeypatch.setitem(
        sys.modules,
        "database",
        SimpleNamespace(profiles_coll=Profiles()),
    )

    assert _saved_weather_location("owner") == "高雄市鹽埕區"


def test_saved_weather_location_does_not_use_unrelated_profile_fields(monkeypatch):
    class Profiles:
        def find_one(self, query, projection):
            raise RuntimeError("mongo unavailable")

    monkeypatch.setitem(
        sys.modules,
        "database",
        SimpleNamespace(profiles_coll=Profiles()),
    )

    assert _saved_weather_location("owner") == ""


def test_agent_location_is_loaded_only_when_weather_has_no_explicit_place(
    monkeypatch,
):
    class Profiles:
        def __init__(self):
            self.calls = 0

        def find_one(self, query, projection):
            self.calls += 1
            return {
                "profile_location": {"city": "高雄市", "district": "鹽埕區"},
            }

    profiles = Profiles()
    monkeypatch.setitem(
        sys.modules,
        "database",
        SimpleNamespace(profiles_coll=profiles),
    )
    explicit = asyncio.run(resolve_weather_location(
        "台北市信義區",
        "owner",
        _saved_weather_location,
    ))
    default = asyncio.run(resolve_weather_location(
        "",
        "owner",
        _saved_weather_location,
    ))

    assert explicit == "台北市信義區"
    assert default == "高雄市鹽埕區"
    assert profiles.calls == 1


def test_live_weather_tool_returns_both_google_sources_without_app_action():
    async def scenario():
        live, socket, events = FakeLive(), FakeWebSocket(), []
        weather = FakeWeather()
        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=FakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={},
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
            weather_service=weather,
        ))
        try:
            await live.incoming.put(message(tool_calls=[SimpleNamespace(
                id="weather-call",
                name="read_weather",
                args={"location": "台北"},
            )]))
            await wait_until(lambda: any(
                response[0] == "weather-call" for response in live.tool_responses
            ))
            response = next(
                item[2] for item in live.tool_responses
                if item[0] == "weather-call"
            )
            assert weather.locations == ["台北"]
            assert response["sources_called"] == [
                "google_weather", "google_air_quality",
            ]
            assert not any(
                event.get("type") == "action_proposal" for event in events
            )
        finally:
            await socket.incoming.put({
                "type": "websocket.receive",
                "text": json.dumps({"type": "stop"}),
            })
            await task

    asyncio.run(scenario())


def test_live_weather_uses_saved_default_but_explicit_location_wins():
    async def scenario():
        live, socket, events = FakeLive(), FakeWebSocket(), []
        weather = FakeWeather()
        location_requests = []

        def saved_location(user_id):
            location_requests.append(user_id)
            return "高雄市鹽埕區"

        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=FakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={},
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
            weather_service=weather,
            weather_location_provider=saved_location,
            weather_user_id="owner",
        ))
        try:
            await live.incoming.put(message(tool_calls=[SimpleNamespace(
                id="weather-default",
                name="read_weather",
                args={},
            )]))
            await wait_until(lambda: any(
                response[0] == "weather-default"
                for response in live.tool_responses
            ))
            await live.incoming.put(message(tool_calls=[SimpleNamespace(
                id="weather-explicit",
                name="read_weather",
                args={"location": "台北市信義區"},
            )]))
            await wait_until(lambda: any(
                response[0] == "weather-explicit"
                for response in live.tool_responses
            ))
            assert weather.locations == ["高雄市鹽埕區", "台北市信義區"]
            assert location_requests == ["owner"]
        finally:
            await socket.incoming.put({
                "type": "websocket.receive",
                "text": json.dumps({"type": "stop"}),
            })
            await task

    asyncio.run(scenario())


def test_legacy_session_loads_agent_location_only_for_default_weather():
    async def scenario():
        weather = FakeWeather()
        location_requests = []

        def saved_location(user_id):
            location_requests.append(user_id)
            return "高雄市鹽埕區"

        runtime = AppVoiceRuntime(
            settings=app_voice_settings(),
            keys=[],
            provider=LegacyFakeProvider(),
            weather_service=weather,
            weather_location_provider=saved_location,
        )
        app = FastAPI()
        app.include_router(create_router(runtime))
        routes = {
            route.path: route.endpoint for route in app.routes
            if hasattr(route, "endpoint")
        }
        issued = await routes["/api/app-voice/session"](
            AppVoiceSessionRequest(
                installation_id="installation-id-123456",
                user_id="test-user-id",
                consent_version=runtime.settings.consent_version,
                consent_accepted_at="2026-09-13T12:00:00Z",
            ),
            SimpleNamespace(
                headers={},
                client=SimpleNamespace(host="127.0.0.1"),
            ),
        )
        assert location_requests == []
        socket = LegacyFakeWebSocket(
            {
                "type": "hello",
                "ticket": issued["ticket"],
                "input_mode": "on_device_text",
                "output_mode": "gemini_tts",
                "context": {"scope": "global", "revision": 0},
            },
            [
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "utterance",
                    "text": "今天天氣如何",
                })},
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "stop",
                })},
            ],
        )

        await routes["/api/app-voice"](socket)

        assert location_requests == ["test-user-id"]
        assert weather.locations == ["高雄市鹽埕區"]

    asyncio.run(scenario())


def test_capability_advertises_both_google_weather_sources():
    runtime = AppVoiceRuntime(
        settings=app_voice_settings(),
        keys=[],
        provider=LegacyFakeProvider(),
        weather_service=FakeWeather(),
    )
    app = FastAPI()
    app.include_router(create_router(runtime))
    route = next(
        route.endpoint for route in app.routes
        if route.path == "/api/app-voice/capability"
    )

    capability = asyncio.run(route())

    assert capability["voice_weather"] is True
    assert capability["voice_weather_sources"] == [
        "google_weather", "google_air_quality",
    ]
    assert "current_weather_and_air_quality" in capability["supported_intents"]
