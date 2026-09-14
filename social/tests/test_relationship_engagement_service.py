import unittest
import json
from unittest.mock import MagicMock, patch

from services import relationship_engagement_service as engagement


class RelationshipEngagementServiceTests(unittest.TestCase):
    def test_shared_summary_recursively_uses_only_new_pair_messages(self):
        cursor = MagicMock()
        cursor.sort.return_value.skip.return_value.limit.return_value = [
            {"sender_id": "owner", "content": "最近聊到看展", "timestamp": 10},
        ]
        response = MagicMock(content=json.dumps({
            "shared_summary": "兩人延續看展話題",
            "interaction_tone": "自在",
            "common_topics": ["看展"],
            "conversation_hooks": ["近期展覽"],
        }))
        match = {"_id": "match-1", "relationship_memory": {
            "shared_summary": "兩人先前聊過電影", "last_summarized_count": 8,
        }}
        with patch.object(engagement.matches_coll, "find_one", return_value=match), \
             patch.object(engagement.messages_coll, "count_documents", return_value=12), \
             patch.object(engagement.messages_coll, "find", return_value=cursor), \
             patch.object(engagement, "generate_chat_completion", return_value=response) as model, \
             patch.object(engagement.matches_coll, "update_one") as update:
            engagement.summarize_relationship("match-1", "pair-room")
        cursor.sort.return_value.skip.assert_called_once_with(8)
        self.assertIn("兩人先前聊過電影", model.call_args.args[0])
        saved = update.call_args.args[1]["$set"]["relationship_memory"]
        self.assertEqual(saved["last_summarized_count"], 9)

    def test_unrelated_turn_does_not_consume_pending_date_feedback(self):
        match = {"_id": "match-1", "from_user": "owner", "to_user": "other"}
        user = {"pending_post_date_feedback": {
            "relationship_id": "match-1", "other_id": "other", "event_id": "date-1",
            "expires_at": 20_000,
        }}
        with patch.object(engagement.time, "time", return_value=10_000), \
             patch.object(engagement.profiles_coll, "update_one") as update:
            reply = engagement.consume_pending_post_date_feedback(
                match, user, "owner", "other", "幫我查明天天氣", source_message_id="m1",
            )
        self.assertIsNone(reply)
        update.assert_not_called()

    def test_declining_date_feedback_closes_question_without_negative_memory(self):
        match = {"_id": "match-1", "from_user": "owner", "to_user": "other"}
        user = {"pending_post_date_feedback": {
            "relationship_id": "match-1", "other_id": "other", "event_id": "date-1",
            "expires_at": 20_000,
        }}
        with patch.object(engagement.time, "time", return_value=10_000), \
             patch.object(engagement.profiles_coll, "update_one"), \
             patch("services.post_date_followup_service.POST_DATE_FOLLOWUPS.update_one"), \
             patch("services.relationship_memory_service.enqueue_relationship_memory_extraction") as enqueue:
            reply = engagement.consume_pending_post_date_feedback(
                match, user, "owner", "other", "我不想聊這個", source_message_id="m1",
            )
        self.assertIn("不會", reply)
        enqueue.assert_not_called()

    def test_related_date_feedback_is_private_and_queued_for_memory_review(self):
        match = {"_id": "match-1", "from_user": "owner", "to_user": "other"}
        user = {"pending_post_date_feedback": {
            "relationship_id": "match-1", "other_id": "other", "event_id": "date-1",
            "expires_at": 20_000,
        }}
        with patch.object(engagement.time, "time", return_value=10_000), \
             patch.object(engagement.profiles_coll, "update_one"), \
             patch("services.post_date_followup_service.POST_DATE_FOLLOWUPS.update_one"), \
             patch("services.relationship_memory_service.enqueue_relationship_memory_extraction") as enqueue:
            reply = engagement.consume_pending_post_date_feedback(
                match, user, "owner", "other", "今天聊得很開心，想再約一次",
                source_message_id="m1",
            )
        self.assertIn("不會自動轉述", reply)
        enqueue.assert_called_once_with(
            "owner", "other", "match-1", "m1", date_event_id="date-1",
        )

    def test_auto_probe_obeys_the_proactive_care_switch(self):
        with patch("services.proactive_followup_service.followup_mode_for_user", return_value="on"), \
             patch.object(engagement.profiles_coll, "find_one", return_value={"proactive_care_enabled": False}), \
             patch.object(engagement.matches_coll, "find") as find:
            engagement.queue_due_feedback("owner")
        find.assert_not_called()

    def test_auto_probe_uses_cas_then_queues_exactly_one_event(self):
        match = {
            "_id": "match-1",
            "from_user": "owner",
            "to_user": "other",
            "shared_message_count": 8,
            "last_chat_at": 0,
            "mediator_state": {"participants": {"from": {}}},
        }
        updated = MagicMock(modified_count=1)
        probe_id = MagicMock(hex="probe-1")
        with patch.object(engagement, "probe_policy", return_value=("balanced", 8, 1800, 86400)), \
             patch("services.proactive_followup_service.followup_mode_for_user", return_value="on"), \
             patch.object(engagement.profiles_coll, "find_one", return_value={"proactive_care_enabled": True}), \
             patch.object(engagement.profiles_coll, "update_one"), \
             patch.object(engagement.matches_coll, "find", return_value=[match]), \
             patch.object(engagement.matches_coll, "update_one", return_value=updated) as update, \
             patch.object(engagement.uuid, "uuid4", return_value=probe_id), \
             patch.object(engagement, "queue_mediator_event") as queue, \
             patch.object(engagement.time, "time", return_value=10_000):
            engagement.queue_due_feedback("owner")

        self.assertEqual(update.call_count, 1)
        query, mutation = update.call_args.args
        self.assertEqual(query["_id"], "match-1")
        state = mutation["$set"]["mediator_state.participants.from"]
        self.assertEqual(state["probe_id"], "probe-1")
        self.assertEqual(state["kind"], "fun_fact")
        queue.assert_called_once_with(
            "owner", engagement.PROBE_QUESTIONS["fun_fact"], "probe_question",
            match_id="match-1", other_id="other", origin="auto",
            probe_kind="fun_fact", probe_id="probe-1",
        )

    def test_auto_probe_does_not_publish_when_compare_and_set_loses_race(self):
        match = {
            "_id": "match-1", "from_user": "owner", "to_user": "other",
            "shared_message_count": 8, "last_chat_at": 0,
            "mediator_state": {"participants": {"from": {}}},
        }
        with patch.object(engagement, "probe_policy", return_value=("balanced", 8, 1800, 86400)), \
             patch("services.proactive_followup_service.followup_mode_for_user", return_value="on"), \
             patch.object(engagement.profiles_coll, "find_one", return_value={"proactive_care_enabled": True}), \
             patch.object(engagement.profiles_coll, "update_one"), \
             patch.object(engagement.matches_coll, "find", return_value=[match]), \
             patch.object(engagement.matches_coll, "update_one", return_value=MagicMock(modified_count=0)), \
             patch.object(engagement, "queue_mediator_event") as queue, \
             patch.object(engagement.time, "time", return_value=10_000):
            engagement.queue_due_feedback("owner")

        queue.assert_not_called()

    def test_manual_fun_fact_uses_cas_before_queuing_private_event(self):
        match = {
            "_id": "match-1", "from_user": "owner", "to_user": "other",
            "shared_message_count": 3, "mediator_state": {"participants": {"to": {}}},
        }
        probe_id = MagicMock(hex="manual-probe-1")
        with patch.object(engagement.time, "time", return_value=10_000), \
             patch.object(engagement.uuid, "uuid4", return_value=probe_id), \
             patch.object(engagement.matches_coll, "update_one", return_value=MagicMock(modified_count=1)) as update, \
             patch.object(engagement, "queue_mediator_event") as queue:
            queued = engagement.queue_manual_fun_fact_probe(match, "owner", "other")

        self.assertTrue(queued)
        query, mutation = update.call_args.args
        self.assertEqual(query["_id"], "match-1")
        self.assertEqual(mutation["$set"]["mediator_state.participants.to"]["trigger"], "manual")
        queue.assert_called_once_with(
            "other", engagement.PROBE_QUESTIONS["fun_fact"], "probe_question",
            match_id="match-1", other_id="owner", origin="manual", requester_id="owner",
            probe_kind="fun_fact", probe_id="manual-probe-1",
        )

    def test_manual_fun_fact_does_not_publish_when_compare_and_set_loses_race(self):
        match = {
            "_id": "match-1", "from_user": "owner", "to_user": "other",
            "shared_message_count": 3, "mediator_state": {"participants": {"to": {}}},
        }
        with patch.object(engagement.matches_coll, "update_one", return_value=MagicMock(modified_count=0)), \
             patch.object(engagement, "queue_mediator_event") as queue:
            queued = engagement.queue_manual_fun_fact_probe(match, "owner", "other")

        self.assertFalse(queued)
        queue.assert_not_called()

    def test_pending_probe_answer_uses_cas_then_publishes_shareable_result(self):
        match = {
            "_id": "match-1", "from_user": "owner", "to_user": "other",
            "mediator_state": {"participants": {"to": {
                "status": "awaiting_answer", "probe_id": "probe-1",
                "kind": "fun_fact", "requester_id": "owner",
            }}},
        }
        user_doc = {"pending_private_feedback": {
            "match_id": "match-1", "other_id": "owner", "probe_id": "probe-1",
        }}
        with patch.object(engagement.time, "time", return_value=10_000), \
             patch.object(engagement.matches_coll, "update_one", return_value=MagicMock(modified_count=1)) as update_match, \
             patch.object(engagement.profiles_coll, "update_one") as update_profile, \
             patch.object(engagement, "queue_mediator_event") as queue:
            reply = engagement.consume_pending_probe_answer(
                match, user_doc, "other", "owner", "  我最近\t喜歡游泳  ",
            )

        self.assertEqual(reply, "收到，我記下來了，之後會用這個幫你們找話題。")
        mutation = update_match.call_args.args[1]["$set"]
        result = mutation["mediator_state.probe_results.probe-1"]
        self.assertEqual(result["answer"], "我最近 喜歡游泳")
        self.assertTrue(result["shareable"])
        update_profile.assert_called_once()
        queue.assert_called_once_with(
            "owner", "我幫你問到一個可聊的點：對方說「我最近 喜歡游泳」。你可以順著這個接話。",
            "probe_result", match_id="match-1", other_id="other", probe_id="probe-1",
        )

    def test_pending_probe_answer_cas_loss_does_not_clear_or_publish(self):
        match = {
            "_id": "match-1", "from_user": "owner", "to_user": "other",
            "mediator_state": {"participants": {"to": {
                "status": "awaiting_answer", "probe_id": "probe-1",
                "kind": "fun_fact", "requester_id": "owner",
            }}},
        }
        user_doc = {"pending_private_feedback": {
            "match_id": "match-1", "other_id": "owner", "probe_id": "probe-1",
        }}
        with patch.object(engagement.matches_coll, "update_one", return_value=MagicMock(modified_count=0)), \
             patch.object(engagement.profiles_coll, "update_one") as update_profile, \
             patch.object(engagement, "queue_mediator_event") as queue:
            reply = engagement.consume_pending_probe_answer(
                match, user_doc, "other", "owner", "我最近喜歡游泳",
            )

        self.assertEqual(reply, "這題已經處理完成，我不會再重問。")
        update_profile.assert_not_called()
        queue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
