"""Opt-in end-to-end smoke through the running public HTTP/WebSocket routes."""

from __future__ import annotations

import asyncio
import json
import os

import httpx
from websockets.asyncio.client import connect

from .live_smoke import _generate_synthetic_audio
from .settings import collect_google_api_keys


async def _run() -> dict:
    if os.getenv("VOICE_LIVE_SMOKE", "").strip().lower() not in {"1", "true", "on"}:
        raise RuntimeError("Set VOICE_LIVE_SMOKE=1 to authorize the live smoke test")
    keys = collect_google_api_keys()
    if not keys:
        raise RuntimeError("No Google API key is configured")
    audio = await asyncio.to_thread(_generate_synthetic_audio, keys[0])
    try:
        hold_seconds = float(os.getenv("VOICE_SMOKE_HOLD_SECONDS", "0"))
    except ValueError:
        hold_seconds = 0
    hold_seconds = max(0.0, min(hold_seconds, 90.0))
    try:
        requested_turns = int(os.getenv("VOICE_SMOKE_TURNS", "1"))
    except ValueError:
        requested_turns = 1
    requested_turns = max(1, min(requested_turns, 3))
    use_public_url = os.getenv("VOICE_SMOKE_PUBLIC", "").strip().lower() in {
        "1", "true", "on",
    }
    http_base = (
        "https://service.misproject.us.ci" if use_public_url else "http://127.0.0.1:8000"
    )
    websocket_url = (
        "wss://service.misproject.us.ci/api/registration/voice"
        if use_public_url
        else "ws://127.0.0.1:8000/api/registration/voice"
    )

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"{http_base}/api/registration/voice/session",
            json={
                "installation_id": "synthetic-server-smoke-0001",
                "consent_version": "free-tier-voice-v1",
                "consent_accepted_at": "2026-09-10T12:00:00Z",
            },
        )
        response.raise_for_status()
        ticket = response.json()["ticket"]

    combined_changes = {}
    combined_rejected = []
    combined_warnings = []
    patch_count = 0
    provider_reconnects = 0

    def collect(message: dict) -> None:
        nonlocal patch_count, provider_reconnects
        if message.get("type") == "form_patch":
            patch_count += 1
            combined_changes.update(message.get("changes") or {})
            combined_rejected.extend(message.get("rejected") or [])
            combined_warnings.extend(message.get("warnings") or [])
        elif message.get("type") == "provider_reconnecting":
            provider_reconnects += 1

    async with connect(websocket_url, max_size=1_000_000) as socket:
        await socket.send(json.dumps({
            "type": "hello",
            "ticket": ticket,
            "revision": 0,
            "form": {},
        }))
        async with asyncio.timeout(25):
            while True:
                message = json.loads(await socket.recv())
                if message.get("type") == "ready":
                    break
                if message.get("type") == "provider_unavailable":
                    raise RuntimeError("Server could not open a Gemini Live session")

        for turn_index in range(requested_turns):
            if turn_index == 0 and hold_seconds:
                deadline = asyncio.get_running_loop().time() + hold_seconds
                silent_pcm = bytes(3200)
                while asyncio.get_running_loop().time() < deadline:
                    await socket.send(silent_pcm)
                    await asyncio.sleep(0.1)
            for offset in range(0, len(audio), 3200):
                await socket.send(audio[offset : offset + 3200])
                await asyncio.sleep(0.08)
            for _ in range(10):
                await socket.send(bytes(3200))
                await asyncio.sleep(0.1)

            if turn_index + 1 < requested_turns:
                target_patch_count = patch_count + 1
                async with asyncio.timeout(40):
                    while patch_count < target_patch_count:
                        message = json.loads(await socket.recv())
                        collect(message)
                        if message.get("type") == "provider_unavailable":
                            raise RuntimeError("Provider became unavailable between turns")
                await asyncio.sleep(0.5)

        await socket.send(json.dumps({"type": "stop"}))

        async with asyncio.timeout(40):
            while True:
                message = json.loads(await socket.recv())
                collect(message)
                if message.get("type") == "closed":
                    break

    changes = dict(combined_changes)
    checks = {
        "nickname": changes.get("nickname") == "小歌",
        "age": changes.get("age") == 25,
        "region": changes.get("region") == "台東縣",
        "interest": all(word in str(changes.get("interest") or "") for word in ("看電影", "打球")),
    }
    return {
        "success": (
            all(checks.values())
            and patch_count >= requested_turns
            and provider_reconnects == 0
        ),
        "checks": checks,
        "changes": changes,
        "held_seconds": hold_seconds,
        "patch_count": patch_count,
        "provider_reconnects": provider_reconnects,
        "rejected": combined_rejected,
        "turns": requested_turns,
        "warnings": list(dict.fromkeys(combined_warnings)),
    }


def main() -> int:
    result = asyncio.run(_run())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
