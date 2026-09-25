import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app_voice_assistant.capability_proxy import CapabilityRefSigner, find_capabilities
from app_voice_assistant.contracts import (
    deterministic_proposal,
    is_chat_cooldown_reason_request,
    is_companion_matching_request,
    safe_context,
)
from app_voice_assistant.duplex_runtime import run_duplex_session
from app_voice_assistant.status_contract import status_result
from app_voice_assistant.tests.test_duplex_runtime import (
    FakeLimiter,
    FakeLive,
    FakeProvider,
    FakeWebSocket,
    _append,
    message,
    wait_until,
)


def _content(*, spoken=None, turn_complete=False):
    return SimpleNamespace(
        interim_input_transcription=None,
        input_transcription=(
            SimpleNamespace(text=spoken, finished=True) if spoken else None
        ),
        output_transcription=None,
        interrupted=False,
        model_turn=None,
        turn_complete=turn_complete,
    )


@pytest.mark.parametrize("question", [
    "這場演唱會誰有興趣會和我去？",
    "台北動漫展誰有興趣會和我去？",
    "有人想和我去台北動漫展嗎？",
    "已配對的朋友誰可能對動漫展有興趣？",
])
def test_event_interest_question_routes_to_matching_ayue(question):
    proposal = deterministic_proposal(question, context=safe_context({}))
    assert is_companion_matching_request(question)
    assert proposal is not None
    assert proposal.intent == "match.ayue_query"
    assert proposal.arguments["question"] == question


def test_known_companion_and_solo_requests_are_not_new_match_searches():
    assert not is_companion_matching_request("我和小美一起去動漫展")
    assert not is_companion_matching_request("推薦我一個人去看展")


def test_proxy_discovery_keeps_the_event_question_with_matching_ayue():
    context = safe_context({"permissions": {
        "match_ayue": True, "match_read": True,
    }})
    signer = CapabilityRefSigner(b"test-secret")
    result = find_capabilities(
        "台北動漫展誰有興趣會和我去？", "perform", context=context,
        signer=signer, user_id="owner", session_id="voice",
    )
    operations = result["recommended_operations"]
    assert len(operations) == 1
    assert signer.verify(
        operations[0]["capability_ref"], user_id="owner",
        session_id="voice", context=context,
    ) == "match.ayue_query"
    assert operations[0]["arguments"]["question"] == "台北動漫展誰有興趣會和我去？"


@pytest.mark.parametrize("question,scope,expected", [
    ("聊天室在冷卻，為什麼不能送訊息？", "chat", True),
    ("為什麼我不能傳訊息？", "chat", True),
    ("Why can't I send messages in this chat?", "chat", True),
    ("為什麼不能傳？", "chat", True),
    ("為什麼行事曆不能送出？", "calendar", False),
    ("剛剛那則有送出嗎？", "chat", False),
])
def test_cooldown_reason_routing_is_chat_scoped(question, scope, expected):
    assert is_chat_cooldown_reason_request(question, scope=scope) is expected
    if expected:
        proposal = deterministic_proposal(question, context=safe_context({
            "scope": scope,
        }))
        assert proposal is not None and proposal.intent == "chat.status.query"


def test_active_cooldown_explains_the_design_without_inventing_a_trigger():
    now = datetime.now(timezone.utc)
    active = status_result(cooldown={
        "state": "active",
        "server_time": now.isoformat(),
        "until": (now + timedelta(minutes=2)).isoformat(),
    })
    assert active["error_code"] == "risk_cooldown"
    assert "保護你和對方" in active["message"]
    assert "單靠冷卻倒數，無法判斷" in active["message"]
    clear = status_result(cooldown={
        "state": "clear", "server_time": now.isoformat(),
    })
    assert "保護你和對方" not in clear["message"]


@pytest.mark.parametrize("routing_mode", ["legacy", "template", "proxy"])
def test_spoken_event_interest_delegates_once_in_all_routing_modes(routing_mode):
    async def scenario():
        live, socket, events = FakeLive(), FakeWebSocket(), []
        task = asyncio.create_task(run_duplex_session(
            socket, provider=FakeProvider(live), limiter=FakeLimiter(),
            identity="test", initial_context={
                "permissions": {"match_ayue": True},
            }, max_session_seconds=30,
            send_event=lambda event: _append(events, event),
            routing_mode=routing_mode,
        ))
        try:
            question = "台北動漫展誰有興趣會和我去？"
            await live.incoming.put(message(content=_content(spoken=question)))
            await wait_until(lambda: any(
                e.get("intent") == "match.ayue_query" for e in events
            ))
            proposals = [
                e for e in events if e.get("type") == "action_proposal"
            ]
            assert len(proposals) == 1
            assert proposals[0]["arguments"]["question"] == question
            await live.incoming.put(message(tool_calls=[SimpleNamespace(
                id="wrong-places", name="ask_public_ayue",
                args={"domain": "places", "question": question},
            )]))
            await wait_until(lambda: any(
                response[0] == "wrong-places" for response in live.tool_responses
            ))
            duplicate = next(
                response[2] for response in live.tool_responses
                if response[0] == "wrong-places"
            )
            assert duplicate["status"] == "already_dispatched"
            assert len([e for e in events if e.get("type") == "action_proposal"]) == 1
        finally:
            await socket.incoming.put({"type": "websocket.disconnect"})
            await task

    asyncio.run(scenario())


@pytest.mark.parametrize("routing_mode", ["legacy", "template", "proxy"])
def test_spoken_cooldown_why_reads_status_and_explains_safety(routing_mode):
    async def scenario():
        live, socket, events = FakeLive(), FakeWebSocket(), []
        task = asyncio.create_task(run_duplex_session(
            socket, provider=FakeProvider(live), limiter=FakeLimiter(),
            identity="test", initial_context={
                "scope": "chat",
                "permissions": {"chat_list": True},
                "feature_status": {"operational_status": True},
            }, max_session_seconds=30,
            send_event=lambda event: _append(events, event),
            routing_mode=routing_mode,
        ))
        try:
            await live.incoming.put(message(content=_content(
                spoken="為什麼聊天室在冷卻時不能送訊息？",
            )))
            await wait_until(lambda: any(
                e.get("intent") == "chat.status.query" for e in events
            ))
            query = next(e for e in events if e.get("intent") == "chat.status.query")
            duplicate_name = (
                "read_app_data" if routing_mode == "template" else
                "find_app_capabilities" if routing_mode == "proxy" else
                "read_chat_status"
            )
            await live.incoming.put(message(tool_calls=[SimpleNamespace(
                id="duplicate-status", name=duplicate_name,
                args={"domain": "delivery", "mode": "perform"},
            )]))
            await wait_until(lambda: any(
                response[0] == "duplicate-status" for response in live.tool_responses
            ))
            assert next(
                response[2]["status"] for response in live.tool_responses
                if response[0] == "duplicate-status"
            ) == "already_dispatched"
            now = datetime.now(timezone.utc)
            result = status_result(cooldown={
                "state": "active", "server_time": now.isoformat(),
                "until": (now + timedelta(minutes=2)).isoformat(),
            })
            await socket.incoming.put({
                "type": "websocket.receive",
                "text": json.dumps({
                    "type": "action_result", "action_id": query["action_id"],
                    **result,
                }),
            })
            await live.incoming.put(message(content=_content(turn_complete=True)))
            await wait_until(lambda: any(
                "保護你和對方" in item for item in live.text
            ))
            assert len([e for e in events if e.get("type") == "action_proposal"]) == 1
        finally:
            await socket.incoming.put({"type": "websocket.disconnect"})
            await task

    asyncio.run(scenario())
