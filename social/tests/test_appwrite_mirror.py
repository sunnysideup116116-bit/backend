import unittest
from unittest.mock import patch

from services import appwrite_mirror


class AppwriteDocumentIdentityTests(unittest.TestCase):
    def test_retries_reuse_the_same_document_id(self):
        message = {
            "message_id": "pair-owner:canonical-message-id",
            "room_id": "other_owner",
            "sender_id": "owner",
            "content": "嗨",
            "timestamp": 1_700_000_000,
        }

        with patch.object(appwrite_mirror, "_ENABLED", True), \
             patch.object(appwrite_mirror.requests, "post") as post:
            appwrite_mirror.mirror_message_to_appwrite(message)
            appwrite_mirror.mirror_message_to_appwrite(message)

        self.assertEqual(post.call_count, 2)
        first_id = post.call_args_list[0].kwargs["json"]["documentId"]
        second_id = post.call_args_list[1].kwargs["json"]["documentId"]
        self.assertEqual(first_id, second_id)
        self.assertRegex(first_id, r"^[0-9a-f]{32}$")

    def test_message_without_canonical_id_is_not_mirrored(self):
        message = {
            "room_id": "other_owner",
            "sender_id": "owner",
            "content": "嗨",
        }

        with patch.object(appwrite_mirror, "_ENABLED", True), \
             patch.object(appwrite_mirror.requests, "post") as post:
            appwrite_mirror.mirror_message_to_appwrite(message)

        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
