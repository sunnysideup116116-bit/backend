import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks

from models import DirectChatRequest, MediatorPrivateRequest
from routers import private_mediator, public_chat
from services.ayue_agent.contracts import AgentResult


class ChatChoiceRouteTests(unittest.TestCase):
    def test_public_stream_choice_does_not_save_an_owner_message(self):
        req = DirectChatRequest(
            user_id="owner", contact_id="ai_assistant", message="",
            choice_id="choice-1", choice_action="confirm",
        )
        expected = {"reply": "已完成"}
        with patch.object(public_chat, "_resolve_ai_room_id", return_value="room"), \
             patch.object(public_chat, "save_message") as save_message, \
             patch.object(public_chat, "_complete_public_turn", return_value=expected) as complete, \
             patch.object(public_chat.profiles_coll, "update_one"):
            result = public_chat._run_public_stream_turn(
                req, BackgroundTasks(), lambda _event: None,
            )

        self.assertEqual(result, expected)
        save_message.assert_not_called()
        self.assertIsNone(complete.call_args.kwargs["user_message_id"])

    def test_public_json_choice_does_not_save_an_owner_message(self):
        req = DirectChatRequest(
            user_id="owner", contact_id="ai_assistant", message="",
            choice_id="choice-1", choice_action="cancel",
        )
        with patch.object(public_chat, "_resolve_ai_room_id", return_value="room"), \
             patch.object(public_chat, "save_message") as save_message, \
             patch.object(public_chat, "record_proactive_activity"), \
             patch.object(public_chat, "_complete_public_turn", return_value={"reply": "已取消"}):
            result = public_chat.direct_chat(req, BackgroundTasks())

        self.assertEqual(result["reply"], "已取消")
        save_message.assert_not_called()

    def test_private_saved_reply_persists_choice_metadata_and_marks_presented(self):
        req = MediatorPrivateRequest(
            user_id="owner", other_id="other", message="想邀請",
        )
        result = AgentResult(
            handled=True, reply="要發起嗎？", agent_run_id="a" * 32,
            choice_prompt={
                "id": "choice-2", "state": "pending",
                "selected": None, "expires_at": 1e18,
            },
        )
        saved = {"message_id": "message-2", "content": "要發起嗎？"}
        with patch.object(private_mediator, "run_private_agent_turn_v2", return_value=result), \
             patch.object(private_mediator, "save_private_mediator_reply", return_value=saved) as save_reply, \
             patch.object(private_mediator, "mark_private_confirmation_presented", return_value=True) as mark:
            response = private_mediator._run_private_v2_saved_turn(
                req, {"status": "accepted"}, "private-room",
            )

        self.assertEqual(response["choice_prompt"]["id"], "choice-2")
        self.assertEqual(save_reply.call_args.kwargs["choice_prompt"]["id"], "choice-2")
        mark.assert_called_once()

    def test_private_json_choice_skips_owner_persistence(self):
        req = MediatorPrivateRequest(
            user_id="owner", other_id="other", message="",
            choice_id="choice-2", choice_action="cancel",
        )
        with patch.object(private_mediator, "find_accepted_match", return_value={"_id": "match"}), \
             patch.object(private_mediator, "save_message") as save_message, \
             patch.object(private_mediator.profiles_coll, "update_one"), \
             patch.object(private_mediator.profiles_coll, "find_one", return_value={}), \
             patch.object(private_mediator, "_run_private_v2_saved_turn", return_value={"reply": "已取消"}):
            response = private_mediator.mediator_private_chat(req, BackgroundTasks())

        self.assertEqual(response["reply"], "已取消")
        save_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
