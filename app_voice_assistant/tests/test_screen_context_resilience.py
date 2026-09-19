import asyncio
import json

import pytest

from app_voice_assistant import duplex_runtime
from app_voice_assistant.tests.test_duplex_runtime import (
    FakeLive, FakeWebSocket, FakeProvider, FakeLimiter, wait_until, _append,
)
from app_voice_assistant.tests.test_screen_context import context


async def update(socket, revision):
    ctx = context()
    ctx['revision'] = revision
    await socket.incoming.put({'type': 'websocket.receive', 'text': json.dumps({
        'type': 'context_changed', 'context': ctx,
    })})


def start(live, socket, events):
    return asyncio.create_task(duplex_runtime.run_duplex_session(
        socket, provider=FakeProvider(live), limiter=FakeLimiter(),
        identity='test', initial_context=context(), max_session_seconds=30,
        send_event=lambda e: _append(events, e),
    ))


@pytest.mark.parametrize('failure', ['blocked', 'error', 'startup'])
def test_screen_failure_releases_shared_transport_and_keeps_voice_alive(monkeypatch, failure):
    monkeypatch.setattr(duplex_runtime, 'SCREEN_CONTEXT_TIMEOUT_SECONDS', .05)
    monkeypatch.setattr(duplex_runtime, 'SCREEN_CONTEXT_COALESCE_SECONDS', .001)

    class SlowScreen(FakeLive):
        def __init__(self):
            super().__init__()
            self.lock = asyncio.Lock()
            self.started = asyncio.Event()
            self.cancelled = False
            self.fail = failure == 'startup'

        async def send_screen_context(self, payload):
            async with self.lock:
                if self.fail:
                    self.started.set()
                    if failure == 'error':
                        raise RuntimeError('screen unavailable')
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        self.cancelled = True
                        raise
                await super().send_screen_context(payload)

        async def send_audio(self, data):
            async with self.lock:
                await super().send_audio(data)

    async def scenario():
        live, socket, events = SlowScreen(), FakeWebSocket(), []
        task = start(live, socket, events)
        try:
            if failure != 'startup':
                await wait_until(lambda: live.text)
                live.fail = True
                await update(socket, 1)
            await asyncio.wait_for(live.started.wait(), .5)
            await socket.incoming.put({'type': 'websocket.receive', 'bytes': b'audio'})
            await wait_until(lambda: live.audio == [b'audio'])
            assert not task.done()
            if failure != 'error':
                assert live.cancelled
            live.fail = False
            await update(socket, 2)
            await wait_until(lambda: getattr(live, 'screen_contexts', [])
                             and json.loads(live.screen_contexts[-1])['revision'] == 2)
        finally:
            await socket.incoming.put({'type': 'websocket.disconnect'})
            await asyncio.wait_for(task, .5)
        assert live.closed

    asyncio.run(scenario())


def test_screen_burst_coalesces_to_latest_and_does_not_delay_audio(monkeypatch):
    monkeypatch.setattr(duplex_runtime, 'SCREEN_CONTEXT_COALESCE_SECONDS', .04)

    async def scenario():
        live, socket, events = FakeLive(), FakeWebSocket(), []
        task = start(live, socket, events)
        try:
            await wait_until(lambda: live.text)
            for revision in range(1, 31):
                await update(socket, revision)
            await socket.incoming.put({'type': 'websocket.receive', 'bytes': b'audio'})
            await wait_until(lambda: live.audio == [b'audio'])
            assert len(live.screen_contexts) == 1
            await wait_until(lambda: len(live.screen_contexts) == 2)
            assert json.loads(live.screen_contexts[-1])['revision'] == 30
        finally:
            await socket.incoming.put({'type': 'websocket.disconnect'})
            await task

    asyncio.run(scenario())


def test_disconnect_cancels_pending_screen_send(monkeypatch):
    monkeypatch.setattr(duplex_runtime, 'SCREEN_CONTEXT_TIMEOUT_SECONDS', 5)
    monkeypatch.setattr(duplex_runtime, 'SCREEN_CONTEXT_COALESCE_SECONDS', .001)

    class BlockedScreen(FakeLive):
        started = None
        cancelled = False

        async def send_screen_context(self, payload):
            if json.loads(payload)['revision']:
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
            await super().send_screen_context(payload)

    async def scenario():
        live, socket, events = BlockedScreen(), FakeWebSocket(), []
        live.started = asyncio.Event()
        task = start(live, socket, events)
        await wait_until(lambda: live.text)
        await update(socket, 1)
        await asyncio.wait_for(live.started.wait(), .5)
        await socket.incoming.put({'type': 'websocket.disconnect'})
        await asyncio.wait_for(task, .2)
        assert live.cancelled and live.closed

    asyncio.run(scenario())
