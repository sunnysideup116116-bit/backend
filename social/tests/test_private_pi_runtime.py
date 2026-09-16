"""Focused contracts for the isolated Private Pi runtime."""

from __future__ import annotations

from unittest.mock import patch

from services.ayue_agent.private_pi import runtime
from services.ayue_agent.private_pi.context import PrivatePiTurnContext
from services.ayue_agent.private_pi.registry import PRIVATE_TOOL_NAMES, tool_schemas
from services.ayue_agent.private_v2 import PRIVATE_CONFIRMATIONS, PrivateAgentTurnContextV2
from services.ayue_agent.shared.confirmation import ConfirmationManager
from services.ayue_agent.shared.test_store import MemoryCollection


def _context(
    message: str,
    *,
    source_message_id: str = "source-1",
    profile_state: dict | None = None,
) -> PrivatePiTurnContext:
    base = PrivateAgentTurnContextV2(
        user_id="owner",
        other_id="other",
        room_id="mediator_private::owner::other",
        message=message,
        pair_revision=3,
        viewer_profile={"recent_context": "最近喜歡散步"},
        counterparty_shareable={"display_name": "小晴", "recent_context": "喜歡咖啡"},
        counterparty_advisory={"private_secret": "never expose"},
        shared_history=[
            {"role": "本人", "content": "我喜歡電影", "message_id": "hidden"},
            {"role": "對方", "content": "我也喜歡電影", "message_id": "hidden-2"},
        ],
        private_history=[
            {"role": "本人", "content": message},
            {"role": "阿月", "content": "我聽著～"},
        ],
        shared_facts=[
            {"evidence_id": "shared:1", "visibility": "shared_fact", "value": "電影"},
        ],
        local_time="2026-09-16 12:00",
        owner_relationship_memories=[
            {"topic": "相處感受", "owner_view": "我覺得很自在", "source": "owner_private"},
        ],
    )
    return PrivatePiTurnContext(
        base=base,
        source_message_id=source_message_id,
        profile_state=profile_state or {},
    )


def _bridge_that_calls(tool_name: str, arguments: dict | None = None, *, reply: str = "收到～"):
    def bridge(initial, _model_call, tool_call, **_kwargs):
        assert {item["name"] for item in initial["tools"]} == set(PRIVATE_TOOL_NAMES)
        tool_call(tool_name, arguments or {})
        return {"finalText": reply, "error": None}

    return bridge


def setup_function(_function):
    PRIVATE_CONFIRMATIONS.clear()


def teardown_function(_function):
    PRIVATE_CONFIRMATIONS.clear()


def test_registry_is_private_only_and_context_drops_authority_fields():
    assert all(name.startswith("private.") for name in PRIVATE_TOOL_NAMES)
    assert not any(name.startswith(("web.", "calendar.", "match.")) for name in PRIVATE_TOOL_NAMES)
    assert {schema["name"] for schema in tool_schemas()} == set(PRIVATE_TOOL_NAMES)

    ctx = _context("我該怎麼接電影話題？")
    history = ctx.history()
    assert all(item["role"] in {"user", "assistant"} for item in history)
    assert all(item["role"] != "user" or item["content"][0]["text"] != ctx.base.message for item in history)
    assert all("hidden" not in str(item) for item in history)
    safe = ctx.safe_context()
    serialized = str(safe)
    assert "never expose" not in serialized
    assert "hidden" not in serialized
    assert "shared:1" not in serialized
    assert "other_id" not in serialized
    assert safe["owner_relationship_memories"][0]["owner_view"] == "我覺得很自在"


def test_private_pi_reads_shared_history_and_emits_activity_events():
    ctx = _context("我們最近聊過什麼？")
    progress: list[dict] = []
    with patch.object(runtime, "pi_available", return_value=True), \
         patch.object(runtime, "build_private_pi_context", return_value=ctx), \
         patch.object(
             runtime,
             "run_bridge",
             side_effect=_bridge_that_calls("private.relationship.get_shared_history"),
         ):
        result = runtime.run_private_pi_turn(
            user_id="owner",
            other_id="other",
            message=ctx.base.message,
            match_doc={"_id": "match", "status": "accepted", "proposal_revision": 3},
            on_progress=progress.append,
        )

    assert result.agent_mode == "pi"
    assert result.conversation_intent == "private_advice"
    assert result.reply == "收到～"
    assert [event["type"] for event in progress] == [
        "stage", "tool_started", "tool_finished",
    ]
    assert all("private" not in str(event.get("text") or "").lower() for event in progress)


def test_date_coordination_reuses_one_confirmation_for_rephrased_turn():
    first = _context("幫我問他要不要一起安排約會？")
    second = _context("要不要一起安排約會？")
    bridge = _bridge_that_calls("private.date.start_coordination", reply="")
    with patch.object(runtime, "pi_available", return_value=True), \
         patch.object(runtime, "build_private_pi_context", side_effect=[first, second]), \
         patch.object(runtime, "run_bridge", side_effect=bridge):
        initial = runtime.run_private_pi_turn(
            user_id="owner", other_id="other", message=first.base.message,
            source_message_id="m-1",
            match_doc={"_id": "match", "status": "accepted", "proposal_revision": 3},
        )
        manager = ConfirmationManager(PRIVATE_CONFIRMATIONS)
        assert initial.choice_prompt and initial.choice_prompt["state"] == "pending"
        assert manager.bind_final_preview(
            user_id="owner", origin_run_id=initial.agent_run_id,
            final_content=initial.reply,
        )
        assert manager.mark_presented(
            user_id="owner", origin_run_id=initial.agent_run_id,
            message_id="assistant-1", persisted_content=initial.reply,
        )
        continued = runtime.run_private_pi_turn(
            user_id="owner", other_id="other", message=second.base.message,
            source_message_id="m-2",
            match_doc={"_id": "match", "status": "accepted", "proposal_revision": 3},
        )

    assert continued.reply == "上方的約會確認還有效；按下確認後我才會通知對方。"
    assert continued.choice_prompt is None
    records = manager.list_active(
        user_id="owner", room_id="mediator_private::owner::other",
        surface="private_ayue", interaction_mode="bubble_buttons_v1",
    )
    assert len(records) == 1


def test_cancel_tool_requires_an_explicit_cancel_phrase():
    assert runtime._explicit_cancel_requested("取消這張確認")
    assert runtime._explicit_cancel_requested("先不要")
    assert not runtime._explicit_cancel_requested("要不要一起安排約會？")


def test_memory_tool_only_queues_explicit_owner_feeling():
    operation = _context("我想要約他去約會，可以幫我安排嗎？")
    feeling = _context("我跟他相處很自在，希望慢慢認識。")
    enqueue_patch = patch.object(runtime, "enqueue_relationship_memory_extraction", return_value=True)
    with patch.object(runtime, "pi_available", return_value=True), \
         patch.object(runtime, "build_private_pi_context", side_effect=[operation, feeling]), \
         patch.object(
             runtime,
             "run_bridge",
             side_effect=lambda initial, model, tool, **kwargs: (
                 _bridge_that_calls(
                     "private.relationship.capture_memory_candidate",
                     {
                         "category": "future_intent",
                         "evidence_span": "我想要約他去約會",
                     },
                 )(initial, model, tool, **kwargs)
                 if "約他去約會" in initial["prompt"]
                 else _bridge_that_calls(
                     "private.relationship.capture_memory_candidate",
                     {
                         "category": "impression",
                         "evidence_span": "我跟他相處很自在",
                     },
                 )(initial, model, tool, **kwargs)
             ),
             ), enqueue_patch as enqueue:
        rejected = runtime.run_private_pi_turn(
            user_id="owner", other_id="other", message=operation.base.message,
            source_message_id="m-op",
            match_doc={"_id": "match", "status": "accepted", "proposal_revision": 3},
        )
        accepted = runtime.run_private_pi_turn(
            user_id="owner", other_id="other", message=feeling.base.message,
            source_message_id="m-feeling",
            match_doc={"_id": "match", "status": "accepted", "proposal_revision": 3},
        )

    assert rejected.relationship_memory_candidate is None
    assert accepted.relationship_memory_candidate == {
        "category": "impression", "evidence_span": "我跟他相處很自在",
    }
    assert enqueue.call_count == 1


def test_post_date_feedback_tool_does_not_require_calendar_jwt():
    pending = {
        "event_id": "date-1",
        "other_id": "other",
        "relationship_id": "match",
        "expires_at": 4_000_000_000,
    }
    ctx = _context(
        "今天聊得很開心，想再約一次。",
        profile_state={"pending_post_date_feedback": pending},
    )
    with patch.object(runtime, "pi_available", return_value=True), \
         patch.object(runtime, "build_private_pi_context", return_value=ctx), \
         patch.object(runtime, "run_bridge", side_effect=_bridge_that_calls(
             "private.relationship.record_post_date_feedback",
             {"action": "answer", "evidence_span": "今天聊得很開心"},
         )), \
         patch.object(runtime, "consume_pending_post_date_feedback", return_value="收到") as consume:
        result = runtime.run_private_pi_turn(
            user_id="owner", other_id="other", message=ctx.base.message,
            source_message_id="feedback-1",
            match_doc={"_id": "match", "status": "accepted", "proposal_revision": 3},
            external_calendar_authorized=False,
        )

    consume.assert_called_once()
    assert result.post_date_feedback_recorded is True


def test_explicit_choice_action_is_the_only_path_that_executes_date_write():
    ctx = _context("")
    with patch.object(runtime, "pi_available", return_value=True), \
         patch.object(runtime, "build_private_pi_context", return_value=ctx), \
         patch.object(runtime, "_execute_write", return_value=(True, "date_invite_created")) as execute, \
         patch.object(runtime, "messages_coll", MemoryCollection()):
        manager = ConfirmationManager(PRIVATE_CONFIRMATIONS)
        choice_id = manager.create_confirmation(
            user_id="owner", agent_name="private_relationship",
            tool_name="private.date.start_coordination", arguments={},
            payload={"other_id": "other", "pair_revision": 3},
            origin_run_id="seed-run", preview="要我現在幫你問對方願不願意一起安排約會嗎？",
            room_id=ctx.base.room_id, surface="private_ayue", interaction_mode="bubble_buttons_v1",
        )
        record = manager.choice_projection(
            user_id="owner", room_id=ctx.base.room_id,
            surface="private_ayue", choice_id=choice_id,
        )
        preview = "要我現在幫你問對方願不願意一起安排約會嗎？"
        assert manager.bind_final_preview(
            user_id="owner", origin_run_id="seed-run", final_content=preview,
        )
        manager.mark_presented(
            user_id="owner", origin_run_id="seed-run", message_id="assistant-seed",
            persisted_content=preview,
        )
        result = runtime.run_private_pi_turn(
            user_id="owner", other_id="other", message="確認",
            choice_id=choice_id, choice_action="confirm",
            match_doc={"_id": "match", "status": "accepted", "proposal_revision": 3},
        )

    execute.assert_called_once_with(
        "private.date.start_coordination", "owner", "other",
        {"_id": "match", "status": "accepted", "proposal_revision": 3},
    )
    assert "問對方" in result.reply
