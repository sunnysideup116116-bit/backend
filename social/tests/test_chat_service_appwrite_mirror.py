import unittest
from unittest.mock import MagicMock, patch

from services import chat_service


class AppwriteMirrorTests(unittest.TestCase):
    def test_save_message_mirrors_after_mongo_insert(self):
        result = MagicMock(inserted_id="mongo-id")
        with patch.object(chat_service.messages_coll, "insert_one", return_value=result) as insert, \
             patch.object(chat_service, "mirror_message_to_appwrite_async") as mirror:
            msg = chat_service.save_message("owner_other", "owner", "嗨")

        insert.assert_called_once()
        self.assertEqual(msg["message_id"], "mongo-id")
        mirror.assert_called_once_with(msg)

    def test_pair_owner_message_mirrors_only_when_created(self):
        created = MagicMock(upserted_id="created")
        duplicate = MagicMock(upserted_id=None)
        with patch.object(chat_service.messages_coll, "update_one", side_effect=[created, duplicate]) as update, \
             patch.object(chat_service, "mirror_message_to_appwrite_async") as mirror:
            first = chat_service.save_pair_owner_message_once(
                "owner_other", "owner", "嗨",
                client_message_id="attempt-1",
                risk_projection={"level": "safe", "delivery": "delivered"},
            )
            second = chat_service.save_pair_owner_message_once(
                "owner_other", "owner", "嗨",
                client_message_id="attempt-1",
                risk_projection={"level": "safe", "delivery": "delivered"},
            )

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(update.call_count, 2)
        mirror.assert_called_once_with(first)

    def test_system_message_mirrors_only_when_created(self):
        created = MagicMock(upserted_id="created")
        duplicate = MagicMock(upserted_id=None)
        with patch.object(chat_service.messages_coll, "update_one", side_effect=[created, duplicate]) as update, \
             patch.object(chat_service, "mirror_message_to_appwrite_async") as mirror:
            first = chat_service.save_system_message_once(
                "owner_other", "歡迎你們", "text", {"event_type": "match_celebration"},
                event_key="match:match-1:celebration:welcome",
            )
            second = chat_service.save_system_message_once(
                "owner_other", "歡迎你們", "text", {"event_type": "match_celebration"},
                event_key="match:match-1:celebration:welcome",
            )

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(update.call_count, 2)
        mirror.assert_called_once_with(first)


if __name__ == "__main__":
    unittest.main()
