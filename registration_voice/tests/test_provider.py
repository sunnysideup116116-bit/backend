import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

from google import genai

from registration_voice.provider import GeminiLiveProvider, _GoogleLiveConnection
from registration_voice.settings import VoiceRegistrationSettings


class FakeRawSession:
    async def receive(self):
        if False:
            yield None


class FakeTranscriptSession:
    def __init__(self):
        self.receive_calls = 0

    async def receive(self):
        self.receive_calls += 1
        if self.receive_calls > 1:
            return
        content = SimpleNamespace(
            interim_input_transcription=SimpleNamespace(text="我是小"),
            input_transcription=SimpleNamespace(text="我是小歌"),
            turn_complete=True,
        )
        yield SimpleNamespace(server_content=content, tool_call=None)


class FakeLiveApi:
    def __init__(self):
        self.model = None
        self.config = None

    @asynccontextmanager
    async def connect(self, *, model, config):
        self.model = model
        self.config = config
        yield FakeRawSession()


class FakeClient:
    instances = []

    def __init__(self, *, api_key, http_options):
        self.api_key = api_key
        self.http_options = http_options
        self.live = FakeLiveApi()
        self.aio = SimpleNamespace(live=self.live)
        self.closed = False
        self.instances.append(self)

    def close(self):
        self.closed = True


def test_google_provider_builds_live_function_call_config_without_network(monkeypatch):
    async def scenario():
        monkeypatch.setattr(genai, "Client", FakeClient)
        settings = replace(
            VoiceRegistrationSettings.from_env({}),
            model="gemini-3.1-flash-live-preview",
            thinking_level="low",
        )
        provider = GeminiLiveProvider(settings)

        async with provider.connect("secret", {"nickname": None}, 7):
            pass

        client = FakeClient.instances[-1]
        assert client.api_key == "secret"
        assert client.live.model == "gemini-3.1-flash-live-preview"
        assert client.live.config.input_audio_transcription is None
        assert client.live.config.thinking_config.thinking_level.value == "LOW"
        activity = client.live.config.realtime_input_config.automatic_activity_detection
        assert activity.silence_duration_ms == 500
        declaration = client.live.config.tools[0].function_declarations[0]
        assert declaration.name == "propose_registration_patch"
        assert "password" not in declaration.parameters_json_schema["properties"]
        warnings = declaration.parameters_json_schema["properties"]["warning_codes"]
        assert "unsupported_language" in warnings["items"]["enum"]
        instruction = str(client.live.config.system_instruction)
        assert "Taiwan Mandarin and English" in instruction
        assert "Traditional Chinese" in instruction
        assert client.closed is True

    asyncio.run(scenario())


def test_provider_reopens_sdk_receive_iterator_after_each_completed_turn():
    class FakeMultiTurnSession:
        def __init__(self):
            self.receive_calls = 0

        async def receive(self):
            self.receive_calls += 1
            if self.receive_calls > 2:
                return
            turn = self.receive_calls
            content = SimpleNamespace(
                interim_input_transcription=None,
                input_transcription=SimpleNamespace(text=f"turn-{turn}"),
                turn_complete=True,
            )
            yield SimpleNamespace(server_content=content, tool_call=None)

    async def scenario():
        session = FakeMultiTurnSession()
        connection = _GoogleLiveConnection(session, SimpleNamespace())
        events = [event async for event in connection.events()]

        assert [(event.type, event.text) for event in events] == [
            ("transcript", "turn-1"),
            ("turn_complete", ""),
            ("transcript", "turn-2"),
            ("turn_complete", ""),
        ]
        assert session.receive_calls == 3

    asyncio.run(scenario())


def test_provider_keeps_interim_and_final_transcripts_from_same_server_event():
    async def scenario():
        connection = _GoogleLiveConnection(FakeTranscriptSession(), SimpleNamespace())
        events = [event async for event in connection.events()]

        assert [(event.type, event.text, event.final) for event in events] == [
            ("transcript", "我是小", False),
            ("transcript", "我是小歌", True),
            ("turn_complete", "", True),
        ]

    asyncio.run(scenario())
