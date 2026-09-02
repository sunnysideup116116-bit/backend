import unittest
from unittest.mock import MagicMock, patch

from services import push_service
from services.notification_service import NotificationEvent


def _event() -> NotificationEvent:
    return NotificationEvent(
        recipient_id="other",
        surface="pair",
        conversation_id="owner_other",
        other_user_id="owner",
        title="小美",
        body="晚上一起吃飯？",
        data={
            "chat_surface": "pair",
            "chat_contact_id": "owner",
            "chat_message_kind": "text",
        },
        tag="chat-abc",
        message_id="message-1",
        timestamp=1.0,
    )


class PushDispatchTests(unittest.TestCase):
    def setUp(self):
        self._enabled = push_service._ENABLED
        push_service._ENABLED = True

    def tearDown(self):
        push_service._ENABLED = self._enabled

    @staticmethod
    def _target_response(targets):
        response = MagicMock(status_code=200)
        response.json.return_value = {"targets": targets}
        return response

    def test_disabled_still_prepares_unread_but_never_calls_appwrite(self):
        push_service._ENABLED = False
        with patch.object(
            push_service, "prepare_message_notifications", return_value=[_event()],
        ) as prepare, patch.object(push_service, "requests") as requests:
            push_service.send_push_notification({"message_id": "message-1"})
        prepare.assert_called_once()
        requests.get.assert_not_called()
        requests.post.assert_not_called()

    def test_no_prepared_event_sends_nothing(self):
        with patch.object(
            push_service, "prepare_message_notifications", return_value=[],
        ), patch.object(push_service, "requests") as requests:
            push_service.send_push_notification({})
        requests.get.assert_not_called()
        requests.post.assert_not_called()

    def test_missing_push_target_is_preflighted_and_skipped(self):
        with patch.object(
            push_service, "prepare_message_notifications", return_value=[_event()],
        ), patch.object(push_service, "requests") as requests:
            requests.get.return_value = self._target_response([])
            push_service.send_push_notification({})
        requests.get.assert_called_once()
        requests.post.assert_not_called()

    def test_dispatches_to_explicit_nonexpired_fcm_target(self):
        target = {
            "$id": "target-1",
            "providerType": "push",
            "providerId": push_service._FCM_PROVIDER_ID,
            "expired": False,
        }
        with patch.object(
            push_service, "prepare_message_notifications", return_value=[_event()],
        ), patch.object(push_service, "requests") as requests:
            requests.get.return_value = self._target_response([target])
            requests.post.return_value = MagicMock(status_code=201, text="")
            push_service.send_push_notification({})
        requests.post.assert_called_once()
        payload = requests.post.call_args.kwargs["json"]
        self.assertEqual(payload["targets"], ["target-1"])
        self.assertNotIn("users", payload)
        self.assertEqual(payload["title"], "小美")
        self.assertEqual(payload["body"], "晚上一起吃飯？")
        self.assertEqual(payload["tag"], "chat-abc")
        self.assertEqual(payload["priority"], "high")
        self.assertEqual(payload["data"]["chat_message_kind"], "text")
        self.assertNotIn("message_type", payload["data"])
        self.assertEqual(len(payload["messageId"]), 32)

    def test_filters_email_expired_and_wrong_provider_targets(self):
        targets = [
            {"$id": "email", "providerType": "email", "expired": False},
            {
                "$id": "expired", "providerType": "push",
                "providerId": push_service._FCM_PROVIDER_ID, "expired": True,
            },
            {
                "$id": "wrong", "providerType": "push",
                "providerId": "other-provider", "expired": False,
            },
        ]
        with patch.object(push_service, "requests") as requests:
            requests.get.return_value = self._target_response(targets)
            self.assertEqual(push_service._valid_push_target_ids("other"), [])

    def test_queue_records_before_spawning_daemon_dispatch(self):
        events = [_event()]
        with patch.object(
            push_service, "prepare_message_notifications", return_value=events,
        ) as prepare, patch.object(push_service, "threading") as threading:
            push_service.queue_push_notification({"message_id": "message-1"})
        prepare.assert_called_once()
        threading.Thread.assert_called_once()
        kwargs = threading.Thread.call_args.kwargs
        self.assertTrue(kwargs["daemon"])
        self.assertEqual(kwargs["name"], "push-dispatch")
        self.assertIs(kwargs["target"], push_service._dispatch_prepared)
        self.assertEqual(kwargs["args"], (events,))


if __name__ == "__main__":
    unittest.main()
