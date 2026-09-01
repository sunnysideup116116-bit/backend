import asyncio
import builtins
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch

from fastapi import BackgroundTasks, Request

from models import DirectChatRequest

# Offline suite injects a minimal config module before discovery.
if "config" in sys.modules and not hasattr(sys.modules["config"], "OLLAMA_FAST_CHAT_MODEL"):
    setattr(sys.modules["config"], "OLLAMA_FAST_CHAT_MODEL", "test")

from routers.public_chat import (
    _complete_public_turn,
    _run_public_stream_turn,
    _sanitize_public_stream_event,
    direct_chat,
    direct_chat_stream,
    queue_profile_skills,
)
from services.ayue_agent.contracts import AgentResult


async def _collect(response):
    return [chunk async for chunk in response.body_iterator]


def _request(host: str = "testclient", *, token_stream: bool = False) -> Request:
    headers = [(b"host", host.encode("ascii"))]
    if token_stream:
        headers.append((b"x-ayue-stream-tokens", b"v1"))
    return Request({
        "type": "http", "method": "POST", "scheme": "http",
        "path": "/api/direct_chat/stream", "raw_path": b"/api/direct_chat/stream",
        "query_string": b"", "headers": headers,
        "client": (host, 12345), "server": (host, 80),
    })


class AyueAgentStreamTests(unittest.TestCase):
    def test_assessment_from_another_room_is_removed_before_scheduler(self):
        req = DirectChatRequest(
            user_id="owner", contact_id="ai_assistant", message="普通聊天",
            ai_room_id="ai_room::owner::b",
        )
        history_cursor = MagicMock()
        history_cursor.sort.return_value.limit.return_value = []
        profile = {"user_id": "owner", "agentic_assessment_session": {
            "session_id": "assessment-a", "kind": "deep_profile",
            "status": "active", "revision": 2,
            "room_id": "ai_room::owner::a",
        }}
        with patch("routers.public_chat.messages_coll.find", return_value=history_cursor), \
             patch("routers.public_chat.profiles_coll.find_one", return_value=profile), \
             patch("routers.public_chat.run_public_agent_turn_v3", return_value=AgentResult(
                 handled=True, reply="我們聊別的。", agent_mode="v3",
             )) as run, \
             patch("routers.public_chat.complete_public_ayue_onboarding"), \
             patch("routers.public_chat.save_message"):
            response = _complete_public_turn(
                req, "ai_room::owner::b", [], background_tasks=None,
            )

        self.assertNotIn(
            "agentic_assessment_session", run.call_args.args[0].user_profile,
        )
        self.assertIsNone(response["assessment_state"])
        self.assertIsNone(response["assessment_kind"])

    def test_json_direct_chat_v3_does_not_load_removed_public_runtime(self):
        req = DirectChatRequest(user_id="owner", contact_id="ai_assistant", message="你好")
        real_import = builtins.__import__
        imported_requests = []
        history_cursor = MagicMock()
        history_cursor.sort.return_value.limit.return_value = []

        def record_import(name, globals=None, locals=None, fromlist=(), level=0):
            imported_requests.append((name, tuple(fromlist or ())))
            return real_import(name, globals, locals, fromlist, level)

        with patch("routers.public_chat.generate_room_id", return_value="room"), \
             patch("routers.public_chat.save_message", return_value={"message_id": "owner-message"}), \
             patch("routers.public_chat.messages_coll.find_one", return_value=None), \
             patch("routers.public_chat.messages_coll.find", return_value=history_cursor), \
             patch("routers.public_chat.matches_coll.find", return_value=[]), \
             patch("routers.public_chat.profiles_coll.update_one"), \
             patch("routers.public_chat.profiles_coll.find_one", return_value={"user_id": "owner"}), \
             patch("routers.public_chat._complete_public_turn", return_value={
                 "reply": "嗨。", "agent_version": "v3", "agent_run_id": "run-v3",
             }) as complete_v3, \
             patch("builtins.__import__", side_effect=record_import):
            response = direct_chat(req, BackgroundTasks())

        self.assertEqual(response["agent_version"], "v3")
        complete_v3.assert_called_once()
        self.assertFalse(any(
            name == "services.ayue_agent.runtime"
            or (name == "services.ayue_agent" and "runtime" in fromlist)
            for name, fromlist in imported_requests
        ))

    def test_non_public_provider_failure_returns_readable_fallback(self):
        req = DirectChatRequest(user_id="owner", contact_id="other", message="嗨")
        history_cursor = MagicMock()
        history_cursor.sort.return_value.limit.return_value = []
        risk_decision = MagicMock(may_persist=True)
        risk_decision.public_projection.return_value = {
            "level": "safe", "delivery": "delivered", "ui_priority": "coach",
        }
        with patch("routers.public_chat.generate_room_id", return_value="pair-room"), \
             patch("routers.public_chat._validated_requested_mentions", return_value=([], False)), \
             patch("routers.public_chat.save_pair_owner_message_once", return_value={
                 "message_id": "message-1", "created": True,
             }), \
             patch("routers.public_chat.save_message", return_value={"message_id": "opening-1"}), \
             patch("routers.public_chat.pair_message_risk_gate.evaluate", return_value=risk_decision), \
             patch("routers.public_chat.queue_profile_skills"), \
             patch("routers.public_chat.profiles_coll.update_one"), \
             patch("routers.public_chat.profiles_coll.find_one", return_value={}), \
             patch("routers.public_chat.messages_coll.find", return_value=history_cursor), \
             patch("routers.public_chat.messages_coll.count_documents", return_value=1), \
             patch("routers.public_chat.matches_coll.find", return_value=[]), \
             patch("routers.public_chat.matches_coll.find_one", return_value=None), \
             patch("routers.public_chat.find_accepted_match", return_value={"_id": "match-1"}), \
             patch("routers.public_chat.generate_chat_completion", side_effect=RuntimeError("provider down")), \
             patch("routers.public_chat.mark_post_chat_activity", return_value=0):
            response = direct_chat(req, BackgroundTasks())

        self.assertEqual(response["reply"], "我先陪你把這段聊完，剛剛回覆沒有成功，再試一次就好。")
        self.assertTrue(response["opening_assist"])
        self.assertNotIn("銝", response["reply"])

    def test_non_public_ai_opening_assist_runs_only_for_first_pair_message(self):
        req = DirectChatRequest(user_id="owner", contact_id="other", message="第二句")
        risk_decision = MagicMock(may_persist=True)
        risk_decision.public_projection.return_value = {
            "level": "safe", "delivery": "delivered", "ui_priority": "coach",
        }
        with patch("routers.public_chat.generate_room_id", return_value="pair-room"), \
             patch("routers.public_chat._validated_requested_mentions", return_value=([], False)), \
             patch("routers.public_chat.find_accepted_match", return_value={"_id": "match-1"}), \
             patch("routers.public_chat.pair_message_risk_gate.evaluate", return_value=risk_decision), \
             patch("routers.public_chat.save_pair_owner_message_once", return_value={
                 "message_id": "message-2", "created": True,
             }), \
             patch("routers.public_chat.profiles_coll.update_one"), \
             patch("routers.public_chat.messages_coll.count_documents", return_value=2), \
             patch("routers.public_chat.mark_post_chat_activity", return_value=2), \
             patch("routers.public_chat.track_message_metrics"), \
             patch("routers.public_chat.generate_chat_completion") as generate, \
             patch("routers.public_chat.save_message") as save:
            response = direct_chat(req, BackgroundTasks())

        self.assertEqual(response["reply"], "")
        self.assertFalse(response["opening_assist"])
        generate.assert_not_called()
        save.assert_not_called()

    def test_public_profile_off_does_not_enqueue_an_alternate_observer(self):
        tasks = MagicMock()
        with patch("routers.public_chat.profile_skills_mode_for_user", return_value="off"):
            mode = queue_profile_skills(
                tasks, "owner", "我想去爬山", "message-1", "global",
            )
        self.assertEqual(mode, "off")
        tasks.add_task.assert_not_called()

    def test_public_stream_path_saves_owner_and_final_once_each(self):
        req = DirectChatRequest(
            user_id="owner", contact_id="ai_assistant", message="你好",
            mentioned_other_id="seed_user_01",
        )
        history_cursor = MagicMock()
        history_cursor.sort.return_value.limit.return_value = []
        with patch("routers.public_chat.generate_room_id", return_value="room"), \
             patch("routers.public_chat._validated_requested_mentions", return_value=(["seed_user_01"], False)), \
             patch("routers.public_chat._mention_display_prefix", return_value="@對方"), \
             patch("routers.public_chat.save_message", side_effect=[
                 {"message_id": "owner-message"}, {"message_id": "assistant-message"},
             ]) as save_message, \
             patch("routers.public_chat.queue_profile_skills", return_value="on") as queue_profile, \
             patch("routers.public_chat.profiles_coll.update_one"), \
             patch("routers.public_chat.profiles_coll.find_one", return_value={"user_id": "owner"}), \
             patch("routers.public_chat.messages_coll.find", return_value=history_cursor), \
             patch("routers.public_chat.run_public_agent_turn_v3", return_value=AgentResult(
                 handled=True, reply="嗨。", agent_run_id="a" * 32, agent_mode="v2",
                 profile_write_allowed=False,
             )):
            response = _run_public_stream_turn(req, BackgroundTasks(), lambda _event: None)
        self.assertEqual(
            save_message.call_args_list,
            [
                call("room", "owner", "@對方 你好", metadata={
                    "owner_raw_content": "你好", "mention_labels": ["對方"],
                }),
                call("room", "ai_assistant", "嗨。", metadata={"agent_run_id": "a" * 32}),
            ],
        )
        queue_profile.assert_called_once_with(
            ANY, "owner", "你好", "owner-message", "global",
            progress_token=ANY,
        )
        self.assertEqual(response["reply"], "嗨。")
        self.assertTrue(response["profile_update_pending"])
        self.assertRegex(response["profile_process_run_key"], r"^[0-9a-f]{32}$")

    def test_assessment_answer_does_not_enter_profile_extractor_and_returns_state(self):
        req = DirectChatRequest(user_id="owner", contact_id="ai_assistant", message="我偏好先規劃好")
        history_cursor = MagicMock()
        history_cursor.sort.return_value.limit.return_value = []
        with patch("routers.public_chat.generate_room_id", return_value="room"), \
             patch("routers.public_chat.save_message", side_effect=[
                 {"message_id": "owner-message"}, {"message_id": "assistant-message"},
             ]), \
             patch("routers.public_chat.profiles_coll.update_one"), \
             patch("routers.public_chat.profiles_coll.find_one", return_value={"user_id": "owner"}), \
             patch("routers.public_chat.messages_coll.find", return_value=history_cursor), \
             patch("routers.public_chat.queue_profile_skills") as queue_profile, \
             patch("routers.public_chat.run_public_agent_turn_v3", return_value=AgentResult(
                 handled=True, reply="遇到臨時變動時呢？", agent_run_id="assessment-run", agent_mode="v2",
                 profile_write_allowed=False, profile_write_reason="assessment",
                 assessment_state="active", assessment_kind="big_five", assessment_revision=2,
             )):
            response = _run_public_stream_turn(req, BackgroundTasks(), lambda _event: None)
        queue_profile.assert_not_called()
        self.assertFalse(response["profile_update_pending"])
        self.assertEqual(response["assessment_state"], "active")
        self.assertEqual(response["assessment_kind"], "big_five")
        self.assertEqual(response["assessment_revision"], 2)

    def test_typed_assessment_cancel_is_saved_once_and_keeps_assessment_out_of_profile_extractor(self):
        req = DirectChatRequest(
            user_id="owner", contact_id="ai_assistant", message="client text",
            assessment_action="cancel",
        )
        history_cursor = MagicMock()
        history_cursor.sort.return_value.limit.return_value = []
        with patch("routers.public_chat.generate_room_id", return_value="room"), \
             patch("routers.public_chat.save_message", side_effect=[
                 {"message_id": "owner-message"}, {"message_id": "assistant-message"},
             ]) as save, \
             patch("routers.public_chat.profiles_coll.update_one"), \
             patch("routers.public_chat.profiles_coll.find_one", return_value={
                 "agentic_assessment_session": {"status": "cancelled", "kind": "deep_profile", "revision": 4},
             }), \
             patch("routers.public_chat.messages_coll.find", return_value=history_cursor), \
             patch("routers.public_chat.queue_profile_skills") as queue_profile, \
             patch("routers.public_chat.run_public_agent_turn_v3", return_value=AgentResult(
                 handled=True, reply="這段探索已取消。", agent_run_id="assessment-cancel-run", agent_mode="v3",
                 profile_write_allowed=False, profile_write_reason="assessment",
                 assessment_state="cancelled", assessment_kind="deep_profile", assessment_revision=4,
             )) as run:
            response = _run_public_stream_turn(req, BackgroundTasks(), lambda _event: None)
        self.assertEqual(response["assessment_state"], "cancelled")
        self.assertEqual(response["assessment_kind"], "deep_profile")
        self.assertEqual(save.call_args_list[0].args[2], "退出測驗")
        self.assertEqual(save.call_count, 2)
        self.assertEqual(run.call_args.args[0].assessment_action, "cancel")
        queue_profile.assert_not_called()

    def test_profile_update_polling_has_no_visible_process_bubble(self):
        source = (
            Path(__file__).resolve().parents[1] / "frontend.html"
        ).read_text(encoding="utf-8")
        self.assertIn("async function pollRecentContextUpdate(runKey, contactId)", source)
        self.assertNotIn("profileProcessTasks", source)
        self.assertNotIn('dataset.processKind = "recent-context"', source)
        self.assertNotIn("我整理一下你剛提到的近況", source)
        self.assertIn("clearAyueProgressBubble", source)
        self.assertNotIn("近期情境已更新", source)

    def test_match_search_progress_is_an_ephemeral_in_flow_chat_bubble(self):
        source = (
            Path(__file__).resolve().parents[1] / "frontend.html"
        ).read_text(encoding="utf-8")
        self.assertNotIn('id="match-search-process-layer"', source)
        self.assertIn('id = "match-search-process-bubble"', source)
        self.assertIn('const matchSearchProcessText = {', source)
        for text in ("先把你的近況整理好", "阿月正在翻翻名單", "看看你們是不是真的合拍", "把這次介紹整理得可愛一點"):
            self.assertIn(text, source)
        self.assertIn('kicker.textContent = "阿月努力牽線中 ✦"', source)
        self.assertIn('iconImage.src = "/images/ayue-match.png"', source)
        self.assertIn('icon.appendChild(iconImage)', source)
        self.assertIn('document.createTextNode("努力中")', source)
        self.assertIn('track.setAttribute("role", "progressbar")', source)
        self.assertIn('className = "match-search-process-bar"', source)
        self.assertIn('className = "chat-bubble bubble-other match-search-process-bubble fade-in"', source)
        self.assertIn('messages.appendChild(bubble)', source)
        self.assertNotIn('layer.replaceChildren(bubble)', source)
        self.assertIn('className = "match-search-collapse"', source)
        self.assertIn('if (isPublicAyue) refreshMatchStatus();', source)
        self.assertEqual(source.count("setTimeout(refreshMatchStatus, 100)"), 1)
        self.assertNotIn("match-progress-card", source)

    def test_debug_trace_shows_direct_chat_fast_path_or_fallback(self):
        source = (
            Path(__file__).resolve().parents[1] / "frontend.html"
        ).read_text(encoding="utf-8")
        self.assertIn('event.mode === "direct_chat"', source)
        self.assertIn("direct_chat · Fast Path", source)
        self.assertIn("Direct Chat Fast Path", source)
        self.assertIn("Direct Chat 未採用", source)
        self.assertIn("direct_chat_fallback_reason", source)

    def test_giphy_message_is_rendered_from_typed_safe_media(self):
        source = (
            Path(__file__).resolve().parents[1] / "frontend.html"
        ).read_text(encoding="utf-8")
        self.assertIn('messageData.message_type === "gif"', source)
        self.assertIn('parsed.protocol === "https:"', source)
        self.assertIn('host === "giphy.com" || host.endsWith(".giphy.com")', source)
        self.assertIn('provider.textContent = "via GIPHY"', source)
        self.assertIn('image.loading = "lazy"', source)
        self.assertIn('appendGiphyMessage(container, text, messageData)', source)

    def test_public_chat_uses_inline_mentions_and_typed_place_cards(self):
        source = (
            Path(__file__).resolve().parents[1] / "frontend.html"
        ).read_text(encoding="utf-8")
        self.assertIn('contenteditable="false"', source)
        self.assertIn('className = "inline-mention"', source)
        self.assertIn("mentions_inline: isPublicAyue", source)
        self.assertIn('event.key !== "Enter" || event.shiftKey', source)
        self.assertIn('mention_labels: outgoingMentions.map(mentionLabel)', source)
        self.assertIn("appendMessageText(div, text", source)
        self.assertIn("function appendAssistantMarkdown(container, text)", source)
        self.assertIn("function appendInlineMarkdown(container, text)", source)
        self.assertIn("appendAssistantMarkdown(div, text)", source)
        self.assertIn('document.createElement(token.startsWith("**") ? "strong" : "code")', source)
        self.assertIn('container.classList.add("message-markdown")', source)
        self.assertIn('gmp-place-details-compact', source)
        self.assertIn('className = "place-card place-card-custom"', source)
        self.assertNotIn('className = "place-card-reason"', source)
        self.assertNotIn('label.textContent = "阿月說明"', source)
        self.assertNotIn("card_description", source)
        self.assertNotIn("appendPlaceCardDescription", source)
        self.assertIn('url.pathname !== "/export/embed.html"', source)
        self.assertIn('frame.loading = "lazy"', source)
        self.assertIn('appendPlaceCards(div', source)

    def test_calendar_mutation_response_invalidates_open_calendar_cache(self):
        source = (
            Path(__file__).resolve().parents[1] / "frontend.html"
        ).read_text(encoding="utf-8")
        self.assertIn("data.calendar_state_changed", source)
        self.assertIn('document.getElementById("calendar-modal")', source)
        self.assertIn("await loadCalendarEvents()", source)

    def test_debug_panel_escapes_final_reply_before_inner_html(self):
        source = (
            Path(__file__).resolve().parents[1] / "frontend.html"
        ).read_text(encoding="utf-8")
        self.assertIn("function escapeDebugHtml(value)", source)
        self.assertIn('escapeDebugHtml((r.reply || "").substring(0, 200))', source)
        self.assertIn("escapeDebugHtml(finalReply.substring(0, 300))", source)

    def test_local_debug_panel_has_bounded_stream_and_structured_v3_details(self):
        source = (
            Path(__file__).resolve().parents[1] / "frontend.html"
        ).read_text(encoding="utf-8")
        self.assertIn("new AbortController()", source)
        self.assertIn("controller.abort(), 120000", source)
        self.assertIn("/api/debug/ayue-runs/", source)
        self.assertIn("任務 DAG／層級關係", source)
        self.assertIn("餵入資料", source)
        self.assertIn("可用 Functions", source)
        self.assertIn("Executor args", source)

        ai_source = (
            Path(__file__).resolve().parents[1] / "services" / "ai_service.py"
        ).read_text(encoding="utf-8")
        self.assertIn("timeout=OLLAMA_REQUEST_TIMEOUT_SECONDS", ai_source)

    def test_public_assessment_exit_control_uses_typed_action_without_confirmation(self):
        source = (Path(__file__).resolve().parents[1] / "frontend.html").read_text(encoding="utf-8")
        self.assertIn('id="assessment-control-bar"', source)
        self.assertIn('id="assessment-exit-button"', source)
        self.assertIn('id="assessment-control-label"', source)
        self.assertIn('assessment_action = assessmentAction', source)
        self.assertIn('payload.assessment_action = assessmentAction', source)
        self.assertIn('value = "退出測驗"', source)
        self.assertIn('function updateAssessmentControls', source)
        self.assertIn('if (!open)', source)
        self.assertIn('"active", "awaiting_commit"', source)
        self.assertIn('role="status"', source)
        self.assertIn('font-bold text-white', source)
        self.assertIn('bg-rose-500', source)
        self.assertIn('id="assessment-control-state"', source)
        self.assertIn('id="assessment-control-detail"', source)
        self.assertIn('bg-gradient-to-r', source)
        self.assertIn('回答會自動接續下一題', source)
        self.assertIn('確認後才會更新原本資料', source)
        self.assertNotIn("window.confirm(", source)

    def test_private_mediator_hides_paused_fun_features_and_uses_ayue_label(self):
        source = (
            Path(__file__).resolve().parents[1] / "frontend.html"
        ).read_text(encoding="utf-8")
        self.assertNotIn(">問趣事<", source)
        self.assertNotIn(">默契小測驗<", source)
        self.assertNotIn("阿月助攻進度", source)
        self.assertIn('label.textContent = "阿月"', source)

    def test_relationship_notification_uses_public_contact_name_not_internal_id(self):
        source = (
            Path(__file__).resolve().parents[1] / "frontend.html"
        ).read_text(encoding="utf-8")
        self.assertIn('contact?.name || "對方"', source)
        self.assertNotIn('"我在你和 " + payload.other_id', source)
        self.assertNotIn('selectContact(payload.other_id, payload.other_id', source)

    def test_public_stream_emits_only_safe_progress_and_compatible_final_response(self):
        req = DirectChatRequest(user_id="owner", contact_id="ai_assistant", message="今天幾月幾號")

        def fake_turn(_req, _tasks, emit, on_token=None):
            emit({"type": "run_started", "agent_run_id": "run-1"})
            emit({
                "type": "plan_created", "agent_run_id": "run-1",
                "prompt_raw": "seed_user_08 private prompt",
                "content_raw": "private model output",
                "plan": [{"task_brief": "private brief"}],
            })
            emit({
                "type": "subagent_started", "agent_run_id": "run-1",
                "agent": "calendar", "prompt_raw": "private calendar prompt",
                "task_brief": "private calendar brief",
            })
            emit({
                "type": "subagent_started", "agent_run_id": "run-1",
                "agent": "synthesizer", "prompt_raw": "private synth prompt",
            })
            emit({
                "type": "subagent_finished", "agent_run_id": "run-1",
                "prompt_raw": "private prompt",
                "tool_calls_raw": [{"arguments": {"event_id": "secret"}}],
            })
            emit({
                "type": "tool_started", "agent_run_id": "run-1", "step_id": "0:read",
                "text": "我確認一下現在的時間…", "arguments": {"user_id": "seed_user_08"},
            })
            emit({
                "type": "tool_finished", "agent_run_id": "run-1", "step_id": "0:read",
                "outcome": "ok", "result": {"revision": 99},
            })
            return {"reply": "今天是 2026-07-30。", "agent_version": "v2", "agent_run_id": "run-1"}

        with             patch("routers.public_chat._run_public_stream_turn", side_effect=fake_turn):
            response = direct_chat_stream(req, BackgroundTasks(), _request())
            chunks = asyncio.run(_collect(response))
        events = [json.loads(chunk) for chunk in chunks]
        self.assertEqual(
            [event["type"] for event in events],
            ["run_started", "stage", "stage", "tool_started", "tool_finished", "final"],
        )
        self.assertEqual(events[1]["stage"], "checking_calendar")
        self.assertEqual(events[1]["text"], "阿月正在確認你的行事曆…")
        self.assertEqual(events[2]["stage"], "composing")
        self.assertEqual(events[2]["text"], "阿月正在整理回覆…")
        self.assertEqual(events[-1]["response"]["reply"], "今天是 2026-07-30。")
        public_text = json.dumps(events, ensure_ascii=False)
        for forbidden in (
            "arguments", "revision", "seed_user_", "prompt", "result_summary",
            "plan_created", "subagent_started", "subagent_finished", "task_brief",
            "step_id", "tool_name",
        ):
            self.assertNotIn(forbidden, public_text)

    def test_public_stream_publishes_bounded_token_fragments(self):
        req = DirectChatRequest(user_id="owner", contact_id="ai_assistant", message="說點什麼")
        callback_seen = threading.Event()
        callbacks = []

        def fake_turn(_req, _tasks, emit, on_token=None):
            callbacks.append(on_token)
            callback_seen.set()
            return {"reply": "你好，很高興認識你。", "agent_version": "v3", "agent_run_id": "run-tok"}

        with             patch("routers.public_chat._run_public_stream_turn", side_effect=fake_turn):
            direct_chat_stream(
                req, BackgroundTasks(), _request(token_stream=True),
            )
            self.assertTrue(callback_seen.wait(1))

        self.assertEqual(len(callbacks), 1)
        self.assertIsNotNone(callbacks[0])
        token = _sanitize_public_stream_event({
            "type": "token", "agent_run_id": "run-tok", "text": "字" * 900,
        })
        self.assertEqual(len(token["text"]), 600)

    def test_public_stream_does_not_publish_tokens_without_opt_in(self):
        req = DirectChatRequest(user_id="owner", contact_id="ai_assistant", message="說點什麼")
        callback_seen = threading.Event()
        callbacks = []

        def fake_turn(_req, _tasks, emit, on_token=None):
            callbacks.append(on_token)
            callback_seen.set()
            return {
                "reply": "你好。", "agent_version": "v3",
                "agent_run_id": "run-default",
            }

        with patch("routers.public_chat._run_public_stream_turn", side_effect=fake_turn):
            direct_chat_stream(req, BackgroundTasks(), _request())
            self.assertTrue(callback_seen.wait(1))

        self.assertEqual(callbacks, [None])

    def test_stream_hides_raw_exception_details(self):
        req = DirectChatRequest(user_id="owner", contact_id="ai_assistant", message="幫我查一下")
        with             patch("routers.public_chat._run_public_stream_turn", side_effect=RuntimeError("seed_user_08 raw database error")):
            response = direct_chat_stream(req, BackgroundTasks(), _request())
            chunks = asyncio.run(_collect(response))
        event = json.loads(chunks[0])
        self.assertEqual(event["type"], "error")
        self.assertIn("這件事還沒處理", event["reply"])
        self.assertNotIn("seed_user_08", event["reply"])
        self.assertNotIn("database", event["reply"])

    def test_run_owned_background_tasks_finish_after_stream_response(self):
        req = DirectChatRequest(user_id="owner", contact_id="ai_assistant", message="記得我想旅行")
        background_finished = threading.Event()

        def fake_turn(_req, tasks, emit, on_token=None):
            tasks.add_task(background_finished.set)
            emit({"type": "run_started", "agent_run_id": "run-background"})
            return {"reply": "我記得。", "agent_version": "v2", "agent_run_id": "run-background"}

        with             patch("routers.public_chat._run_public_stream_turn", side_effect=fake_turn):
            response = direct_chat_stream(req, BackgroundTasks(), _request())
            chunks = asyncio.run(_collect(response))
        self.assertEqual(json.loads(chunks[-1])["type"], "final")
        self.assertTrue(background_finished.wait(1), "run-owned background work did not finish")

    def test_unknown_progress_event_is_not_published(self):
        req = DirectChatRequest(user_id="owner", contact_id="ai_assistant", message="聊天")

        def fake_turn(_req, _tasks, emit, on_token=None):
            emit({"type": "debug", "prompt": "seed_user_08", "result": {"private": True}})
            return {"reply": "好呀。", "agent_version": "v2", "agent_run_id": "run-safe"}

        with             patch("routers.public_chat._run_public_stream_turn", side_effect=fake_turn):
            response = direct_chat_stream(req, BackgroundTasks(), _request())
            chunks = asyncio.run(_collect(response))
        events = [json.loads(chunk) for chunk in chunks]
        self.assertEqual([event["type"] for event in events], ["final"])
        self.assertNotIn("seed_user_08", json.dumps(events, ensure_ascii=False))

    def test_private_redirect_prefills_public_without_auto_submit(self):
        source = (Path(__file__).resolve().parents[1] / "frontend.html").read_text(encoding="utf-8")
        start = source.index("async function handoffPrivateToPublic")
        end = source.index("function setPrivateProgress", start)
        helper = source[start:end]
        self.assertIn("closeMediatorPrivatePanel()", helper)
        self.assertIn('selectContact(\n                "ai_assistant"', helper)
        self.assertIn("clearMention()", helper)
        self.assertIn("input.value = message", helper)
        self.assertIn('new Event("input", {bubbles: true})', helper)
        self.assertIn("input.focus()", helper)
        self.assertNotIn("requestSubmit", helper)
        self.assertNotIn("sendPrivateMediatorMessage", helper)


if __name__ == "__main__":
    unittest.main()
