import json
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from models import CalendarActionRequest, ChatRequest, RelationshipGameRequest
from routers.chat_messages import get_messages
from routers.chat_onboarding import chat_endpoint
from routers.demo import reset_db_state
from routers.proactive import proactive_check
from routers.system import get_notifications
from routers.match import get_match_status
from routers.relationship_dates import cancel_date_coordination
from routers.relationship_quiz import cancel_relationship_quiz


class _Cursor(list):
    def sort(self, *_args):
        return self


class ChatLeafRouterTests(unittest.TestCase):
    def test_big_five_onboarding_delegates_to_the_shared_session_service(self):
        outcome = {"status": "committed", "reply": "新的結果已套用。", "kind": "big_five"}
        profile = {"big_five": {"O": 7, "summary": "喜歡探索"}, "agentic_assessment_session": {
            "session_id": "s", "kind": "big_five", "status": "completed", "revision": 3,
        }}
        with patch("routers.chat_onboarding.handle_assessment_ui_message", return_value=outcome) as handle, \
             patch("routers.chat_onboarding.profiles_coll.find_one", return_value=profile):
            response = chat_endpoint(ChatRequest(user_id="owner", message="確認", state="big_five"))

        self.assertTrue(response["is_complete"])
        self.assertEqual(response["big_five"]["O"], 7)
        self.assertEqual(response["assessment_state"], "completed")
        handle.assert_called_once_with("owner", "big_five", "確認", initial_interest=None, initialize=False)

    def test_first_interest_is_persisted_and_reused_after_the_browser_stops_sending_it(self):
        with patch("routers.chat_onboarding.handle_assessment_ui_message", return_value={"status": "continued", "reply": "你好。", "draft": {}}) as handle, \
             patch("routers.chat_onboarding.profiles_coll.find_one", return_value={}):
            chat_endpoint(ChatRequest(user_id="owner", message="你好", state="big_five", initial_interest="喜欢旅行"))
        handle.assert_called_once_with("owner", "big_five", "你好", initial_interest="喜欢旅行", initialize=False)

        with patch("routers.chat_onboarding.handle_assessment_ui_message", return_value={"status": "continued", "reply": "繼續。", "draft": {}}) as handle, \
             patch("routers.chat_onboarding.profiles_coll.find_one", return_value={}):
            chat_endpoint(ChatRequest(user_id="owner", message="繼續", state="big_five"))
        handle.assert_called_once_with("owner", "big_five", "繼續", initial_interest=None, initialize=False)

    def test_default_interest_never_overwrites_a_saved_interest(self):
        with patch("routers.chat_onboarding.handle_assessment_ui_message", return_value={"status": "continued", "reply": "繼續聊聊。", "draft": {}}) as handle, \
             patch("routers.chat_onboarding.profiles_coll.find_one", return_value={"initial_interest": "看電影"}):
            chat_endpoint(ChatRequest(user_id="owner", message="重新開始", state="big_five", initial_interest="無特別興趣"))
        handle.assert_called_once_with("owner", "big_five", "重新開始", initial_interest="無特別興趣", initialize=False)

    def test_assessment_ui_has_an_explicit_exit_that_uses_the_shared_reset_endpoint(self):
        source = (Path(__file__).resolve().parents[1] / "frontend.html").read_text(encoding="utf-8")
        self.assertIn("exitAssessmentFromUi('big_five')", source)
        self.assertIn("exitAssessmentFromUi('deep_profile')", source)
        self.assertIn("async function exitAssessmentFromUi(state)", source)
        self.assertIn("/api/chat/reset", source)
        self.assertIn("initialize: true", source)
        self.assertNotIn("initDeepProfile();", source)

    def test_ai_history_still_creates_only_the_initial_greeting(self):
        messages = _Cursor([{"sender_id": "ai_assistant", "content": "哈囉"}])
        with patch("routers.chat_messages.generate_room_id", return_value="room"), \
             patch("routers.chat_messages.messages_coll.count_documents", return_value=0), \
             patch("routers.chat_messages.save_message") as save, \
             patch("routers.chat_messages.messages_coll.find", return_value=messages), \
             patch("routers.chat_messages.profiles_coll.find_one", return_value={
                 "active_match_proposal_id": "proposal-visible-id",
             }):
            response = get_messages("ai_assistant", "owner")

        self.assertEqual(response, {
            "messages": list(messages),
            "active_match_proposal_id": "proposal-visible-id",
            "date_coordination": None,
            "established_dates": [],
        })
        save.assert_called_once_with(
            "room", "ai_assistant", "哈囉，我是阿月。最近想做什麼、想去哪裡，儘管跟我說；我會邊聊邊幫你留意合適的人。",
        )

    def test_date_cancel_remains_a_thin_domain_service_adapter(self):
        expected = {"status": "cancelled"}
        with patch("routers.relationship_dates.cancel_coordination_or_event", return_value=expected) as cancel:
            response = cancel_date_coordination(
                CalendarActionRequest(user_id="owner"), "other", "coordination-1",
            )

        self.assertEqual(response, {"coordination": expected})
        cancel.assert_called_once_with("owner", "other", "coordination-1")

    def test_demo_reset_keeps_its_existing_scoped_cleanup(self):
        with patch("routers.demo.messages_coll.delete_many") as delete_messages, \
             patch("routers.demo.calendar_events_coll.delete_many") as delete_calendar_events, \
             patch("routers.demo.matches_coll.update_many") as update_matches, \
             patch("routers.demo.profiles_coll.update_many") as update_profiles, \
             patch("builtins.print"):
            response = reset_db_state()

        self.assertEqual(response, {"status": "success", "message": "DB state reset"})
        delete_messages.assert_called_once_with({"room_id": {"$regex": "mediator_private"}})
        delete_calendar_events.assert_called_once_with({})
        update_matches.assert_called_once_with({}, {"$unset": {"date_coordination": ""}})
        update_profiles.assert_called_once_with({}, {"$set": {"mediator_inbox": []}})

    def test_proactive_router_delegates_to_the_delivery_service(self):
        expected = {"has_new": False}
        with patch("routers.proactive.run_proactive_check", return_value=expected) as check:
            response = proactive_check("owner", conversation_active=True)

        self.assertEqual(response, expected)
        check.assert_called_once_with("owner", True)

    def test_quiz_cancel_keeps_its_specific_not_accepted_error(self):
        with patch("routers.relationship_quiz.find_accepted_match", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                cancel_relationship_quiz(RelationshipGameRequest(user_id="owner", other_id="other"))

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail, "只能在已接受配對中取消測驗")

    def test_pending_notification_keeps_identity_out_of_visible_profile_text(self):
        pending = [{
            "_id": "match-1", "from_user": "seed_user_09",
            "reason": "小樂跟你都喜歡電影", "receiver_reason": "小樂想認識你",
        }]
        with patch("routers.system.matches_coll.find", return_value=pending), \
             patch("routers.system.profiles_coll.find_one", return_value={
                 "display_name": "小樂", "current_context": "小樂最近想看電影",
                 "big_five": {"summary": "小樂很外向"}, "distinctive_tags": ["小樂愛電影"],
             }):
            response = get_notifications("owner")
        visible = response["notifications"][0]
        self.assertEqual(visible["from_user"], "seed_user_09")  # opaque action binding
        for field in ("reason", "receiver_reason", "from_user_context", "from_user_big_five", "from_user_distinctive_tags"):
            self.assertNotIn("小樂", str(visible[field]))

    def test_v4_notification_exposes_only_the_receiver_projection(self):
        invitation = "有位比較外向的人,最近提到「想去郊區讀書」。你們或許可以先聊聊;你想認識對方嗎?"
        pending = [{
            "_id": "match-v4", "from_user": "seed_user_09", "to_user": "owner",
            "status": "pending", "reason_version": "v4_friend_intro",
            "friend_intro_v4": {
                "initiator_preview": {
                    "viewer_id": "seed_user_09", "counterparty_id": "owner",
                    "viewer_text": "不應出現的另一方向",
                },
                "receiver_invitation": {
                    "viewer_id": "owner", "counterparty_id": "seed_user_09",
                    "counterparty_context_snapshot": "想去郊區讀書",
                    "counterparty_public_personality": "比較外向",
                    "viewer_text": invitation,
                },
            },
        }]
        with patch("routers.system.matches_coll.find", return_value=pending):
            visible = get_notifications("owner")["notifications"][0]
        self.assertEqual(visible["viewer_reason"], invitation)
        self.assertNotIn("from_user", visible)
        serialized = json.dumps(visible, ensure_ascii=False)
        self.assertNotIn("seed_user", serialized)
        self.assertNotIn("不應出現的另一方向", serialized)

    def test_public_match_status_strips_internal_revision_timestamps(self):
        snapshot = {
            "state": "waiting_other", "scope": "live_match", "is_terminal": False,
            "chat_opened": False, "counterparty": "對方", "reason_code": None,
            "revision": 7, "updated_at": 12345,
        }
        with patch("routers.match.reconcile_match_state", return_value=None), \
             patch("routers.match.public_match_search_status", return_value={"status": "idle"}), \
             patch("routers.match.get_match_status_snapshot", return_value=snapshot):
            response = get_match_status("owner")
        public_snapshot = response["status_snapshot"]
        self.assertNotIn("revision", public_snapshot)
        self.assertNotIn("updated_at", public_snapshot)


if __name__ == "__main__":
    unittest.main()
