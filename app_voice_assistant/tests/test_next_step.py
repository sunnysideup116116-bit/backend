import asyncio
import json
from types import SimpleNamespace

import pytest
import mongomock

from app_voice_assistant.contracts import deterministic_proposal
from app_voice_assistant.duplex_runtime import run_duplex_session
from app_voice_assistant.next_step import (
    build_place_next_step_offer,
    place_next_step_action,
    select_place_next_step,
)
from app_voice_assistant.task_service import VoiceTaskService
from app_voice_assistant.tests.test_duplex_runtime import (
    FakeLimiter,
    FakeLive,
    FakeProvider,
    FakeWebSocket,
    _append,
    message,
    wait_until,
)


PLACES = [{"name": "台東山海咖啡", "category": "cafe"}]
PERMISSIONS = {
    "public_ayue": True,
    "places": True,
    "calendar_read": True,
    "match_ayue": True,
}


def _offer(*, permissions=None, result=None, now=100):
    return build_place_next_step_offer(
        SimpleNamespace(
            intent="ayue.public_query",
            arguments={"domain": "places", "question": "這週末台東哪裡好玩？"},
        ),
        result or {"status": "success", "data": {"recommendations": PLACES}},
        PERMISSIONS if permissions is None else permissions,
        now=now,
    )


def test_offer_requires_a_successful_structured_place_result_and_permission():
    assert _offer()["calendar_range"] == "weekend"
    assert _offer(result={"status": "failed", "data": {"recommendations": PLACES}}) is None
    assert _offer(result={"status": "success", "data": {"recommendations": []}}) is None
    assert _offer(permissions={}) is None
    assert "行事曆" in _offer(permissions={"calendar_read": True})["prompt"]
    assert "配對阿月" not in _offer(permissions={"calendar_read": True})["prompt"]


@pytest.mark.parametrize("utterance, expected", [
    ("好", "clarify"),
    ("第一個", "calendar"),
    ("幫我查明天行事曆", "calendar"),
    ("請配對阿月推薦同行人", "companion"),
    ("不要找人，查行事曆", "calendar"),
    ("不用查行事曆，找同行人", "companion"),
    ("不要找同行人", "dismiss"),
    ("不用查行事曆", "dismiss"),
    ("先不用", "dismiss"),
    ("新增行程", None),
    ("修改 Google 日曆行程", None),
    ("配對阿月目前配對進度？", None),
    ("查下個月行事曆", None),
    ("台東好玩的地方呢", None),
])
def test_explicit_selection_and_calendar_write_guard(utterance, expected):
    assert select_place_next_step(utterance, _offer(), now=110) == expected


def test_offer_expires_and_selection_uses_the_spoken_range():
    offer = _offer()
    assert select_place_next_step("查行事曆", offer, now=191) is None
    assert place_next_step_action(
        "calendar", offer, selected_text="查明天 Google 行事曆",
    ) == ("calendar.query", {"source": "google", "range": "tomorrow"})
    assert place_next_step_action(
        "calendar", offer, selected_text="查這週行事曆",
    ) == ("calendar.query", {"source": "all", "range": "week"})
    offer["source_transcript"] = "台東哪裡好玩，和誰去？"
    assert deterministic_proposal(
        offer["source_transcript"], context={
            "_next_step_offer": offer, "permissions": PERMISSIONS,
        },
    ).arguments != place_next_step_action("companion", offer)[1]


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


async def _start_place_session(*, routing_mode="legacy"):
    live, socket, events = FakeLive(), FakeWebSocket(), []
    task_service = None
    if routing_mode == "proxy":
        db = mongomock.MongoClient().db
        task_service = VoiceTaskService(db.batches, db.tasks, enabled=True)
    task = asyncio.create_task(run_duplex_session(
        socket,
        provider=FakeProvider(live),
        limiter=FakeLimiter(),
        identity="test",
        initial_context={"permissions": PERMISSIONS},
        max_session_seconds=30,
        send_event=lambda event: _append(events, event),
        routing_mode=routing_mode,
        task_service=task_service,
    ))
    if routing_mode != "template":
        await live.incoming.put(message(content=_content(spoken="這週末台東哪裡好玩？")))
    if routing_mode == "proxy":
        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="find-places", name="find_app_capabilities",
            args={"query": "這週末台東哪裡好玩？", "mode": "perform"},
        )]))
    else:
        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id="places",
            name="ask_app_ayue" if routing_mode == "template" else "ask_public_ayue",
            args={"domain": "places", "question": "這週末台東哪裡好玩？"},
        )]))
    await wait_until(lambda: any(e.get("intent") == "ayue.public_query" for e in events))
    place_action_id = next(
        e["action_id"] for e in events if e.get("intent") == "ayue.public_query"
    )
    await socket.incoming.put({"type": "websocket.receive", "text": json.dumps({
        "type": "action_result", "action_id": place_action_id,
        "result_version": 1, "success": True,
        "message": "推薦台東山海咖啡。",
        "data": {"recommendations": PLACES},
    })})
    await live.incoming.put(message(content=_content(turn_complete=True)))
    await wait_until(lambda: any("next_step_offer" in item for item in live.text))
    return live, socket, events, task


@pytest.mark.parametrize("routing_mode", ["legacy", "template", "proxy"])
def test_place_result_offers_once_then_waits_for_explicit_calendar_choice(routing_mode):
    async def scenario():
        live, socket, events, task = await _start_place_session(routing_mode=routing_mode)
        try:
            assert "要我查這週末的行事曆" in live.text[-1]
            assert "不要提到有多個阿月" not in live.text[-1]
            assert [e.get("intent") for e in events if e.get("type") == "action_proposal"] == [
                "ayue.public_query",
            ]
            await live.incoming.put(message(tool_calls=[SimpleNamespace(
                id="premature", name="read_calendar", args={"range": "week"},
            )]))
            await wait_until(lambda: any(row[0] == "premature" for row in live.tool_responses))
            assert next(row[2]["status"] for row in live.tool_responses if row[0] == "premature") == "needs_input"
            await live.incoming.put(message(content=_content(turn_complete=True)))
            await live.incoming.put(message(content=_content(spoken="好")))
            await live.incoming.put(message(content=_content(turn_complete=True)))
            await wait_until(lambda: any("要先查行事曆" in item for item in live.text))
            assert not any(e.get("intent") == "calendar.query" for e in events)
            await live.incoming.put(message(content=_content(turn_complete=True)))
            await live.incoming.put(message(content=_content(spoken="查明天行事曆")))
            await wait_until(lambda: any(e.get("intent") == "calendar.query" for e in events))
            calendar = next(e for e in events if e.get("intent") == "calendar.query")
            assert calendar["arguments"] == {"source": "all", "range": "tomorrow"}
            await live.incoming.put(message(tool_calls=[SimpleNamespace(
                id="duplicate", name="read_calendar", args={"range": "tomorrow"},
            )]))
            await wait_until(lambda: any(row[0] == "duplicate" for row in live.tool_responses))
            assert next(row[2]["status"] for row in live.tool_responses if row[0] == "duplicate") == "already_dispatched"
            assert sum(e.get("intent") == "calendar.query" for e in events) == 1
        finally:
            await socket.incoming.put({"type": "websocket.disconnect"})
            await task

    asyncio.run(scenario())


@pytest.mark.parametrize("routing_mode", ["legacy", "template", "proxy"])
def test_matching_choice_survives_page_change_and_delegates_once(routing_mode):
    async def scenario():
        live, socket, events, task = await _start_place_session(routing_mode=routing_mode)
        try:
            await socket.incoming.put({"type": "websocket.receive", "text": json.dumps({
                "type": "context_changed",
                "context": {"scope": "calendar", "permissions": PERMISSIONS},
            })})
            await live.incoming.put(message(content=_content(turn_complete=True)))
            await live.incoming.put(message(content=_content(spoken="請配對阿月推薦同行人")))
            await wait_until(lambda: any(e.get("intent") == "match.ayue_query" for e in events))
            matching = next(e for e in events if e.get("intent") == "match.ayue_query")
            assert "剛才推薦的地點" in matching["arguments"]["question"]
            assert sum(e.get("intent") == "match.ayue_query" for e in events) == 1
            assert not any(e.get("intent") == "calendar.query" for e in events)
        finally:
            await socket.incoming.put({"type": "websocket.disconnect"})
            await task

    asyncio.run(scenario())


def test_calendar_choice_preserves_google_source_in_legacy_routing():
    async def scenario():
        live, socket, events, task = await _start_place_session()
        try:
            await live.incoming.put(message(content=_content(turn_complete=True)))
            await live.incoming.put(message(content=_content(spoken="查明天 Google 行事曆")))
            await wait_until(lambda: any(e.get("intent") == "calendar.query" for e in events))
            calendar = next(e for e in events if e.get("intent") == "calendar.query")
            assert calendar["arguments"] == {"source": "google", "range": "tomorrow"}
        finally:
            await socket.incoming.put({"type": "websocket.disconnect"})
            await task

    asyncio.run(scenario())


def test_unrelated_google_calendar_edit_clears_the_old_offer():
    async def scenario():
        live, socket, events, task = await _start_place_session()
        try:
            await live.incoming.put(message(content=_content(turn_complete=True)))
            await live.incoming.put(message(content=_content(
                spoken="幫我修改 Google 日曆上的行程",
            )))
            await live.incoming.put(message(content=_content(turn_complete=True)))
            await wait_until(lambda: any(
                "Google" in item and "無法" in item for item in live.text[2:]
            ))
            await live.incoming.put(message(content=_content(turn_complete=True)))
            await live.incoming.put(message(content=_content(spoken="好")))
            await live.incoming.put(message(content=_content(turn_complete=True)))
            await wait_until(lambda: any(
                e.get("type") == "user_transcript" and e.get("text") == "好"
                for e in events
            ))
            assert not any("要先查行事曆" in item for item in live.text)
            assert [e.get("intent") for e in events if e.get("type") == "action_proposal"] == [
                "ayue.public_query",
            ]
        finally:
            await socket.incoming.put({"type": "websocket.disconnect"})
            await task

    asyncio.run(scenario())
