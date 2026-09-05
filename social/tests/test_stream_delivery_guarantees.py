import asyncio
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import BackgroundTasks, Request

from models import DirectChatRequest, MediatorPrivateRequest
from routers.private_mediator import mediator_private_chat_stream
from routers.public_chat import direct_chat_stream
from services.ayue_agent.private_v2 import (
    PrivateAgentDecision,
    run_private_agent_turn_v2,
)
from tests.test_private_v2 import _context as private_context


def _request(path: str, *, token_stream: bool = False) -> Request:
    headers = [(b"host", b"testclient")]
    if token_stream:
        headers.append((b"x-ayue-stream-tokens", b"v1"))
    encoded = path.encode("ascii")
    return Request({
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": encoded,
        "query_string": b"",
        "headers": headers,
        "client": ("testclient", 12345),
        "server": ("testclient", 80),
    })


class StreamDeliveryGuaranteesTests(unittest.TestCase):
    def test_private_runtime_forwards_live_provider_tokens_before_final(self):
        context = private_context()
        decision = PrivateAgentDecision(
            kind="final",
            intent="advice",
            confidence=.9,
            reply="這段規劃器草稿不應直接送到前端。",
        )
        fragments = ["你可以先從", "電影聊起，", "再接住對方的回應。"]
        reply = "".join(fragments)
        tokens = []

        def live_completion(_prompt, *, on_token=None, **_kwargs):
            self.assertIsNotNone(on_token)
            for fragment in fragments:
                on_token(fragment)
            return SimpleNamespace(content=reply)

        with patch(
            "services.ayue_agent.private_v2.build_private_turn_context_v2",
            return_value=context,
        ), patch(
            "services.ayue_agent.private_v2._plan", return_value=decision,
        ), patch(
            "services.ayue_agent.private_v2.generate_chat_completion",
            side_effect=live_completion,
        ), patch("services.ayue_agent.private_v2._trace"):
            result = run_private_agent_turn_v2(
                user_id="owner",
                other_id="other",
                message=context.message,
                match_doc={"status": "accepted", "proposal_revision": 1},
                on_token=tokens.append,
            )

        self.assertEqual(result.reply, reply)
        self.assertEqual(tokens, fragments)

    def test_public_first_token_is_observable_before_final(self):
        release = threading.Event()

        def fake_turn(_req, _tasks, emit, on_token=None):
            emit({"type": "run_started", "agent_run_id": "run-live"})
            on_token("第")
            release.wait(1)
            on_token("一字")
            return {"reply": "第一字", "agent_run_id": "run-live"}

        async def scenario(response):
            iterator = response.body_iterator.__aiter__()
            started = json.loads(await asyncio.wait_for(anext(iterator), 1))
            token = json.loads(await asyncio.wait_for(anext(iterator), 1))
            self.assertFalse(release.is_set())
            release.set()
            remaining = [json.loads(chunk) async for chunk in iterator]
            return started, token, remaining

        req = DirectChatRequest(
            user_id="owner", contact_id="ai_assistant", message="說話",
        )
        with patch("routers.public_chat._run_public_stream_turn", side_effect=fake_turn):
            response = direct_chat_stream(
                req,
                BackgroundTasks(),
                _request("/api/direct_chat/stream", token_stream=True),
            )
            started, token, remaining = asyncio.run(scenario(response))

        self.assertEqual(started["type"], "run_started")
        self.assertEqual(token, {
            "type": "token", "agent_run_id": "run-live", "text": "第",
        })
        self.assertEqual(remaining[-1]["type"], "final")
        self.assertEqual(response.headers["cache-control"], "no-cache, no-transform")
        self.assertEqual(response.headers["x-accel-buffering"], "no")

    def test_public_queue_keeps_every_token_in_order(self):
        fragments = [str(index) for index in range(100)]

        def fake_turn(_req, _tasks, _emit, on_token=None):
            for fragment in fragments:
                on_token(fragment)
            return {"reply": "".join(fragments), "agent_run_id": "run-all"}

        async def collect(response):
            return [json.loads(chunk) async for chunk in response.body_iterator]

        req = DirectChatRequest(
            user_id="owner", contact_id="ai_assistant", message="列出數字",
        )
        with patch("routers.public_chat._run_public_stream_turn", side_effect=fake_turn):
            events = asyncio.run(collect(direct_chat_stream(
                req,
                BackgroundTasks(),
                _request("/api/direct_chat/stream", token_stream=True),
            )))

        self.assertEqual(
            [event["text"] for event in events if event["type"] == "token"],
            fragments,
        )
        self.assertEqual(events[-1]["type"], "final")

    def test_private_stream_delivers_tokens_and_no_buffer_headers(self):
        def fake_turn(_req, _match, _room, _emit, _run_id, on_token=None):
            on_token("悄")
            on_token("悄話")
            return {"reply": "悄悄話", "agent_run_id": "private-run"}

        async def collect(response):
            return [json.loads(chunk) async for chunk in response.body_iterator]

        req = MediatorPrivateRequest(
            user_id="owner", other_id="other", message="想問問",
        )
        with patch("routers.private_mediator.find_accepted_match", return_value={"status": "accepted"}), \
             patch("routers.private_mediator.generate_mediator_private_room_id", return_value="room"), \
             patch("routers.private_mediator.save_message"), \
             patch("routers.private_mediator._run_private_v2_saved_turn", side_effect=fake_turn):
            response = mediator_private_chat_stream(req, BackgroundTasks())
            events = asyncio.run(collect(response))

        self.assertEqual(
            [event["text"] for event in events if event["type"] == "token"],
            ["悄", "悄話"],
        )
        self.assertEqual(events[-1]["type"], "final")
        self.assertEqual(response.headers["cache-control"], "no-cache, no-transform")
        self.assertEqual(response.headers["x-accel-buffering"], "no")


if __name__ == "__main__":
    unittest.main()
