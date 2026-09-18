"""Focused contracts for the isolated Private Pi runtime."""

from __future__ import annotations

from unittest.mock import patch

from services.ayue_agent.private_pi import runtime
from services.ayue_agent.private_pi.context import PrivatePiTurnContext
from services.ayue_agent.private_pi.policy import PRIVATE_PI_POLICY
from services.ayue_agent.private_pi.registry import (
    PRIVATE_TOOL_MAP,
    PRIVATE_TOOL_NAMES,
    tool_schemas,
)
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
            {
                "topic": "相處感受",
                "owner_view": "我覺得很自在、我覺得對方很帥",
                "views": [
                    {"text": "我覺得很自在", "updated_at": 100},
                    {"text": "我覺得對方很帥", "updated_at": 110},
                ],
                "source": "owner_private",
            },
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
    assert "private.relationship.respond_to_probe" not in PRIVATE_TOOL_NAMES
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
    memory = safe["owner_relationship_memories"][0]
    assert memory["owner_view"] == "我覺得很自在、我覺得對方很帥"
    assert [item["text"] for item in memory["views"]] == [
        "我覺得很自在", "我覺得對方很帥",
    ]


def test_private_policy_requires_contextual_owner_view_memory_capture():
    description = PRIVATE_TOOL_MAP[
        "private.relationship.capture_memory_candidate"
    ].description
    assert "每回合的關係記憶判斷" in PRIVATE_PI_POLICY
    assert "它好冷漠" in PRIVATE_PI_POLICY
    assert "必須先呼叫 private.relationship.capture_memory_candidate" in PRIVATE_PI_POLICY
    assert "只有本回合訊息" in description
    assert "查看或管理既有記憶的要求不得呼叫此工具" in description
    assert "必須呼叫 private.surface.present_relationship_memories" in PRIVATE_PI_POLICY


def test_model_can_present_relationship_memory_entry_with_copy_and_placement():
    ctx = _context("讓我看看你記得哪些關於他的事")
    progress: list[dict] = []
    with patch.object(runtime, "pi_available", return_value=True), \
         patch.object(runtime, "build_private_pi_context", return_value=ctx), \
         patch.object(
             runtime,
             "run_bridge",
             side_effect=_bridge_that_calls(
                 "private.surface.present_relationship_memories",
                 {
                     "title": "關於小晴，我記得這些",
                     "summary": "你可以進去確認，也能修改或撤銷。",
                     "label": "看看記憶",
                    "placement": "before_answer",
                 },
                 reply="都整理在這裡。",
             ),
         ):
        result = runtime.run_private_pi_turn(
            user_id="owner",
            other_id="other",
            message=ctx.base.message,
            match_doc={"_id": "match", "status": "accepted", "proposal_revision": 3},
            on_progress=progress.append,
        )

    assert result.relationship_memory_entry is not None
    assert result.relationship_memory_entry.model_dump() == {
        "kind": "relationship_memory_entry",
        "title": "關於小晴，我記得這些",
        "summary": "你可以進去確認，也能修改或撤銷。",
        "label": "看看記憶",
        "placement": "before_reply",
    }
    assert any(
        event.get("type") == "tool_started" and "記憶入口" in event.get("text", "")
        for event in progress
    )


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


def test_fixed_fallback_is_reserved_for_a_provider_timeout():
    ctx = _context("幫我整理一下")

    def bridge(_initial, model_call, _tool_call, **_kwargs):
        response = model_call([], 10**12, [])
        return {"finalText": "", "error": response.get("error")}

    with (
        patch.object(runtime, "pi_available", return_value=True),
        patch.object(runtime, "build_private_pi_context", return_value=ctx),
        patch.object(runtime, "run_bridge", side_effect=bridge),
        patch.object(runtime, "generate_chat_completion_with_tools", side_effect=TimeoutError),
    ):
        timeout_result = runtime.run_private_pi_turn(
            user_id="owner", other_id="other", message=ctx.base.message,
            match_doc={"_id": "match", "status": "accepted", "proposal_revision": 3},
        )

    assert timeout_result.reply == runtime.PRIVATE_RUNTIME_FALLBACK_REPLY
    assert timeout_result.fallback_reason == "pi_provider_timeout"

    with (
        patch.object(runtime, "pi_available", return_value=True),
        patch.object(runtime, "build_private_pi_context", return_value=ctx),
        patch.object(runtime, "run_bridge", side_effect=bridge),
        patch.object(
            runtime,
            "generate_chat_completion_with_tools",
            side_effect=ConnectionError("provider offline"),
        ),
    ):
        provider_result = runtime.run_private_pi_turn(
            user_id="owner", other_id="other", message=ctx.base.message,
            match_doc={"_id": "match", "status": "accepted", "proposal_revision": 3},
        )

    assert provider_result.reply != runtime.PRIVATE_RUNTIME_FALLBACK_REPLY
    assert provider_result.fallback_reason == "pi_provider_error"
    assert "模型服務" in provider_result.reply


def test_reply_validation_keeps_normal_system_tool_and_permission_language():
    reply = "這是一般的系統提醒；你可以說說想用哪個工具，以及需要什麼權限。"
    assert runtime._safe_reply(reply, []) == reply
    assert runtime._safe_reply("x" * 3601, []) is None
    assert runtime._safe_reply("這次會讀取 user_id", []) is None


def test_unavailable_or_context_failure_does_not_use_timeout_copy():
    ctx = _context("幫我整理一下")
    with patch.object(runtime, "pi_available", return_value=False):
        unavailable = runtime.run_private_pi_turn(
            user_id="owner", other_id="other", message=ctx.base.message,
            match_doc={"_id": "match", "status": "accepted"},
        )
    assert unavailable.reply != runtime.PRIVATE_RUNTIME_FALLBACK_REPLY
    assert unavailable.fallback_reason == "pi_runtime_unavailable"

    with (
        patch.object(runtime, "pi_available", return_value=True),
        patch.object(
            runtime,
            "build_private_pi_context",
            side_effect=RuntimeError("context down"),
        ),
    ):
        context_failure = runtime.run_private_pi_turn(
            user_id="owner", other_id="other", message=ctx.base.message,
            match_doc={"_id": "match", "status": "accepted"},
        )
    assert context_failure.reply != runtime.PRIVATE_RUNTIME_FALLBACK_REPLY
    assert context_failure.fallback_reason == "pi_context_failed"


def test_memory_candidate_is_enqueued_before_a_later_provider_failure():
    ctx = _context("我喜歡他，幫我記住")

    def bridge(initial, _model_call, tool_call, **_kwargs):
        tool_call(
            "private.relationship.capture_memory_candidate",
            {"candidates": [{
                "category": "impression",
                "evidence_span": "我喜歡他",
                "statement": "我對對方有好感",
            }]},
        )
        return {"finalText": "", "error": "pi_provider_error"}

    with (
        patch.object(runtime, "pi_available", return_value=True),
        patch.object(runtime, "build_private_pi_context", return_value=ctx),
        patch.object(runtime, "run_bridge", side_effect=bridge),
        patch.object(runtime, "enqueue_relationship_memory_extraction", return_value=True) as enqueue,
    ):
        result = runtime.run_private_pi_turn(
            user_id="owner", other_id="other", message=ctx.base.message,
            source_message_id="source-1",
            match_doc={"_id": "match", "status": "accepted", "proposal_revision": 3},
        )

    enqueue.assert_called_once_with(
        "owner", "other", "match", "source-1",
        candidates=[{
            "category": "impression",
            "evidence_span": "我喜歡他",
            "statement": "我對對方有好感",
        }],
    )
    assert result.relationship_memory_candidate == {
        "candidates": [{
            "category": "impression", "evidence_span": "我喜歡他",
            "statement": "我對對方有好感",
        }],
    }
    assert result.relationship_memory_entry is not None
    assert result.relationship_memory_entry.label == "查看或管理"
    assert result.fallback_reason == "pi_provider_error"


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
        prepared = manager.record_for_choice(
            user_id="owner",
            room_id=first.base.room_id,
            surface="private_ayue",
            choice_id=initial.choice_prompt["id"],
            require_pending=False,
        )
        assert prepared and prepared["expected_persisted_fingerprint"]
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


def test_date_choice_confirm_and_cancel_resolve_the_visible_card():
    for action, expected_state in (("confirm", "confirmed"), ("cancel", "cancelled")):
        PRIVATE_CONFIRMATIONS.clear()
        ctx = _context("幫我問他要不要一起安排約會？")
        match = {"_id": "match", "status": "accepted", "proposal_revision": 3}
        with patch.object(runtime, "pi_available", return_value=True), \
             patch.object(runtime, "build_private_pi_context", return_value=ctx), \
             patch.object(
                 runtime,
                 "run_bridge",
                 side_effect=_bridge_that_calls("private.date.start_coordination", reply=""),
             ), \
             patch.object(
                 runtime,
                 "_execute_write",
                 return_value=(True, "date_invite_created", None),
             ) as execute:
            initial = runtime.run_private_pi_turn(
                user_id="owner", other_id="other", message=ctx.base.message,
                source_message_id="source-1", match_doc=match,
            )
            manager = ConfirmationManager(PRIVATE_CONFIRMATIONS)
            assert manager.mark_presented(
                user_id="owner",
                origin_run_id=initial.agent_run_id,
                message_id="assistant-1",
                persisted_content=initial.reply,
            )
            resolved = runtime.run_private_pi_turn(
                user_id="owner", other_id="other", message="",
                choice_id=initial.choice_prompt["id"], choice_action=action,
                match_doc=match,
            )

        assert resolved.choice_resolution["state"] == expected_state
        assert manager.list_active(
            user_id="owner",
            room_id=ctx.base.room_id,
            surface="private_ayue",
            interaction_mode="bubble_buttons_v1",
        ) == []
        assert execute.call_count == (1 if action == "confirm" else 0)


def test_cancel_tool_requires_an_explicit_cancel_phrase():
    assert runtime._explicit_cancel_requested("取消這張確認")
    assert runtime._explicit_cancel_requested("先不要")
    assert not runtime._explicit_cancel_requested("要不要一起安排約會？")


def test_memory_tool_trusts_agent_semantics_but_requires_source_evidence():
    invalid = _context("我只想安排約會")
    feeling = _context("我很在乎他")
    progress: list[dict] = []
    enqueue_patch = patch.object(runtime, "enqueue_relationship_memory_extraction", return_value=True)
    with patch.object(runtime, "pi_available", return_value=True), \
         patch.object(runtime, "build_private_pi_context", side_effect=[invalid, feeling]), \
         patch.object(
             runtime,
             "run_bridge",
             side_effect=lambda initial, model, tool, **kwargs: (
                 _bridge_that_calls(
                     "private.relationship.capture_memory_candidate",
                     {
                         "category": "future_intent",
                         "evidence_span": "訊息裡沒有這句",
                         "statement": "我想安排一次約會",
                     },
                 )(initial, model, tool, **kwargs)
                 if "只想安排約會" in initial["prompt"]
                 else _bridge_that_calls(
                     "private.relationship.capture_memory_candidate",
                     {
                         "category": "impression",
                         "evidence_span": "我很在乎他",
                         "statement": "我很重視和對方的關係",
                     },
                 )(initial, model, tool, **kwargs)
             ),
             ), enqueue_patch as enqueue:
        rejected = runtime.run_private_pi_turn(
            user_id="owner", other_id="other", message=invalid.base.message,
            source_message_id="m-invalid",
            match_doc={"_id": "match", "status": "accepted", "proposal_revision": 3},
        )
        accepted = runtime.run_private_pi_turn(
            user_id="owner", other_id="other", message=feeling.base.message,
            source_message_id="m-feeling",
            match_doc={"_id": "match", "status": "accepted", "proposal_revision": 3},
            on_progress=progress.append,
        )

    assert rejected.relationship_memory_candidate is None
    assert accepted.relationship_memory_candidate == {
        "candidates": [{
            "category": "impression", "evidence_span": "我很在乎他",
            "statement": "我很重視和對方的關係",
        }],
    }
    assert accepted.relationship_memory_entry is not None
    assert enqueue.call_count == 1
    assert any(
        event.get("type") == "tool_started"
        and "記憶整理" in str(event.get("text") or "")
        for event in progress
    )
    assert any(
        event.get("type") == "tool_finished" and event.get("outcome") == "ok"
        for event in progress
    )


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
