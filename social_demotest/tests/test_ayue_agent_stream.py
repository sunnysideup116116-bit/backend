import asyncio
import builtins
import json
import sys
import threading
import unittest
from unittest.mock import ANY, MagicMock, call, patch

from fastapi import BackgroundTasks

from models import DirectChatRequest

# Offline suite injects a minimal config module before discovery.
if "config" in sys.modules and not hasattr(sys.modules["config"], "OLLAMA_FAST_CHAT_MODEL"):
    setattr(sys.modules["config"], "OLLAMA_FAST_CHAT_MODEL", "test")

from routers.chat import _run_public_v2_stream_turn, direct_chat, direct_chat_stream, queue_profile_skills
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

        with patch("routers.chat.generate_room_id", return_value="room"), \
             patch("routers.chat.save_message", return_value={"message_id": "owner-message"}), \
             patch("routers.chat.messages_coll.find_one", return_value=None), \
             patch("routers.chat.messages_coll.find", return_value=history_cursor), \
             patch("routers.chat.profiles_coll.update_one"), \
             patch("routers.chat.profiles_coll.find_one", return_value={"user_id": "owner"}), \
             patch("routers.chat.agent_mode_for_user", return_value="on"), \
             patch("routers.chat._complete_public_v2_turn", return_value={
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
        with patch("routers.chat.generate_room_id", return_value="room"), \
             patch("routers.chat.save_message", side_effect=[
                 {"message_id": "owner-message"}, {"message_id": "assistant-message"},
             ]) as save_message, \
             patch("routers.chat.messages_coll.find_one", return_value={"content": "對方這次先婉拒了邀請。"}), \
             patch("routers.chat.messages_coll.find", return_value=history_cursor), \
             patch("routers.chat.profiles_coll.update_one"), \
             patch("routers.chat.profiles_coll.find_one", return_value={"user_id": "owner"}), \
             patch("routers.chat.agent_mode_for_user", return_value="off"), \
             patch("routers.chat.profile_skills_mode_for_user", return_value="off"), \
             patch("routers.chat.match_outcome_followup_reply", return_value="對方這次沒有接受。"), \
             patch("routers.chat._complete_public_v2_turn") as complete_v2:
            response = direct_chat(req, BackgroundTasks())

        self.assertEqual(response["conversation_intent"], "match_outcome_followup")
        self.assertEqual(response["reply"], "對方這次沒有接受。")
        complete_v2.assert_not_called()
        self.assertEqual(save_message.call_count, 2)

    def test_public_v2_profile_off_does_not_fall_back_to_legacy_observer(self):
        tasks = MagicMock()
        with patch("routers.chat.profile_skills_mode_for_user", return_value="off"):
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
        with patch("routers.chat.generate_room_id", return_value="room"), \
             patch("routers.chat._validated_requested_mentions", return_value=(["seed_user_01"], False)), \
             patch("routers.chat._mention_display_prefix", return_value="@對方"), \
             patch("routers.chat.save_message", side_effect=[
                 {"message_id": "owner-message"}, {"message_id": "assistant-message"},
             ]) as save_message, \
             patch("routers.chat.queue_profile_skills") as queue_profile, \
             patch("routers.chat.profiles_coll.update_one"), \
             patch("routers.chat.profiles_coll.find_one", return_value={"user_id": "owner"}), \
             patch("routers.chat.messages_coll.find", return_value=history_cursor), \
             patch("routers.chat.run_public_agent_turn", return_value=AgentResult(
                 handled=True, reply="嗨。", agent_run_id="run-save-once", agent_mode="v2",
                 profile_write_allowed=False,
             )):
            response = _run_public_v2_stream_turn(req, BackgroundTasks(), lambda _event: None)
        self.assertEqual(
            save_message.call_args_list,
            [
                call("room", "owner", "@對方 你好", metadata={"owner_raw_content": "你好"}),
                call("room", "ai_assistant", "嗨。"),
            ],
        )
        queue_profile.assert_called_once_with(
            ANY, "owner", "你好", "owner-message", "global", allow_legacy_fallback=False,
        )
        self.assertEqual(response["reply"], "嗨。")

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

        with patch("routers.chat.agent_mode_for_user", return_value="on"), \
             patch("routers.chat._run_public_v2_stream_turn", side_effect=fake_turn):
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
        with patch("routers.chat.agent_mode_for_user", return_value="on"), \
             patch("routers.chat._run_public_v2_stream_turn", side_effect=RuntimeError("seed_user_08 raw database error")):
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

        with patch("routers.chat.agent_mode_for_user", return_value="on"), \
             patch("routers.chat._run_public_v2_stream_turn", side_effect=fake_turn):
            response = direct_chat_stream(req, BackgroundTasks())
            chunks = asyncio.run(_collect(response))
        self.assertEqual(json.loads(chunks[-1])["type"], "final")
        self.assertTrue(background_finished.wait(1), "run-owned background work did not finish")

    def test_unknown_progress_event_is_not_published(self):
        req = DirectChatRequest(user_id="owner", contact_id="ai_assistant", message="聊天")

        def fake_turn(_req, _tasks, emit):
            emit({"type": "debug", "prompt": "seed_user_08", "result": {"private": True}})
            return {"reply": "好呀。", "agent_version": "v2", "agent_run_id": "run-safe"}

        with patch("routers.chat.agent_mode_for_user", return_value="on"), \
             patch("routers.chat._run_public_v2_stream_turn", side_effect=fake_turn):
            response = direct_chat_stream(req, BackgroundTasks())
            chunks = asyncio.run(_collect(response))
        events = [json.loads(chunk) for chunk in chunks]
        self.assertEqual([event["type"] for event in events], ["final"])
        self.assertNotIn("seed_user_08", json.dumps(events, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
