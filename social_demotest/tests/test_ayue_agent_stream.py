import asyncio
import builtins
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch

from fastapi import BackgroundTasks

from models import DirectChatRequest

# Offline suite injects a minimal config module before discovery.
if "config" in sys.modules and not hasattr(sys.modules["config"], "OLLAMA_FAST_CHAT_MODEL"):
    setattr(sys.modules["config"], "OLLAMA_FAST_CHAT_MODEL", "test")

from routers.public_chat import _run_public_v2_stream_turn, direct_chat, direct_chat_stream, queue_profile_skills
from services.ayue_agent.contracts import AgentResult


async def _collect(response):
    return [chunk async for chunk in response.body_iterator]


class AyueAgentStreamTests(unittest.TestCase):
    def test_json_direct_chat_v2_does_not_load_legacy_routing(self):
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
             patch("routers.public_chat.profiles_coll.update_one"), \
             patch("routers.public_chat.profiles_coll.find_one", return_value={"user_id": "owner"}), \
             patch("routers.public_chat.agent_mode_for_user", return_value="on"), \
             patch("routers.public_chat._complete_public_v2_turn", return_value={
                 "reply": "嗨。", "agent_version": "v2", "agent_run_id": "run-v2",
             }) as complete_v2, \
             patch("builtins.__import__", side_effect=record_import):
            response = direct_chat(req, BackgroundTasks())

        self.assertEqual(response["agent_version"], "v2")
        complete_v2.assert_called_once()
        self.assertFalse(any(
            name == "services.ayue_agent.legacy_match_routing"
            or (name == "services.ayue_agent" and "legacy_match_routing" in fromlist)
            for name, fromlist in imported_requests
        ))

    def test_json_direct_chat_off_mode_executes_legacy_rollback_outcome_path(self):
        req = DirectChatRequest(user_id="owner", contact_id="ai_assistant", message="為什麼？")
        history_cursor = MagicMock()
        history_cursor.sort.return_value.limit.return_value = []
        with patch("routers.public_chat.generate_room_id", return_value="room"), \
             patch("routers.public_chat.save_message", side_effect=[
                 {"message_id": "owner-message"}, {"message_id": "assistant-message"},
             ]) as save_message, \
             patch("routers.public_chat.messages_coll.find_one", return_value={"content": "對方這次先婉拒了邀請。"}), \
             patch("routers.public_chat.messages_coll.find", return_value=history_cursor), \
             patch("routers.public_chat.profiles_coll.update_one"), \
             patch("routers.public_chat.profiles_coll.find_one", return_value={"user_id": "owner"}), \
             patch("routers.public_chat.agent_mode_for_user", return_value="off"), \
             patch("routers.public_chat.profile_skills_mode_for_user", return_value="off"), \
             patch("routers.public_chat.match_outcome_followup_reply", return_value="對方這次沒有接受。"), \
             patch("routers.public_chat._complete_public_v2_turn") as complete_v2:
            response = direct_chat(req, BackgroundTasks())

        self.assertEqual(response["conversation_intent"], "match_outcome_followup")
        self.assertEqual(response["reply"], "對方這次沒有接受。")
        complete_v2.assert_not_called()
        self.assertEqual(save_message.call_count, 2)

    def test_non_public_provider_failure_returns_readable_fallback(self):
        req = DirectChatRequest(user_id="owner", contact_id="other", message="嗨")
        history_cursor = MagicMock()
        history_cursor.sort.return_value.limit.return_value = []
        with patch("routers.public_chat.generate_room_id", return_value="pair-room"), \
             patch("routers.public_chat._validated_requested_mentions", return_value=([], False)), \
             patch("routers.public_chat.save_message", return_value={"message_id": "message-1"}), \
             patch("routers.public_chat.queue_profile_skills"), \
             patch("routers.public_chat.profiles_coll.update_one"), \
             patch("routers.public_chat.profiles_coll.find_one", return_value={}), \
             patch("routers.public_chat.messages_coll.find", return_value=history_cursor), \
             patch("routers.public_chat.matches_coll.find_one", return_value=None), \
             patch("routers.public_chat.find_accepted_match", return_value=None), \
             patch("routers.public_chat.generate_chat_completion", side_effect=RuntimeError("provider down")), \
             patch("routers.public_chat.mark_post_chat_activity", return_value=0):
            response = direct_chat(req, BackgroundTasks())

        self.assertEqual(response["reply"], "我剛剛有點分心，等我一下，我再好好回你。")
        self.assertNotIn("銝", response["reply"])

    def test_public_v2_profile_off_does_not_fall_back_to_legacy_observer(self):
        tasks = MagicMock()
        with patch("routers.public_chat.profile_skills_mode_for_user", return_value="off"):
            mode = queue_profile_skills(
                tasks, "owner", "我想去爬山", "message-1", "global",
                allow_legacy_fallback=False,
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
             patch("routers.public_chat.run_public_agent_turn", return_value=AgentResult(
                 handled=True, reply="嗨。", agent_run_id="run-save-once", agent_mode="v2",
                 profile_write_allowed=False,
             )):
            response = _run_public_v2_stream_turn(req, BackgroundTasks(), lambda _event: None)
        self.assertEqual(
            save_message.call_args_list,
            [
                call("room", "owner", "@對方 你好", metadata={
                    "owner_raw_content": "你好", "mention_labels": ["對方"],
                }),
                call("room", "ai_assistant", "嗨。"),
            ],
        )
        queue_profile.assert_called_once_with(
            ANY, "owner", "你好", "owner-message", "global", allow_legacy_fallback=False,
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
             patch("routers.public_chat.run_public_agent_turn", return_value=AgentResult(
                 handled=True, reply="遇到臨時變動時呢？", agent_run_id="assessment-run", agent_mode="v2",
                 profile_write_allowed=False, profile_write_reason="assessment",
                 assessment_state="active", assessment_kind="big_five", assessment_revision=2,
             )):
            response = _run_public_v2_stream_turn(req, BackgroundTasks(), lambda _event: None)
        queue_profile.assert_not_called()
        self.assertFalse(response["profile_update_pending"])
        self.assertEqual(response["assessment_state"], "active")
        self.assertEqual(response["assessment_kind"], "big_five")
        self.assertEqual(response["assessment_revision"], 2)

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

    def test_giphy_message_is_rendered_from_typed_safe_media(self):
        source = (
            Path(__file__).resolve().parents[1] / "frontend.html"
        ).read_text(encoding="utf-8")
        self.assertIn('messageData.message_type === "gif"', source)
        self.assertIn('parsed.protocol === "https:"', source)
        self.assertIn('host === "giphy.com" || host.endsWith(".giphy.com")', source)
        self.assertIn('provider.textContent = "via GIPHY"', source)
        self.assertIn('proactData.type === "match_connected_gif"', source)
        self.assertIn('copy.textContent = text || "這張有夠好笑，丟給你 😂"', source)
        self.assertIn('image.loading = "lazy"', source)
        self.assertIn('appendGiphyMessage(container, text, messageData)', source)

    def test_public_chat_uses_inline_mentions_and_typed_place_cards(self):
        source = (
            Path(__file__).resolve().parents[1] / "frontend.html"
        ).read_text(encoding="utf-8")
        self.assertIn('contenteditable="false"', source)
        self.assertIn('className = "inline-mention"', source)
        self.assertIn('.inline-mention { display: inline; background: transparent; color: #8b5cf6;', source)
        self.assertIn("mentions_inline: isPublicAyue", source)
        self.assertIn('event.key !== "Enter" || event.shiftKey', source)
        self.assertIn('mention_labels: outgoingMentions.map(mentionLabel)', source)
        self.assertIn("appendMessageText(div, text", source)
        self.assertIn('gmp-place-details-compact', source)
        self.assertIn('className = "place-card place-card-custom"', source)
        self.assertIn('url.pathname !== "/export/embed.html"', source)
        self.assertIn('frame.loading = "lazy"', source)
        self.assertIn('appendPlaceCards(div', source)

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

        def fake_turn(_req, _tasks, emit):
            emit({"type": "run_started", "agent_run_id": "run-1"})
            emit({
                "type": "tool_started", "agent_run_id": "run-1", "step_id": "0:read",
                "text": "我確認一下現在的時間…", "arguments": {"user_id": "seed_user_08"},
            })
            emit({
                "type": "tool_finished", "agent_run_id": "run-1", "step_id": "0:read",
                "outcome": "ok", "result": {"revision": 99},
            })
            return {"reply": "今天是 2026-07-30。", "agent_version": "v2", "agent_run_id": "run-1"}

        with patch("routers.public_chat.agent_mode_for_user", return_value="on"), \
             patch("routers.public_chat._run_public_v2_stream_turn", side_effect=fake_turn):
            response = direct_chat_stream(req, BackgroundTasks())
            chunks = asyncio.run(_collect(response))
        events = [json.loads(chunk) for chunk in chunks]
        self.assertEqual([event["type"] for event in events], ["run_started", "tool_started", "tool_finished", "final"])
        self.assertEqual(events[-1]["response"]["reply"], "今天是 2026-07-30。")
        public_text = json.dumps(events, ensure_ascii=False)
        for forbidden in ("arguments", "revision", "seed_user_", "prompt", "result"):
            self.assertNotIn(forbidden, public_text)

    def test_stream_hides_raw_exception_details(self):
        req = DirectChatRequest(user_id="owner", contact_id="ai_assistant", message="幫我查一下")
        with patch("routers.public_chat.agent_mode_for_user", return_value="on"), \
             patch("routers.public_chat._run_public_v2_stream_turn", side_effect=RuntimeError("seed_user_08 raw database error")):
            response = direct_chat_stream(req, BackgroundTasks())
            chunks = asyncio.run(_collect(response))
        event = json.loads(chunks[0])
        self.assertEqual(event["type"], "error")
        self.assertNotIn("seed_user_08", event["reply"])
        self.assertNotIn("database", event["reply"])

    def test_run_owned_background_tasks_finish_after_stream_response(self):
        req = DirectChatRequest(user_id="owner", contact_id="ai_assistant", message="記得我想旅行")
        background_finished = threading.Event()

        def fake_turn(_req, tasks, emit):
            tasks.add_task(background_finished.set)
            emit({"type": "run_started", "agent_run_id": "run-background"})
            return {"reply": "我記得。", "agent_version": "v2", "agent_run_id": "run-background"}

        with patch("routers.public_chat.agent_mode_for_user", return_value="on"), \
             patch("routers.public_chat._run_public_v2_stream_turn", side_effect=fake_turn):
            response = direct_chat_stream(req, BackgroundTasks())
            chunks = asyncio.run(_collect(response))
        self.assertEqual(json.loads(chunks[-1])["type"], "final")
        self.assertTrue(background_finished.wait(1), "run-owned background work did not finish")

    def test_unknown_progress_event_is_not_published(self):
        req = DirectChatRequest(user_id="owner", contact_id="ai_assistant", message="聊天")

        def fake_turn(_req, _tasks, emit):
            emit({"type": "debug", "prompt": "seed_user_08", "result": {"private": True}})
            return {"reply": "好呀。", "agent_version": "v2", "agent_run_id": "run-safe"}

        with patch("routers.public_chat.agent_mode_for_user", return_value="on"), \
             patch("routers.public_chat._run_public_v2_stream_turn", side_effect=fake_turn):
            response = direct_chat_stream(req, BackgroundTasks())
            chunks = asyncio.run(_collect(response))
        events = [json.loads(chunk) for chunk in chunks]
        self.assertEqual([event["type"] for event in events], ["final"])
        self.assertNotIn("seed_user_08", json.dumps(events, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
