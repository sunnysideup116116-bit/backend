import unittest
from unittest.mock import MagicMock, patch

from services import push_service


class PushDispatchTests(unittest.TestCase):
    def setUp(self):
        self._enabled = push_service._ENABLED
        push_service._ENABLED = True

    def tearDown(self):
        push_service._ENABLED = self._enabled

    def test_skips_when_disabled(self):
        push_service._ENABLED = False
        with patch.object(push_service, "requests") as requests:
            push_service.send_push_notification({
                "room_id": "owner_other",
                "sender_id": "owner",
                "content": "嗨",
            })
        requests.post.assert_not_called()

    def test_skips_ai_assistant_sender(self):
        with patch.object(push_service, "requests") as requests:
            push_service.send_push_notification({
                "room_id": "owner_ai_assistant",
                "sender_id": "ai_assistant",
                "content": "你好",
            })
        requests.post.assert_not_called()

    def test_skips_ai_assistant_receiver(self):
        with patch.object(push_service, "requests") as requests:
            push_service.send_push_notification({
                "room_id": "owner_ai_assistant",
                "sender_id": "owner",
                "content": "你好",
            })
        requests.post.assert_not_called()

    def test_skips_blocked_messages(self):
        with patch.object(push_service, "requests") as requests:
            push_service.send_push_notification({
                "room_id": "owner_other",
                "sender_id": "owner",
                "content": "壞話",
                "is_blocked": True,
            })
        requests.post.assert_not_called()

    def test_skips_active_receiver(self):
        with patch.object(push_service, "_receiver_active", return_value=True), \
             patch.object(push_service, "requests") as requests:
            push_service.send_push_notification({
                "room_id": "owner_other",
                "sender_id": "owner",
                "content": "嗨",
            })
        requests.post.assert_not_called()

    def test_dispatches_with_user_target_and_data(self):
        with patch.object(push_service, "_receiver_active", return_value=False), \
             patch.object(push_service, "_display_name", return_value="小美"), \
             patch.object(push_service, "requests") as requests:
            push_service.send_push_notification({
                "room_id": "owner_other",
                "sender_id": "owner",
                "content": "晚上一起吃飯？",
                "message_type": "text",
            })
        requests.post.assert_called_once()
        args = requests.post.call_args
        self.assertEqual(args.args[0], f"{push_service._ENDPOINT}/messaging/messages/push")
        payload = args.kwargs["json"]
        self.assertEqual(payload["title"], "小美")
        self.assertEqual(payload["body"], "晚上一起吃飯？")
        self.assertEqual(payload["users"], ["other"])
        self.assertEqual(payload["priority"], "high")
        self.assertEqual(payload["data"]["contact_id"], "owner")
        self.assertEqual(payload["data"]["msg_type"], "text")
        self.assertNotIn("message_type", payload["data"])

    def test_payload_uses_unique_message_id(self):
        with patch.object(push_service, "_receiver_active", return_value=False), \
             patch.object(push_service, "requests") as requests:
            push_service.send_push_notification({
                "room_id": "owner_other",
                "sender_id": "owner",
                "content": "嗨",
            })
        payload = requests.post.call_args.kwargs["json"]
        self.assertEqual(payload["messageId"], "unique()")

    def test_image_message_body(self):
        with patch.object(push_service, "_receiver_active", return_value=False), \
             patch.object(push_service, "requests") as requests:
            push_service.send_push_notification({
                "room_id": "owner_other",
                "sender_id": "owner",
                "content": "",
                "message_type": "image",
            })
        payload = requests.post.call_args.kwargs["json"]
        self.assertEqual(payload["body"], "傳了一張圖片")

    def test_queue_spawns_daemon_thread(self):
        with patch.object(push_service, "threading") as threading, \
             patch.object(push_service, "send_push_notification"):
            push_service.queue_push_notification({"room_id": "a_b", "sender_id": "a"})
        threading.Thread.assert_called_once()
        kwargs = threading.Thread.call_args.kwargs
        self.assertTrue(kwargs["daemon"])
        self.assertEqual(kwargs["name"], "push-dispatch")


if __name__ == "__main__":
    unittest.main()
