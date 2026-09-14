"""Image-message contract tests for the pair direct-chat adapter.

Image messages carry no analyzable text: the risk gate is skipped, the
message is persisted with message_type="image" and metadata.file_id, and
text-driven opening assist is never triggered.
"""

import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from routers.public_chat import direct_chat
from models import DirectChatRequest


class PairImageMessageTests(unittest.TestCase):
    def setUp(self):
        super().setUp()
        blocker = patch(
            "routers.public_chat.risk_block_service.is_pair_blocked",
            return_value=False,
        )
        blocker.start()
        self.addCleanup(blocker.stop)

    def _request(self, **overrides):
        payload = dict(
            user_id="owner",
            contact_id="other",
            message="",
            client_message_id="img-attempt-1",
            file_id="file-123",
        )
        payload.update(overrides)
        return DirectChatRequest(**payload)

    @patch("routers.public_chat.pair_message_risk_gate.evaluate")
    def test_image_message_skips_risk_gate(self, risk_gate):
        match = {"_id": "match-1", "status": "accepted"}
        tasks = MagicMock()
        with patch("routers.public_chat.find_accepted_match", return_value=match), \
             patch("routers.public_chat.save_pair_owner_message_once",
                   return_value={"message_id": "pair-owner:x", "created": True}) as save, \
             patch("routers.public_chat.profiles_coll.update_one"):
            response = direct_chat(self._request(), tasks)

        risk_gate.assert_not_called()
        self.assertEqual(response["image_sent"], True)
        save.assert_called_once()
        call_kwargs = save.call_args.kwargs
        self.assertEqual(call_kwargs["message_type"], "image")
        self.assertEqual(call_kwargs["file_id"], "file-123")
        self.assertEqual(call_kwargs["risk_projection"]["level"], "safe")
        self.assertEqual(call_kwargs["risk_projection"]["delivery"], "delivered")
        tasks.add_task.assert_not_called()

    def test_image_message_requires_accepted_match(self):
        with patch("routers.public_chat.find_accepted_match", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                direct_chat(self._request(), MagicMock())

        self.assertEqual(raised.exception.status_code, 403)

    @patch("routers.public_chat.pair_message_risk_gate.evaluate")
    def test_duplicate_image_message_returns_duplicate_without_second_save(self, risk_gate):
        match = {"_id": "match-1", "status": "accepted"}
        with patch("routers.public_chat.find_accepted_match", return_value=match), \
             patch("routers.public_chat.save_pair_owner_message_once",
                   return_value={"message_id": "pair-owner:x", "created": False}) as save, \
             patch("routers.public_chat.profiles_coll.update_one"):
            response = direct_chat(self._request(), MagicMock())

        self.assertEqual(response["duplicate"], True)
        save.assert_called_once()
        risk_gate.assert_not_called()

    @patch("routers.public_chat.pair_message_risk_gate.evaluate")
    def test_image_message_never_triggers_opening_assist_llm(self, risk_gate):
        match = {"_id": "match-1", "status": "accepted"}
        with patch("routers.public_chat.find_accepted_match", return_value=match), \
             patch("routers.public_chat.save_pair_owner_message_once",
                   return_value={"message_id": "pair-owner:x", "created": True}), \
             patch("routers.public_chat.profiles_coll.update_one"), \
             patch("routers.public_chat.messages_coll.count_documents") as count, \
             patch("routers.public_chat.generate_chat_completion") as generate:
            response = direct_chat(self._request(), MagicMock())

        count.assert_not_called()
        generate.assert_not_called()
        self.assertEqual(response["image_sent"], True)
        self.assertNotIn("opening_assist", response)

    def test_text_message_still_runs_risk_gate(self):
        req = DirectChatRequest(
            user_id="owner", contact_id="other", message="嗨",
            client_message_id="text-attempt-1",
        )
        match = {"_id": "match-1", "status": "accepted"}
        with patch("routers.public_chat.find_accepted_match", return_value=match), \
             patch("routers.public_chat.save_pair_owner_message_once",
                   return_value={"message_id": "pair-owner:x", "created": True}) as save, \
             patch("routers.public_chat.profiles_coll.update_one"), \
             patch("routers.public_chat.messages_coll.count_documents", return_value=2), \
             patch("routers.public_chat.mark_post_chat_activity", return_value=2), \
             patch("routers.public_chat.track_message_metrics"), \
             patch("routers.public_chat.pair_message_risk_gate.evaluate",
                   return_value=MagicMock(
                       may_persist=True,
                       public_projection=lambda: {
                           "level": "safe",
                           "delivery": "delivered",
                           "ui_priority": "coach",
                       },
                   )) as risk_gate:
            direct_chat(req, MagicMock())

        risk_gate.assert_called_once()
        save.assert_called_once()
        self.assertEqual(save.call_args.args[0], "other_owner")
        self.assertEqual(save.call_args.args[2], "嗨")
        self.assertIsNone(save.call_args.kwargs.get("file_id"))


if __name__ == "__main__":
    unittest.main()
