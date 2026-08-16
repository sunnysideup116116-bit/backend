import unittest
from unittest.mock import MagicMock, patch

from services import chat_service


class SystemMessageTests(unittest.TestCase):
    def test_system_message_uses_a_deterministic_mongo_id_for_idempotency(self):
        result = MagicMock(upserted_id="created")
        with patch.object(chat_service.messages_coll, "update_one", return_value=result) as update:
            first = chat_service.save_system_message_once(
                "owner_other", "歡迎你們", "text", {"event_type": "match_celebration"},
                event_key="match:match-1:celebration:welcome",
            )
            second = chat_service.save_system_message_once(
                "owner_other", "歡迎你們", "text", {"event_type": "match_celebration"},
                event_key="match:match-1:celebration:welcome",
            )

        self.assertEqual(first["message_id"], second["message_id"])
        self.assertTrue(first["created"])
        self.assertEqual(update.call_count, 2)
        self.assertEqual(update.call_args_list[0].args[0], update.call_args_list[1].args[0])
        self.assertIn("$setOnInsert", update.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
