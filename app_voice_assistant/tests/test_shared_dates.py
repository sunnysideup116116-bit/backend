from types import SimpleNamespace
import asyncio
import json

from app_voice_assistant.contracts import (
    validate_proposal, requires_confirmation, context_allows_proposal,
    safe_context, deterministic_proposal,
)
from app_voice_assistant.duplex_runtime import _append_transcript, _proposal_from_function
from app_voice_assistant.duplex_runtime import run_duplex_session
from .test_duplex_runtime import FakeLive, FakeWebSocket, FakeProvider, FakeLimiter, message, wait_until, _append


def test_date_invitation_query_does_not_read_match_hub():
    for phrase in ("有沒有約會邀請", "我的共同約會", "Do I have date invitations?"):
        proposal = deterministic_proposal(phrase, context={})
        assert proposal.intent == "date.query"


def test_shared_date_write_is_confirmed_and_needs_both_calendar_permissions():
    proposal = _proposal_from_function(SimpleNamespace(name="update_shared_date", args={
        "contact_name": "小安", "changes": {"start_time": "18:00", "end_time": "20:00", "activity": "散步"},
        "user_id": "forged", "coordination_id": "forged",
    }), revision=2)
    assert proposal.intent == "date.update"
    assert set(proposal.arguments) == {"contact_name", "changes"}
    assert requires_confirmation(proposal)
    assert context_allows_proposal(safe_context({"permissions": {"calendar_read": True, "calendar_write": True}}), proposal)
    assert not context_allows_proposal(safe_context({"permissions": {"calendar_read": False, "calendar_write": True}}), proposal)


def test_date_changes_reject_unknown_fields_invalid_times_and_dates():
    for changes in ({"user_id": "other"}, {"start_time": "25:00"}, {"date": "2026-02-30"}, {"notes": "x" * 501}):
        assert validate_proposal({"intent": "date.update", "arguments": {"contact_name": "小安", "changes": changes}}, base_revision=0) is None


def test_transcript_keeps_english_spaces_repeated_words_and_chinese():
    text = ""
    for delta in ("Hello", " ", "Sunny", ", this is ", "very", " very", " nice."):
        text = _append_transcript(text, delta)
    assert text == "Hello Sunny, this is very very nice."
    assert _append_transcript("今天", "一起散步。") == "今天一起散步。"
    assert _append_transcript("Hello", "Hello Sunny") == "Hello Sunny"
    assert len(_append_transcript("a" * 300, " more words")) > 240


def test_live_shared_date_confirmation_and_long_read_results():
    async def scenario():
        live, socket, events = FakeLive(), FakeWebSocket(), []
        task = asyncio.create_task(run_duplex_session(socket, provider=FakeProvider(live), limiter=FakeLimiter(), identity="test", initial_context={"permissions": {"calendar_read": True, "calendar_write": True}}, max_session_seconds=30, send_event=lambda event: _append(events, event)))
        try:
            await live.incoming.put(message(tool_calls=[SimpleNamespace(id="read-dates", name="read_shared_dates", args={"contact_name": ""})]))
            await wait_until(lambda: any(e.get("intent") == "date.query" for e in events))
            text = "目前待回覆：小安。" + "已讀取的日期與安排。" * 40 + "歷史已拒絕：小明。"
            await socket.incoming.put({"type": "websocket.receive", "text": json.dumps({"type": "action_result", "action_id": "read-dates", "success": True, "message": text})})
            await wait_until(lambda: any(r[0] == "read-dates" for r in live.tool_responses))
            assert live.tool_responses[-1][2]["message"] == text
            await live.incoming.put(message(tool_calls=[SimpleNamespace(id="accept-date", name="respond_date_invitation", args={"contact_name": "小安", "accepted": True})]))
            await wait_until(lambda: any(e.get("type") == "confirmation_required" for e in events))
            assert not any(e.get("intent") == "date.respond" and e.get("type") == "action_proposal" for e in events)
            await live.incoming.put(message(tool_calls=[SimpleNamespace(id="confirm-date", name="confirm_pending_action", args={"spoken_phrase": "確認"})]))
            await wait_until(lambda: any(e.get("intent") == "date.respond" and e.get("type") == "action_proposal" for e in events))
            actions = [e for e in events if e.get("intent") == "date.respond" and e.get("type") == "action_proposal"]
            assert len(actions) == 1
            assert actions[0]["arguments"] == {"contact_name": "小安", "accepted": True}
        finally:
            await socket.incoming.put({"type": "websocket.receive", "text": json.dumps({"type": "stop"})})
            await task
    asyncio.run(scenario())


def test_memory_question_corrects_wrong_public_tool_even_without_public_permission():
    async def scenario():
        live, socket, events = FakeLive(), FakeWebSocket(), []
        task = asyncio.create_task(run_duplex_session(socket, provider=FakeProvider(live), limiter=FakeLimiter(), identity="test", initial_context={"permissions": {"memory_read": True, "public_ayue": False}}, max_session_seconds=30, send_event=lambda event: _append(events, event)))
        try:
            await live.incoming.put(message(content=SimpleNamespace(interim_input_transcription=None, input_transcription=SimpleNamespace(text="你記得我什麼", finished=True), output_transcription=None, interrupted=False, model_turn=None, turn_complete=False), tool_calls=[SimpleNamespace(id="memory-wrong", name="ask_public_ayue", args={"domain": "memory", "question": "你記得我什麼"})]))
            await wait_until(lambda: any(e.get("type") == "action_proposal" for e in events))
            action = next(e for e in events if e.get("type") == "action_proposal")
            assert action["intent"] == "memory.query"
            assert action["arguments"] == {"query": ""}
        finally:
            await socket.incoming.put({"type": "websocket.receive", "text": json.dumps({"type": "stop"})})
            await task
    asyncio.run(scenario())
