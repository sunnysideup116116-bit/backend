import unittest
from unittest.mock import MagicMock, patch

from services import notification_service as service


def _pair_message(sender="a", room="a_b"):
    return {
        "room_id": room,
        "sender_id": sender,
        "content": "嗨",
        "message_type": "text",
        "message_id": "m1",
        "timestamp": 10.0,
        "metadata": {},
    }


class NotificationClassificationTests(unittest.TestCase):
    def test_pair_message_targets_other_participant(self):
        with patch.object(service, "_display_name", return_value="小安"):
            events = service.classify_message_notifications(_pair_message())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].recipient_id, "b")
        self.assertEqual(events[0].surface, service.PAIR)
        self.assertEqual(events[0].other_user_id, "a")
        self.assertEqual(events[0].title, "小安")

    def test_public_ayue_legacy_and_multi_room_target_owner(self):
        legacy = service.classify_message_notifications({
            **_pair_message("ai_assistant", "ai_assistant_owner"),
            "content": "我有新消息",
        })
        multi = service.classify_message_notifications({
            **_pair_message("ai_assistant", "ai_room::owner::room1"),
            "content": "我有新消息",
        })
        self.assertEqual(legacy[0].recipient_id, "owner")
        self.assertEqual(legacy[0].surface, service.PUBLIC_AYUE)
        self.assertEqual(legacy[0].data["chat_ai_room_id"], "")
        self.assertEqual(multi[0].recipient_id, "owner")
        self.assertEqual(multi[0].data["chat_ai_room_id"], "ai_room::owner::room1")

    def test_owner_authored_public_ayue_message_never_notifies_self(self):
        self.assertEqual(
            service.classify_message_notifications(
                _pair_message("owner", "ai_assistant_owner"),
            ),
            [],
        )

    def test_private_ayue_targets_room_owner(self):
        events = service.classify_message_notifications({
            **_pair_message("ai_assistant", "mediator_private::owner::other"),
            "content": "悄悄告訴你",
        })
        self.assertEqual(events[0].recipient_id, "owner")
        self.assertEqual(events[0].surface, service.MEDIATOR_PRIVATE)
        self.assertEqual(events[0].other_user_id, "other")

    def test_private_owner_message_and_safety_notice_are_excluded(self):
        self.assertEqual(
            service.classify_message_notifications(
                _pair_message("owner", "mediator_private::owner::other"),
            ),
            [],
        )
        blocked = _pair_message("ai_assistant", "a_b")
        blocked["metadata"] = {"event_type": "blocked_notice"}
        self.assertEqual(service.classify_message_notifications(blocked), [])

    def test_date_invite_targets_invitee_and_quiz_targets_both(self):
        invite = _pair_message("ai_assistant", "a_b")
        invite["metadata"] = {
            "event_type": "date_coordination_invite",
            "initiator_id": "a",
            "invitee_id": "b",
        }
        quiz = _pair_message("ai_assistant", "a_b")
        quiz["metadata"] = {
            "event_type": "compatibility_quiz_result",
            "notification_recipients": ["a", "b"],
        }
        self.assertEqual(
            [event.recipient_id for event in service.classify_message_notifications(invite)],
            ["b"],
        )
        self.assertEqual(
            {event.recipient_id for event in service.classify_message_notifications(quiz)},
            {"a", "b"},
        )


class NotificationPresenceRulesTests(unittest.TestCase):
    def _prepare(self, msg, contexts, preferences=None):
        with patch.object(service, "_active_contexts", return_value=contexts), \
             patch.object(service, "_increment_unread") as increment, \
             patch.object(
                 service,
                 "get_notification_preferences",
                 return_value=preferences or {
                     "global_enabled": True,
                     "public_ayue_enabled": True,
                     "muted_peer_ids": [],
                     "muted_mediator_ids": [],
                 },
             ):
            dispatches = service.prepare_message_notifications(msg)
        return dispatches, increment

    def test_exact_pair_chat_suppresses_push_and_unread(self):
        dispatches, increment = self._prepare(
            _pair_message(),
            [{"surface": service.PAIR, "conversation_id": "a_b", "other_user_id": "a"}],
        )
        self.assertEqual(dispatches, [])
        increment.assert_not_called()

    def test_scenario_one_private_same_partner_suppresses_push_but_keeps_unread(self):
        dispatches, increment = self._prepare(
            _pair_message(),
            [{
                "surface": service.MEDIATOR_PRIVATE,
                "conversation_id": "mediator_private::b::a",
                "other_user_id": "a",
            }],
        )
        self.assertEqual(dispatches, [])
        increment.assert_called_once()

    def test_scenario_two_private_other_partner_still_pushes(self):
        dispatches, increment = self._prepare(
            _pair_message(),
            [{
                "surface": service.MEDIATOR_PRIVATE,
                "conversation_id": "mediator_private::b::c",
                "other_user_id": "c",
            }],
        )
        self.assertEqual(len(dispatches), 1)
        increment.assert_called_once()

    def test_scenario_three_pair_chat_does_not_suppress_private_ayue(self):
        private = {
            **_pair_message("ai_assistant", "mediator_private::b::a"),
            "content": "阿月的新訊息",
        }
        dispatches, increment = self._prepare(
            private,
            [{"surface": service.PAIR, "conversation_id": "a_b", "other_user_id": "a"}],
        )
        self.assertEqual(len(dispatches), 1)
        increment.assert_called_once()

    def test_scenario_four_public_ayue_does_not_suppress_pair_message(self):
        dispatches, increment = self._prepare(
            _pair_message(),
            [{
                "surface": service.PUBLIC_AYUE,
                "conversation_id": "ai_assistant_b",
                "other_user_id": "",
            }],
        )
        self.assertEqual(len(dispatches), 1)
        increment.assert_called_once()

    def test_muting_only_suppresses_push_not_unread(self):
        dispatches, increment = self._prepare(
            _pair_message(),
            [],
            preferences={
                "global_enabled": True,
                "public_ayue_enabled": True,
                "muted_peer_ids": ["a"],
                "muted_mediator_ids": [],
            },
        )
        self.assertEqual(dispatches, [])
        increment.assert_called_once()


class NotificationStateTests(unittest.TestCase):
    def test_unread_upsert_never_updates_one_path_with_two_operators(self):
        collection = MagicMock()
        event = service.NotificationEvent(
            recipient_id="owner",
            surface=service.PAIR,
            conversation_id="owner_other",
            other_user_id="other",
            title="對方",
            body="嗨",
            data={},
            tag="chat-test",
            message_id="message-1",
            timestamp=1.0,
        )
        with patch.object(service, "notification_threads_coll", collection):
            service._increment_unread(event)
        update = collection.update_one.call_args.args[1]
        paths = [set(value.keys()) for value in update.values()]
        for index, left in enumerate(paths):
            for right in paths[index + 1 :]:
                self.assertTrue(left.isdisjoint(right))

    def test_presence_is_per_session_and_background_deletes_it(self):
        collection = MagicMock()
        with patch.object(service, "notification_presence_coll", collection):
            service.report_notification_presence(
                user_id="owner",
                session_id="browser-1",
                visible=True,
                surface=service.PUBLIC_AYUE,
                conversation_id="ai_assistant_owner",
                now=100.0,
            )
            service.report_notification_presence(
                user_id="owner",
                session_id="browser-1",
                visible=False,
                now=101.0,
            )
        collection.update_one.assert_called_once()
        collection.delete_one.assert_called_once()

    def test_scoped_preference_uses_add_to_set_and_pull(self):
        profiles = MagicMock()
        profiles.find_one.return_value = {"notification_preferences": {}}
        with patch.object(service, "profiles_coll", profiles):
            service.update_notification_preference("owner", "peer", False, "other")
            first_update = profiles.update_one.call_args.args[1]
            service.update_notification_preference("owner", "peer", True, "other")
            second_update = profiles.update_one.call_args.args[1]
        self.assertEqual(
            first_update,
            {"$addToSet": {"notification_preferences.muted_peer_ids": "other"}},
        )
        self.assertEqual(
            second_update,
            {"$pull": {"notification_preferences.muted_peer_ids": "other"}},
        )


if __name__ == "__main__":
    unittest.main()
