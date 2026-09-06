"""Regression for request-scoped match context and ordinary search state."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services import match_search_job_service as jobs
from services.match_search_context import (
    extract_invitation_topic,
    safe_search_context,
    search_context_for_turn,
)
from services.ayue_agent.contracts import AgentTurnContext, PublicAgentTurnContext
from services.ayue_agent.v3 import write_executors as writes
from services.ayue_agent.v3.confirmation import ConfirmationManager
from services.ayue_agent.v3.match_runtime import status_reply
from services.ayue_agent.v3.planner import _planner_prompt
from tests.match_flow_store import Collection


OFFER = "我暫時找不到足夠的共同依據；要不要只放寬這次搜尋的情境範圍？"
EMPTY = {"status": "insufficient_common_ground", "reason_code": "insufficient_common_ground"}


@pytest.mark.parametrize("message", [
    "幫我找一個也會衝浪的人",
    "想配到會衝浪的人",
    "找會衝浪的人",
])
def test_explicit_topic_phrasings_bind_topic_and_original_query(message):
    assert extract_invitation_topic(message) == "衝浪"
    assert search_context_for_turn(message=message, message_id="source-1") == {
        "invitation_topic": "衝浪",
        "query_text": message,
        "source_message_id": "source-1",
    }


def test_search_context_is_bounded_and_legacy_scope_flags_are_ignored():
    result = safe_search_context({
        "allow_adjacent": True,
        "widened": True,
        "invitation_topic": "主題" * 100,
        "query_text": "查詢" * 400,
        "source_message_id": "source-1",
        "unexpected": "drop me",
    })
    assert set(result) == {"invitation_topic", "query_text", "source_message_id"}
    assert len(result["invitation_topic"]) <= 80
    assert len(result["query_text"]) <= 600


def test_generic_search_does_not_recover_an_old_topic_from_history():
    history = [
        {
            "role": "user",
            "content": "我想找人一起滑雪",
            "message_id": "old-topic",
        },
        {
            "role": "assistant",
            "content": "我可以幫你找可能對「滑雪」有興趣的夥伴，要我開始找嗎？",
        },
    ]
    for message in ("我想配對", "幫我找人", "依近期情境就好"):
        assert search_context_for_turn(
            message=message, message_id="new-search", history=history,
        ) == {}


def test_explicit_topic_continuation_is_kept_but_generic_new_person_is_not():
    assert search_context_for_turn(
        message="再找一個滑雪的", message_id="follow-up",
    ) == {
        "invitation_topic": "滑雪",
        "query_text": "再找一個滑雪的",
        "source_message_id": "follow-up",
    }
    assert search_context_for_turn(
        message="再找一個新的朋友", message_id="generic",
    ) == {}


def test_negated_topic_clears_a_stale_bound_context():
    assert search_context_for_turn(
        {"invitation_topic": "滑雪", "query_text": "我想找人一起滑雪"},
        message="沒有要滑雪，依近期情境就好",
        message_id="correction",
    ) == {}


def test_only_a_bare_confirmation_can_recover_the_immediately_offered_topic():
    history = [
        {
            "role": "user",
            "content": "我想找人一起滑雪",
            "message_id": "source",
        },
        {
            "role": "assistant",
            "content": "我可以幫你找可能對「滑雪」有興趣的夥伴，要我開始找嗎？",
        },
    ]
    assert search_context_for_turn(
        message="確認", message_id="confirmation", history=history,
    ) == {
        "invitation_topic": "滑雪",
        "query_text": "我想找人一起滑雪",
        "source_message_id": "source",
    }


def _context(message="ok", history=None):
    return AgentTurnContext(
        user_id="owner", room_id="room", message=message,
        recent_history=history if history is not None else [
            {"sender_id": "ai_assistant", "room_id": "room", "content": OFFER},
            {"sender_id": "owner", "room_id": "room", "content": message},
        ],
    )


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setattr(writes, "assess_match_opportunity", lambda *a, **kw: SimpleNamespace(state="ready"))


@pytest.mark.parametrize("message", ["ok", "OK!", "okay", "yes", "yes please", "sure", "好", "好啊！", "可以。", "願意"])
def test_answer_to_old_offer_does_not_create_widening_context(ready, message):
    payload, preview = writes.prepare_write_confirmation(
        "match.start_search", {}, _context(message), SimpleNamespace(match_search=EMPTY),
    )
    assert payload["data"] == {}
    assert "只放寬這次" not in preview and "開始" in preview


def test_waiting_other_can_start_a_fresh_search_without_touching_the_invitation(ready):
    turn = SimpleNamespace(
        match_search=EMPTY,
        active_proposal={"stage": "waiting_other"},
    )
    payload, preview = writes.prepare_write_confirmation(
        "match.start_search", {}, _context("我想配對", []), turn,
    )
    assert payload["action"] == "match.start_search"
    assert "原本那張邀請會繼續等對方回覆" in preview
    assert "依你最近的近況找" in preview


@pytest.mark.parametrize("message", ["不要放寬", "不用放寬", "先別放寬", "不要不同方向", "don't broaden", "不放寬，照原條件", "i want match"])
def test_negative_or_new_search_does_not_widen(ready, message):
    payload, _ = writes.prepare_write_confirmation(
        "match.start_search", {}, _context(message), SimpleNamespace(match_search=EMPTY),
    )
    assert not payload["data"].get("search_context", {}).get("allow_adjacent")


@pytest.mark.parametrize("history", [
    [],
    [{"sender_id": "owner", "content": OFFER}],
    [{"sender_id": "ai_assistant", "content": "要不要放寬週末的安排？"}],
    [{"sender_id": "ai_assistant", "content": OFFER}, {"sender_id": "ai_assistant", "content": "要聊聊今天嗎？"}],
    [{"sender_id": "ai_assistant", "content": OFFER, "room_id": "another-room"}],
    [{"sender_id": "ai_assistant", "content": OFFER}, {"sender_id": "owner", "content": "先聊其他事"}],
])
def test_bare_agreement_needs_immediate_assistant_search_offer(ready, history):
    payload, _ = writes.prepare_write_confirmation(
        "match.start_search", {}, _context("好", history), SimpleNamespace(match_search=EMPTY),
    )
    assert not payload["data"].get("search_context", {}).get("allow_adjacent")


@pytest.mark.parametrize("message", ["放寬這次情境範圍", "please broaden this search"])
def test_explicit_scope_change_requires_canonical_insufficient_result(ready, message):
    ctx = _context(message, [])
    payload, _ = writes.prepare_write_confirmation("match.start_search", {}, ctx, SimpleNamespace(match_search=EMPTY))
    assert not payload["data"].get("search_context")
    payload, _ = writes.prepare_write_confirmation("match.start_search", {}, ctx, SimpleNamespace(match_search={"status": "idle"}))
    assert not payload["data"].get("search_context")


def test_confirmed_topic_reaches_persisted_job_without_profile_mutation(ready, monkeypatch):
    ctx = _context("想找人一起衝浪", [])
    ctx.message_id = "owner-message-1"
    turn = SimpleNamespace(match_search=EMPTY)
    profile = {"user_id": "owner", "current_context_revision": 7, "current_context": "想找拍照的伴", "preferences": {"hard": "保留"}}
    profiles = Collection([profile])
    collection = Collection()
    monkeypatch.setattr(jobs, "MATCH_SEARCH_JOBS", collection)
    monkeypatch.setattr(jobs, "profiles_coll", profiles)
    monkeypatch.setattr(jobs, "_has_live_match", lambda _user: False)
    monkeypatch.setattr(jobs, "_live_match", lambda _user: None)
    monkeypatch.setattr(writes, "TOOL_CALLS", Collection())
    pipeline = MagicMock(return_value={"matches": [], "reason_code": "insufficient_common_ground"})
    monkeypatch.setattr(jobs, "_pipeline", pipeline)
    delivery = MagicMock()
    monkeypatch.setattr(jobs, "queue_mediator_event", delivery)

    pending, preview = writes.prepare_write_confirmation("match.start_search", {}, ctx, turn)
    manager = ConfirmationManager(Collection())
    cid = manager.create_confirmation(
        user_id="owner", agent_name="match", tool_name=pending["action"], arguments={},
        payload=pending["data"], origin_run_id="preview-run", preview=preview, room_id="room",
    )
    assert not collection.rows
    pipeline.assert_not_called()
    assert manager.bind_final_preview(user_id="owner", origin_run_id="preview-run", final_content=preview)
    assert manager.mark_presented(user_id="owner", origin_run_id="preview-run", message_id="preview-message", persisted_content=preview)

    def executor(tool_name, arguments, user_id, payload=None):
        assert user_id == ctx.user_id
        return writes.execute_write(tool_name, arguments, ctx, turn, "confirm-run", 0, confirmation_id=cid, payload=payload)

    result = manager.execute_confirmed(user_id="owner", choice_id=cid, room_id="room", executor=executor)
    assert result[0]["ok"]
    assert len(collection.rows) == 1
    assert collection.rows[0]["search_context"] == {
        "invitation_topic": "衝浪",
        "query_text": "想找人一起衝浪",
        "source_message_id": "owner-message-1",
    }
    assert collection.rows[0]["origin_room_id"] == "room"
    assert not manager.execute_confirmed(user_id="owner", choice_id=cid, room_id="room", executor=executor)
    assert len(collection.rows) == 1
    assert jobs.run_one_match_search_job()
    assert pipeline.call_args.kwargs["search_context"]["query_text"] == "想找人一起衝浪"
    assert "已放寬" not in delivery.call_args.args[1]
    assert "補充" in delivery.call_args.args[1]
    stored = profiles.find_one({"user_id": "owner"})
    assert stored["current_context"] == profile["current_context"]
    assert stored["preferences"] == profile["preferences"]
    assert collection.rows[0]["status"] == "insufficient_common_ground"
    assert jobs.enqueue_match_search("owner", source="agent_v3", idempotency_key="fresh", force_new=True)["status"] == "queued"
    assert collection.rows[1]["search_context"] == {}


@pytest.mark.parametrize("context", [{}, {"allow_adjacent": True}, {"widened": True}])
def test_worker_no_result_copy_never_reflects_retired_scope_flags(monkeypatch, context):
    job = {"job_id": "job", "user_id": "owner", "origin_room_id": "room", "search_context": context}
    for name in ("_report_progress", "_job_has_ownership", "_finish_job"):
        monkeypatch.setattr(jobs, name, MagicMock(return_value=True))
    monkeypatch.setattr(jobs, "_claim_next_job", lambda _now: job)
    monkeypatch.setattr(jobs, "_pipeline", MagicMock(return_value={"matches": [], "reason_code": "insufficient_common_ground"}))
    queue = MagicMock()
    monkeypatch.setattr(jobs, "queue_mediator_event", queue)
    assert jobs.run_one_match_search_job()
    text = queue.call_args.args[1]
    assert "已放寬" not in text
    assert "補充" in text
    assert queue.call_args.kwargs["origin_room_id"] == "room"
    assert queue.call_args.kwargs["event_key"] == "match-search-job:job:empty"


def test_insufficient_is_a_known_status_and_planner_gets_safe_reason():
    reply = status_reply({"state": "insufficient_common_ground"})
    assert "共同依據" in reply
    assert "無法確認" not in reply
    turn = PublicAgentTurnContext(user_id="owner", room_id="room", message="ok", match_search={**EMPTY, "job_id": "private-job"})
    prompt = _planner_prompt(turn)
    assert json.loads(prompt)["match_search"]["reason_code"] == "insufficient_common_ground"
    assert "private-job" not in prompt


def test_status_copy_uses_natural_waiting_and_server_quota_language():
    waiting = status_reply({"state": "waiting_other"})
    assert waiting == "那張邀請還在等對方回覆，你也可以繼續認識其他人。"
    available = status_reply({
        "state": "idle",
        "daily_quota": {"active": {"remaining": 2}},
    })
    assert "今天還可以請我介紹 2 位新朋友" in available
    assert "額度" not in available and "扣打" not in available
