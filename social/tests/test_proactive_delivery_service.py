import json
import unittest
from unittest.mock import MagicMock, patch

from services import proactive_delivery_service as delivery


class ProactiveDeliveryServiceTests(unittest.TestCase):
    def test_persisted_post_date_notice_refreshes_the_private_surface(self):
        marker = {"post_date_followup_delivery": {
            "event_id": "date-1", "other_id": "other",
            "message": "今天原本安排的約會，後來情況怎麼樣？",
        }}
        with patch.object(delivery.profiles_coll, "find_one", return_value={"user_id": "owner"}), \
             patch.object(delivery.profiles_coll, "find_one_and_update", return_value=marker), \
             patch.object(delivery, "queue_due_feedback") as queue:
            response = delivery.proactive_check("owner")
        self.assertEqual(response["surface"], "relationship_private")
        self.assertEqual(response["type"], "post_date_followup")
        self.assertEqual(response["other_id"], "other")
        queue.assert_not_called()

    def test_match_gif_is_saved_as_typed_message_in_private_mediator_room(self):
        event = {
            "event_id": "gif-1", "type": "match_connected_gif", "match_id": "match-1",
            "other_id": "other", "message": "太好了～快去和對方聊聊吧！",
            "media": {"provider": "giphy", "url": "https://media.giphy.com/demo.gif"},
        }
        match = {
            "_id": "match-1", "from_user": "owner", "to_user": "other",
            "status": "accepted", "private_unread": {"from": 0},
        }
        updated_match = {**match, "private_unread": {"from": 1}}

        with patch.object(delivery, "save_message") as save, \
             patch.object(delivery.matches_coll, "find_one_and_update", return_value=updated_match):
            response = delivery._deliver_relationship_event(
                "owner", "other", event, match, delivery._metadata(event),
            )

        self.assertEqual(response["surface"], "relationship_private")
        save.assert_called_once_with(
            "mediator_private::owner::other", "ai_assistant", event["message"],
            message_type="gif", metadata=delivery._metadata(event),
        )

    def test_legacy_probe_cleanup_is_idempotent_and_only_cancels_inflight_state(self):
        match = {
            "_id": "match-1",
            "from_user": "owner",
            "to_user": "other",
            "status": "accepted",
            "mediator_state": {
                "participants": {
                    "from": {
                        "status": "awaiting_answer",
                        "probe_id": "probe-1",
                        "kind": "fun_fact",
                    },
                },
            },
        }
        with (
            patch.object(delivery.profiles_coll, "update_one") as profile_update,
            patch.object(delivery.matches_coll, "find", return_value=[match]),
            patch.object(
                delivery.matches_coll,
                "update_one",
                return_value=MagicMock(modified_count=1),
            ) as match_update,
        ):
            stats = delivery.cleanup_legacy_private_probe_state("owner", now=5000)

        self.assertEqual(stats["matches"], 1)
        profile_update.assert_called_once()
        profile_mutation = profile_update.call_args.args[1]
        self.assertIn("mediator_inbox", profile_mutation["$pull"])
        self.assertIn("pending_private_feedback", profile_mutation["$unset"])
        match_mutation = match_update.call_args.args[1]["$set"]
        self.assertEqual(
            match_mutation["mediator_state.participants.from.status"],
            "cancelled",
        )
        self.assertEqual(
            match_mutation["mediator_state.participants.from.cancel_reason"],
            "legacy_probe_disabled",
        )

    def test_legacy_probe_event_is_suppressed_without_state_or_message_writes(self):
        event = {
            "event_id": "event-1",
            "type": "probe_question",
            "match_id": "match-1",
            "other_id": "other",
            "probe_id": "probe-1",
            "probe_kind": "fun_fact",
            "message": "跟我說一件最近的小事？",
        }
        match = {
            "_id": "match-1",
            "from_user": "owner",
            "to_user": "other",
            "status": "accepted",
            "private_unread": {"from": 0},
            "mediator_state": {
                "participants": {
                    "from": {
                        "status": "queued",
                        "probe_id": "probe-1",
                        "kind": "fun_fact",
                    }
                }
            },
        }
        updated_match = {**match, "private_unread": {"from": 1}}

        with patch.object(delivery.profiles_coll, "find_one", return_value={"user_id": "owner"}), \
             patch.object(delivery.profiles_coll, "find_one_and_update", return_value=None), \
             patch.object(delivery.profiles_coll, "update_one") as update_profile, \
             patch.object(delivery.matches_coll, "find_one_and_update", return_value=updated_match), \
             patch.object(delivery.matches_coll, "update_one") as update_match, \
             patch.object(delivery.messages_coll, "find_one") as find_message, \
             patch.object(delivery, "queue_due_feedback"), \
             patch.object(delivery, "claim_next_mediator_event", return_value=event), \
             patch.object(delivery, "_event_match", return_value=match), \
             patch.object(delivery, "save_message") as save, \
             patch.object(delivery, "consume_proactive_delivery", return_value=None) as consume:
            response = delivery.proactive_check("owner")

        self.assertEqual(response, {"has_new": False})
        save.assert_not_called()
        find_message.assert_not_called()
        update_match.assert_not_called()
        consume.assert_called_once_with("owner")

    def test_legacy_probe_does_not_block_a_following_valid_event(self):
        old_event = {"event_id": "old", "type": "probe_question", "message": "週末？"}
        valid_event = {"event_id": "valid", "type": "match_search_empty"}
        with (
            patch.object(delivery.profiles_coll, "find_one", return_value={"user_id": "owner"}),
            patch.object(delivery.profiles_coll, "find_one_and_update", return_value=None),
            patch.object(
                delivery,
                "claim_next_mediator_event",
                side_effect=[old_event, valid_event],
            ) as claim,
            patch.object(
                delivery,
                "_deliver_claimed_event",
                return_value={"has_new": True, "type": "match_search_empty"},
            ) as deliver,
            patch.object(delivery, "consume_proactive_delivery") as consume,
        ):
            response = delivery.proactive_check("owner")

        self.assertEqual(response["has_new"], True)
        self.assertEqual(claim.call_count, 2)
        deliver.assert_called_once_with("owner", valid_event)
        consume.assert_not_called()

    def test_stale_proposal_is_not_saved_or_replaced_by_care_message(self):
        event = {
            "event_id": "event-2",
            "type": "match_proposal",
            "match_id": "0123456789abcdef01234567",
            "message": "找到一位人選。",
        }
        with patch.object(delivery.profiles_coll, "find_one", return_value={"user_id": "owner"}), \
             patch.object(delivery.profiles_coll, "find_one_and_update", return_value=None), \
             patch.object(delivery.matches_coll, "find_one", return_value=None), \
             patch.object(delivery, "queue_due_feedback"), \
             patch.object(delivery, "claim_next_mediator_event", return_value=event), \
             patch.object(delivery, "_event_match", return_value=None), \
             patch.object(delivery, "save_message") as save, \
             patch.object(delivery, "consume_proactive_delivery") as consume:
            response = delivery.proactive_check("owner")

        self.assertEqual(response, {"has_new": False, "stale": True})
        save.assert_not_called()
        consume.assert_not_called()

    def test_stale_receiver_intro_is_not_saved_after_the_match_has_ended(self):
        event = {
            "event_id": "event-intro", "type": "incoming_match_intro",
            "match_id": "0123456789abcdef01234567", "message": "小葵小葵～先看看我整理的提案。",
        }
        with patch.object(delivery.matches_coll, "find_one", return_value=None), \
             patch.object(delivery, "create_proposal_room") as create_room, \
             patch.object(delivery, "save_message") as save:
            response = delivery._deliver_global_event("owner", event, delivery._metadata(event))

        self.assertEqual(response, {"has_new": False, "stale": True})
        save.assert_not_called()
        create_room.assert_not_called()

    def test_live_event_receiver_intro_is_dropped_without_an_empty_card(self):
        event = {
            "event_id": "event-intro", "type": "incoming_match_intro",
            "match_id": "0123456789abcdef01234567",
            "message": "先看看我整理的活動提案。",
        }
        match = {
            "_id": "0123456789abcdef01234567",
            "from_user": "other", "to_user": "owner", "status": "pending",
            "proposal_namespace": "event_invitation",
            "proposal_source": "event_opportunity",
        }
        with patch.object(delivery.matches_coll, "find_one", return_value=match), \
             patch.object(delivery, "create_proposal_room") as create_room, \
             patch.object(delivery, "save_system_message_once") as save:
            response = delivery._deliver_global_event(
                "owner", event, delivery._metadata(event),
            )

        self.assertEqual(response, {
            "has_new": False,
            "deduplicated": True,
            "event_intro_suppressed": True,
        })
        create_room.assert_not_called()
        save.assert_not_called()

    def test_v4_proposal_delivery_rebuilds_only_the_viewer_projection(self):
        event = {
            "event_id": "event-v4", "type": "incoming_match_interest",
            "match_id": "0123456789abcdef01234567", "other_id": "seed_user_01",
            "proposal_role": "receiver", "message": "有位人選想認識你。",
            "origin_room_id": "ai_room::owner::origin",
            "matches": [{"receiver_reason": "舊資料", "reason_items": ["不應送出"]}],
        }
        invitation = "有位比較外向的人，最近提到「想去郊區讀書」。你們或許可以先聊聊；你想認識對方嗎？"
        match = {
            "_id": "0123456789abcdef01234567", "from_user": "seed_user_01", "to_user": "owner",
            "status": "pending", "reason_version": "v4_friend_intro",
            "proposal_delivery_rooms": {"receiver": "ai_room::owner::origin"},
            "friend_intro_v4": {
                "initiator_preview": {
                    "viewer_id": "seed_user_01", "counterparty_id": "owner",
                    "viewer_text": "給另一方向看的文字",
                },
                "receiver_invitation": {
                    "viewer_id": "owner", "counterparty_id": "seed_user_01",
                    "counterparty_context_snapshot": "想去郊區讀書",
                    "counterparty_public_personality": "比較外向",
                    "viewer_public_personality": "",
                    "viewer_text": invitation,
                },
            },
        }
        with patch.object(delivery.matches_coll, "find_one", return_value=match), \
             patch.object(delivery, "get_room", return_value={"room_id": "ai_room::owner::origin"}), \
             patch.object(delivery, "save_system_message_once", return_value={"message_id": "card"}) as save_notice, \
             patch.object(delivery, "create_proposal_room") as create_room:
            response = delivery._deliver_global_event("owner", event, delivery._metadata(event))

        projected = response["matches"][0]
        self.assertIn("想去郊區讀書", projected["viewer_reason"])
        self.assertIn("比較外向", projected["viewer_reason"])
        self.assertEqual(projected["reason_version"], "v4_friend_intro")
        self.assertNotIn("receiver_reason", projected)
        self.assertNotIn("reason_items", projected)
        serialized = json.dumps(response, ensure_ascii=False)
        self.assertNotIn("seed_user", serialized)
        self.assertNotIn("給另一方向看的文字", serialized)
        saved_metadata = save_notice.call_args.kwargs["metadata"]
        self.assertIsNone(saved_metadata["other_id"])
        create_room.assert_not_called()
        self.assertNotIn("proposal_room_id", response)
        self.assertEqual(response["surface"], "global_mediator")
        self.assertEqual(response["type"], "incoming_match_interest")
        self.assertEqual(response["origin_room_id"], "ai_room::owner::origin")
        self.assertEqual(save_notice.call_args.kwargs["message_type"], "mediator_card")
        self.assertEqual(save_notice.call_args.kwargs["metadata"]["event_type"], "incoming_match_interest")

    def test_proposal_delivery_creates_dedicated_room_with_card_message(self):
        event = {
            "event_id": "event-proposal", "type": "match_proposal",
            "match_id": "0123456789abcdef01234567", "message": "找到一位人選。",
            "origin_room_id": "ai_room::owner::origin",
        }
        match = {
            "_id": "0123456789abcdef01234567", "from_user": "owner", "to_user": "seed_user_01",
            "status": "pending", "reason_version": "legacy",
            "proposal_delivery_rooms": {"initiator": "ai_room::owner::origin"},
        }
        with patch.object(delivery.matches_coll, "find_one", return_value=match), \
             patch.object(delivery, "reason_for_viewer", return_value="小晴和你都喜歡爬山。"), \
             patch.object(delivery, "get_room", return_value={"room_id": "ai_room::owner::origin"}), \
             patch.object(delivery, "save_system_message_once", return_value={"message_id": "card"}) as save_notice, \
             patch.object(delivery, "create_proposal_room") as create_room:
            response = delivery._deliver_global_event("owner", event, delivery._metadata(event))

        self.assertTrue(response["has_new"])
        self.assertNotIn("proposal_room_id", response)
        create_room.assert_not_called()
        call_args = save_notice.call_args
        self.assertEqual(call_args.args[0], "ai_room::owner::origin")
        self.assertEqual(call_args.args[1], "找到一位人選。")
        card_metadata = call_args.kwargs["metadata"]
        self.assertEqual(card_metadata["event_type"], "match_proposal")
        self.assertEqual(card_metadata["matches"][0]["match_id"], "0123456789abcdef01234567")
        self.assertEqual(card_metadata["matches"][0]["viewer_reason"], "小晴和你都喜歡爬山。")
        self.assertNotIn("seed_user_01", json.dumps(response))
        self.assertEqual(save_notice.call_args.args[0], "ai_room::owner::origin")
        self.assertEqual(save_notice.call_args.kwargs["message_type"], "mediator_card")
        self.assertEqual(card_metadata["canonical_status"], "pending")

    def test_legacy_automatic_proposal_is_suppressed_without_expiring(self):
        event = {
            "event_id": "event-auto", "type": "match_proposal",
            "match_id": "0123456789abcdef01234567", "message": "舊的自動配對。",
        }
        match = {
            "_id": "0123456789abcdef01234567", "from_user": "owner",
            "to_user": "other", "status": "draft", "proposal_source": "automatic",
        }
        with patch.object(delivery.matches_coll, "find_one", return_value=match), \
             patch.object(delivery.matches_coll, "update_one") as suppress, \
             patch.object(delivery, "create_proposal_room") as create_room:
            response = delivery._deliver_global_event(
                "owner", event, delivery._metadata(event),
            )

        self.assertTrue(response["automatic_proposal_suppressed"])
        create_room.assert_not_called()
        update = suppress.call_args.args[1]
        self.assertTrue(update["$set"]["proposal_suppressed"])
        self.assertNotIn("status", update["$set"])
        self.assertNotIn("expired_at", update["$set"])

    def test_proposal_delivery_keeps_legacy_room_path_for_non_proposal_events(self):
        event = {
            "event_id": "event-care", "type": "proactive_care",
            "message": "最近過得如何？",
        }
        with patch.object(delivery, "most_recent_ai_room", return_value="ai_room::owner::legacy") as most_recent, \
             patch.object(delivery, "save_system_message_once") as save, \
             patch.object(delivery, "create_proposal_room") as create_room:
            response = delivery._deliver_global_event("owner", event, delivery._metadata(event))

        self.assertTrue(response["has_new"])
        self.assertNotIn("proposal_room_id", response)
        save.assert_called_once()
        self.assertEqual(save.call_args.args[0], "ai_room::owner::legacy")
        most_recent.assert_called_once_with("owner")
        create_room.assert_not_called()

    def test_non_proposal_result_uses_valid_origin_room_and_event_dedupe_key(self):
        event = {
            "event_id": "event-empty", "event_key": "job:empty",
            "type": "match_search_empty", "message": "這輪沒有新人選。",
            "origin_room_id": "ai_room::owner::origin",
        }
        with patch.object(delivery, "get_room", return_value={"room_id": "ai_room::owner::origin"}), \
             patch.object(delivery, "most_recent_ai_room") as most_recent, \
             patch.object(delivery, "save_system_message_once") as save:
            response = delivery._deliver_global_event("owner", event, delivery._metadata(event))

        self.assertEqual(response["origin_room_id"], "ai_room::owner::origin")
        self.assertEqual(save.call_args.args[0], "ai_room::owner::origin")
        self.assertEqual(save.call_args.kwargs["event_key"], "job:empty")
        most_recent.assert_not_called()

    def test_memory_notice_precedes_claimed_event_and_care_delivery(self):
        notice_doc = {
            "memory_notices": [{"message": "我記住了。", "memory": {"kind": "preference"}}]
        }
        with patch.object(delivery.profiles_coll, "find_one", return_value={"user_id": "owner"}), \
             patch.object(delivery.profiles_coll, "find_one_and_update", return_value=notice_doc), \
             patch.object(delivery, "queue_due_feedback"), \
             patch.object(delivery, "claim_next_mediator_event") as claim, \
             patch.object(delivery, "consume_proactive_delivery") as consume:
            response = delivery.proactive_check("owner")

        self.assertEqual(response, {
            "has_new": True,
            "surface": "ephemeral_notice",
            "type": "memory_learned",
            "message": "我記住了。",
            "memory": {"kind": "preference"},
        })
        claim.assert_not_called()
        consume.assert_not_called()

    def test_proactive_care_delivery_preserves_origin_room_without_internal_ids(self):
        marker = {
            "message": "看展後感覺如何？", "message_id": "care-1",
            "origin_room_id": "ai_room::owner::one",
        }
        with patch.object(delivery.profiles_coll, "find_one", return_value={"user_id": "owner"}), \
             patch.object(delivery.profiles_coll, "find_one_and_update", return_value=None), \
             patch.object(delivery, "queue_due_feedback"), \
             patch.object(delivery, "claim_next_mediator_event", return_value=None), \
             patch.object(delivery, "consume_proactive_delivery", return_value=marker):
            response = delivery.proactive_check("owner")
        self.assertEqual(response["origin_room_id"], "ai_room::owner::one")
        self.assertEqual(response["type"], "proactive_care")
        self.assertNotIn("message_id", response)


if __name__ == "__main__":
    unittest.main()
