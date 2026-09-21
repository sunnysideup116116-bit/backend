"""Candy live feedback: delivery, match intent and stable confirmation copy."""
import asyncio
import json
import threading
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import mongomock
import pytest
from fastapi import BackgroundTasks

from models import DirectChatRequest
from routers.public_chat import direct_chat_stream
from services.ayue_agent.contracts import (
    AgentResult, PublicAgentRequestContext, PublicAgentTurnContext, TurnClockV1,
)
from services.ayue_agent.pi import public_turn, tool_runtime
from services.ayue_agent.pi import runtime as pi_runtime
from services.ai_service import ToolCallResult
from services.ayue_agent.contracts import ToolResult
from services.ayue_agent.pi.registry import tool_schemas
from services.ayue_agent.shared.confirmation import ConfirmationManager, project_match_choice_history
from services.ayue_agent.shared.contact_selections import ContactSelectionManager
from services.ayue_agent.shared.operation_batches import OperationBatchManager
from tests.test_stream_delivery_guarantees import _request


def make_turn(message="幫我加到行事曆", history=None):
    request = PublicAgentRequestContext(
        user_id="owner", room_id="room", message=message,
        external_calendar_authorized=True, recent_history=history or [],
    )
    turn = PublicAgentTurnContext(
        user_id="owner", room_id="room", message=message,
        recent_messages=history or [],
        clock=TurnClockV1(
            timezone="Asia/Taipei", local_date="2026-09-14", local_time="12:00",
            local_iso="2026-09-14T12:00:00+08:00",
            utc_iso="2026-09-14T04:00:00+00:00", weekday_zh_tw="星期一",
        ),
    )
    turn._raw_ctx = request
    turn._mentioned_ids = []
    return request, turn


def test_refresh_pending_match_card_keeps_server_display_and_labels():
    store = mongomock.MongoClient().test
    preview = "我會依你最近分享的近況找人。要開始搜尋嗎？"
    store.confirmations.insert_one({
        "_id": "choice", "user_id": "owner", "room_id": "room",
        "surface": "public_ayue", "interaction_mode": "bubble_buttons_v1",
        "tool_name": "match.start_search", "arguments": {}, "payload": {},
        "status": "pending", "preview_text": preview, "expires_at": time.time() + 900,
    })
    messages = [{"metadata": {"choice_prompt": {"id": "choice", "state": "pending"}}}]
    before = deepcopy(messages)
    refreshed = project_match_choice_history(messages, user_id="owner", room_id="room", collection=store.confirmations)
    choice = refreshed[0]["metadata"]["choice_prompt"]
    assert choice["display"]["summary"] == preview
    assert choice["confirm_label"] == "開始搜尋"
    assert choice["state"] == "pending"
    assert messages == before


def test_final_language_normalization_keeps_person_wording():
    store = mongomock.MongoClient().test
    request, _ = make_turn("找人一起衝浪")
    result = public_turn._bind_interactions(
        AgentResult(handled=True, reply="我會幫你找合適的對象。", messages=["我會幫你找合適的對象。"]),
        ctx=request, run_id="run", confirmations=ConfirmationManager(store.c),
        selections=ContactSelectionManager(store.s), batches=OperationBatchManager(store.b),
    )
    assert result.reply == "我會幫你找合適的對象。"
    assert "物件" not in result.reply


def test_pi_connect_event_does_not_wait_for_the_first_model_response():
    release = threading.Event()
    def slow_turn(*_args, **_kwargs):
        release.wait(2)
        return {"reply": "查詢完成。", "agent_mode": "pi"}

    async def check(response):
        iterator = response.body_iterator.__aiter__()
        try:
            first = json.loads(await asyncio.wait_for(anext(iterator), 0.3))
            assert first["type"] == "run_started"
            assert "reply" not in first and "text" not in first
        finally:
            release.set()
        rest = [json.loads(chunk) async for chunk in iterator]
        assert rest[-1]["type"] == "final"

    with patch("routers.public_chat.authenticated_owner_matches", return_value=True), \
         patch("routers.public_chat._require_public_pi", return_value="pi"), \
         patch("routers.public_chat._run_public_stream_turn", side_effect=slow_turn):
        response = direct_chat_stream(DirectChatRequest(user_id="owner", contact_id="ai_assistant", message="找人"), BackgroundTasks(), _request("/api/direct_chat/stream"))
        asyncio.run(check(response))


def test_pi_activity_search_schema_and_preparation_preserve_surfing(monkeypatch):
    from services.ayue_agent.shared import write_executors
    monkeypatch.setattr(write_executors, "assess_match_opportunity", lambda *_a, **_k: SimpleNamespace(state="ready"))
    store = mongomock.MongoClient().test
    _, turn = make_turn("好幫我找一起衝浪的人")
    runtime = tool_runtime.PiToolRuntime(turn, run_id="surfing-run", trace={}, confirmation_collection=store.c, contact_selection_collection=store.s, operation_batch_collection=store.b)
    schema = next(item for item in tool_schemas() if item["name"] == "match.start_search")
    assert "topic" in schema["parameters"]["properties"]
    result = runtime.dispatch("match.start_search", {"kind": "activity", "topic": "衝浪"})
    assert result["result"]["pending_confirmation"]
    record = store.c.find_one({})
    assert record["payload"]["search_context"]["invitation_topic"] == "衝浪"
    assert record["status"] == "prepared"
    assert "delivery_mode" not in record["payload"]
    assert "衝浪" in record["preview_text"]


@pytest.mark.parametrize("message,topic", [
    ("幫我找喜歡 K-pop 的人", "K-pop"),
    ("幫我找喜歡 Kpop 的人", "K-pop"),
])
def test_pi_preference_search_is_canonical_and_confirmation_bound(monkeypatch, message, topic):
    from services.ayue_agent.shared import write_executors
    monkeypatch.setattr(
        write_executors, "assess_match_opportunity",
        lambda *_a, **_k: SimpleNamespace(state="ready"),
    )
    store = mongomock.MongoClient().test
    _, turn = make_turn(message)
    runtime = tool_runtime.PiToolRuntime(
        turn, run_id="preference-run", trace={}, confirmation_collection=store.c,
        contact_selection_collection=store.s, operation_batch_collection=store.b,
    )
    result = runtime.dispatch("match.start_search", {
        "kind": "preference", "topic": topic,
    })
    assert result["result"]["pending_confirmation"]
    record = store.c.find_one({})
    context = record["payload"]["search_context"]
    assert context["search_intent"] == "preference"
    assert context["normalized_topic"] == "K-pop"
    assert context["canonical_preference_key"] == "k_pop"
    assert "明確保存" in record["preview_text"]
    assert "不會用近期活動猜測偏好" in record["preview_text"]
    assert "delivery_mode" not in record["payload"]


def test_explicit_preference_wording_overrides_a_misclassified_activity_kind(monkeypatch):
    from services.ayue_agent.shared import write_executors
    monkeypatch.setattr(
        write_executors, "assess_match_opportunity",
        lambda *_a, **_k: SimpleNamespace(state="ready"),
    )
    store = mongomock.MongoClient().test
    _, turn = make_turn("幫我找喜歡 Kpop 的人")
    runtime = tool_runtime.PiToolRuntime(
        turn, run_id="preference-intent-guard", trace={},
        confirmation_collection=store.c,
        contact_selection_collection=store.s,
        operation_batch_collection=store.b,
    )
    result = runtime.dispatch(
        "match.start_search", {"kind": "activity", "topic": "K-pop"},
    )
    assert result["result"]["pending_confirmation"]
    context = store.c.find_one({})["payload"]["search_context"]
    assert context["search_intent"] == "preference"
    assert context["canonical_preference_key"] == "k_pop"


@pytest.mark.parametrize("kind", ["preference", "activity"])
def test_negative_preference_request_never_becomes_positive_graph_search(
    monkeypatch, kind,
):
    store = mongomock.MongoClient().test
    _, turn = make_turn("幫我找不喜歡 K-pop 的人")
    runtime = tool_runtime.PiToolRuntime(
        turn, run_id="negative-preference-guard", trace={},
        confirmation_collection=store.c,
        contact_selection_collection=store.s,
        operation_batch_collection=store.b,
    )
    result = runtime.dispatch(
        "match.start_search", {"kind": kind, "topic": "K-pop"},
    )
    assert result["error_code"] == "preflight_rejected"
    assert store.c.count_documents({}) == 0


@pytest.mark.parametrize("message,history,arguments,topic", [
    ("對", [{"role": "user", "content": "幫我找一起衝浪的人"}, {"role": "assistant", "content": "要開始找衝浪人選嗎？"}], {"kind": "activity", "topic": "衝浪"}, "衝浪"),
    ("改用近期情境就好", [{"role": "user", "content": "找衝浪人選"}], {"kind": "recent_context"}, None),
])
def test_match_followup_uses_visible_context_and_explicit_recent_mode(monkeypatch, message, history, arguments, topic):
    from services.ayue_agent.shared import write_executors
    monkeypatch.setattr(write_executors, "assess_match_opportunity", lambda *_a, **_k: SimpleNamespace(state="ready"))
    store = mongomock.MongoClient().test
    _, turn = make_turn(message, history)
    runtime = tool_runtime.PiToolRuntime(turn, run_id="followup", trace={}, confirmation_collection=store.c, contact_selection_collection=store.s, operation_batch_collection=store.b)
    result = runtime.dispatch("match.start_search", arguments)
    assert result["result"]["pending_confirmation"]
    payload = store.c.find_one({})["payload"]
    assert (payload.get("search_context") or {}).get("invitation_topic") == topic
    assert "delivery_mode" not in payload


def test_match_unseen_topic_or_forged_authority_never_prepares(monkeypatch):
    store = mongomock.MongoClient().test
    _, turn = make_turn("找人")
    runtime = tool_runtime.PiToolRuntime(turn, run_id="no-authority", trace={}, confirmation_collection=store.c, contact_selection_collection=store.s, operation_batch_collection=store.b)
    result = runtime.dispatch("match.start_search", {"kind": "activity", "topic": "衝浪"})
    assert result["error_code"] == "preflight_rejected"
    with pytest.raises(ValueError):
        runtime.dispatch("match.start_search", {"kind": "activity", "topic": "衝浪", "delivery_mode": "invite_on_match"})
    assert store.c.count_documents({}) == 0


def test_confirmed_surfing_search_reaches_executor_once(monkeypatch):
    from services.ayue_agent.shared import write_executors
    monkeypatch.setattr(write_executors, "assess_match_opportunity", lambda *_a, **_k: SimpleNamespace(state="ready"))
    store = mongomock.MongoClient().test
    monkeypatch.setattr(write_executors, "TOOL_CALLS", store.calls)
    submitted = []
    def start_search(user_id, **kwargs):
        submitted.append(kwargs)
        return {"status": "queued"}
    monkeypatch.setattr(write_executors, "start_match_search", start_search)
    request, turn = make_turn("幫我找一起衝浪的人")
    runtime = tool_runtime.PiToolRuntime(turn, run_id="surfing", trace={}, confirmation_collection=store.c, contact_selection_collection=store.s, operation_batch_collection=store.b)
    runtime.dispatch("match.start_search", {"kind": "activity", "topic": "衝浪"})
    manager = ConfirmationManager(store.c)
    record = store.c.find_one({})
    assert submitted == []
    manager.bind_final_preview(user_id=request.user_id, origin_run_id="surfing", final_content=record["preview_text"])
    assert manager.mark_presented(user_id=request.user_id, origin_run_id="surfing", message_id="saved", persisted_content=record["preview_text"])
    for _ in range(2):
        manager.execute_confirmed(user_id=request.user_id, room_id=request.room_id, surface="public_ayue", choice_id=record["_id"], executor=lambda name, args, uid, payload: write_executors.execute_write(name, args, request, turn, "confirm-run", 0, payload=payload))
    assert len(submitted) == 1
    assert submitted[0]["search_context"]["invitation_topic"] == "衝浪"
    assert "delivery_mode" not in submitted[0]


def test_same_turn_place_result_reuses_private_coordinates_without_prompt_leak(monkeypatch):
    store = mongomock.MongoClient().test
    _, turn = make_turn("找嘉義餐廳，再找那間附近的飲料店")
    runtime = tool_runtime.PiToolRuntime(
        turn, run_id="places-chain", trace={}, confirmation_collection=store.c,
        contact_selection_collection=store.s, operation_batch_collection=store.b,
    )
    calls = []

    def execute(call, ctx, **_kwargs):
        calls.append(call.arguments)
        if len(calls) == 1:
            return ToolResult(
                ok=True,
                data={"places": [{
                    "name": "鳴笛中式餐廳嘉義分店", "category": "restaurant",
                    "distance_m": 100, "address_summary": "嘉義市中山路528號",
                    "map_url": "https://www.google.com/maps/place/example",
                    "provider": "google", "place_id": "ChIJexample",
                }]},
                private_data={"place_anchor_candidates": [{
                    "name": "鳴笛中式餐廳嘉義分店", "address_summary": "嘉義市中山路528號",
                    "provider": "google", "place_id": "ChIJexample",
                    "map_url": "https://www.google.com/maps/place/example",
                    "latitude": 23.479, "longitude": 120.449,
                }]},
            )
        trusted = getattr(ctx, "_pi_trusted_place_anchor", None)
        assert trusted["requested_anchor"] == "鳴笛中式餐廳嘉義分店"
        assert trusted["latitude"] == 23.479
        assert trusted["longitude"] == 120.449
        return ToolResult(ok=True, data={"places": []})

    monkeypatch.setattr(tool_runtime, "execute_tool", execute)
    first = runtime.read("places.search_nearby", {
        "anchor": "嘉義", "categories": ["restaurant"], "limit": 5,
    })
    second = runtime.read("places.search_nearby", {
        "anchor": "鳴笛中式餐廳嘉義分店", "categories": ["cafe"],
        "cuisine": "飲料店", "limit": 5,
    })

    assert first["status"] == "ok" and second["status"] == "ok"
    assert "latitude" not in str(first)
    assert "longitude" not in str(first)
    assert getattr(turn._raw_ctx, "_pi_trusted_place_anchor", None) is None


def test_invalid_private_place_coordinates_are_ignored(monkeypatch):
    store = mongomock.MongoClient().test
    _, turn = make_turn("找店")
    runtime = tool_runtime.PiToolRuntime(
        turn, run_id="invalid-place-anchor", trace={}, confirmation_collection=store.c,
        contact_selection_collection=store.s, operation_batch_collection=store.b,
    )
    runtime.results.append({"private_data": {"place_anchor_candidates": [{
        "name": "同名店", "provider": "google", "place_id": "bad",
        "latitude": "not-a-number", "longitude": 999,
    }]}})

    def execute(_call, ctx, **_kwargs):
        assert getattr(ctx, "_pi_trusted_place_anchor", None) is None
        return ToolResult(ok=True, data={"places": []})

    monkeypatch.setattr(tool_runtime, "execute_tool", execute)
    result = runtime.read("places.search_nearby", {
        "anchor": "同名店", "categories": ["cafe"], "limit": 3,
    })
    assert result["status"] == "ok"


def test_ambiguous_same_name_place_candidates_never_reuse_coordinates(monkeypatch):
    store = mongomock.MongoClient().test
    _, turn = make_turn("找同名店附近")
    runtime = tool_runtime.PiToolRuntime(
        turn, run_id="ambiguous-place-anchor", trace={}, confirmation_collection=store.c,
        contact_selection_collection=store.s, operation_batch_collection=store.b,
    )
    runtime.results.append({"private_data": {"place_anchor_candidates": [
        {"name": "同名店", "address_summary": "嘉義市一號", "provider": "google",
         "place_id": "one", "latitude": 23.47, "longitude": 120.44},
        {"name": "同名店", "address_summary": "嘉義市二號", "provider": "google",
         "place_id": "two", "latitude": 23.48, "longitude": 120.45},
    ]}})

    def execute(_call, ctx, **_kwargs):
        assert getattr(ctx, "_pi_trusted_place_anchor", None) is None
        return ToolResult(ok=True, data={"places": []})

    monkeypatch.setattr(tool_runtime, "execute_tool", execute)
    result = runtime.read("places.search_nearby", {
        "anchor": "同名店", "categories": ["cafe"], "limit": 3,
    })
    assert result["status"] == "ok"


def test_unsafe_provider_stream_is_held_before_public_tokens(monkeypatch):
    store = mongomock.MongoClient().test
    request, turn = make_turn("哈囉")
    monkeypatch.setattr(public_turn, "build_public_agent_turn_context", lambda *_a, **_k: turn)
    monkeypatch.setattr(public_turn, "validated_mentioned_contact_ids", lambda *_a: ([], False))
    def provider(*_a, **kwargs):
        kwargs["on_token"]("我已準備 RAW_UNVALIDATED [[confirmation]]。")
        return ToolCallResult(content="你好，我是阿月。", tool_calls=[])
    monkeypatch.setattr(pi_runtime, "generate_chat_completion_with_tools", provider)
    tokens = []
    result = public_turn.run_pi_public_turn(request, on_token=tokens.append,
        confirmation_collection=store.c, contact_selection_collection=store.s,
        operation_batch_collection=store.b, runs_collection=store.r)
    assert result.reply == "你好，我是阿月。"
    assert "".join(tokens) == result.reply
    assert "RAW_UNVALIDATED" not in str(tokens)
    assert store.r.find_one({})["pi_model_diagnostics"][0]["code"] is None


def test_safe_provider_sentence_is_published_before_completion(monkeypatch):
    store = mongomock.MongoClient().test
    request, turn = make_turn("哈囉")
    monkeypatch.setattr(public_turn, "build_public_agent_turn_context", lambda *_a, **_k: turn)
    monkeypatch.setattr(public_turn, "validated_mentioned_contact_ids", lambda *_a: ([], False))
    release = threading.Event()
    published = threading.Event()
    tokens: list[str] = []
    result: dict[str, AgentResult] = {}

    def provider(*_a, **kwargs):
        kwargs["on_token"]("你好，我先看懂你的問題。")
        assert release.wait(2)
        kwargs["on_token"]("答案馬上來。")
        return ToolCallResult(content="你好，我先看懂你的問題。答案馬上來。", tool_calls=[])

    def collect(fragment: str) -> None:
        tokens.append(fragment)
        published.set()

    def run() -> None:
        result["value"] = public_turn.run_pi_public_turn(
            request,
            on_token=collect,
            confirmation_collection=store.c,
            contact_selection_collection=store.s,
            operation_batch_collection=store.b,
            runs_collection=store.r,
        )

    monkeypatch.setattr(pi_runtime, "generate_chat_completion_with_tools", provider)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert published.wait(5)
        assert "".join(tokens) == "你好，我先看懂你的問題。"
    finally:
        release.set()
        worker.join(5)

    assert not worker.is_alive()
    assert result["value"].reply == "你好，我先看懂你的問題。答案馬上來。"
    assert "".join(tokens) == result["value"].reply
