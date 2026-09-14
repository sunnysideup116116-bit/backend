import unittest
from unittest.mock import Mock, patch

from services.ayue_agent.proactive_care import (
    ProactiveCareContext,
    build_proactive_care_context,
    claim_proactive_care,
    finalize_proactive_care_claim,
    generate_proactive_care,
    normalize_proactive_frequency,
    proactive_frequency_seconds,
)
from services.message_use_service import metadata_for_use


class ProactiveCareTests(unittest.TestCase):
    def setUp(self):
        self.context = ProactiveCareContext(
            latest_owner_message="我最近想去合掌村旅行",
            previous_assistant_message="",
            recent_context="近期規劃合掌村旅行",
            tone="friend", local_date="2026-07-30", local_period="下午",
        )

    def test_grounded_care_keeps_ayue_as_speaker(self):
        with patch(
            "services.ayue_agent.proactive_care.generate_chat_completion",
            return_value='{"message":"合掌村聽起來很值得期待，你最想看雪景還是合掌造？","focus":"latest_message","grounding_span":"我最近想去合掌村旅行","confidence":0.91}',
        ):
            result = generate_proactive_care(self.context)
        self.assertIsNotNone(result)
        self.assertNotIn("欸阿月", result.message)

    def test_role_inversion_or_missing_grounding_sends_nothing(self):
        with patch(
            "services.ayue_agent.proactive_care.generate_chat_completion",
            return_value='{"message":"欸阿月，你最近想去合掌村嗎？","focus":"latest_message","grounding_span":"我最近想去合掌村旅行","confidence":0.95}',
        ):
            self.assertIsNone(generate_proactive_care(self.context))
        with patch(
            "services.ayue_agent.proactive_care.generate_chat_completion",
            return_value='{"message":"聽起來不錯。","focus":"latest_message","grounding_span":"不存在的內容","confidence":0.95}',
        ):
            self.assertIsNone(generate_proactive_care(self.context))

    def test_invalid_first_answer_gets_one_bounded_repair(self):
        with patch(
            "services.ayue_agent.proactive_care.generate_chat_completion",
            side_effect=[
                "not-json",
                '{"message":"合掌村之旅感覺很有畫面，你最期待哪一段？","focus":"recent_context","grounding_span":"合掌村旅行","confidence":0.93}',
            ],
        ) as model:
            result = generate_proactive_care(self.context)
        self.assertIsNotNone(result)
        self.assertEqual(model.call_count, 2)

    def test_control_only_latest_message_uses_recent_context(self):
        context = self.context.model_copy(update={"latest_owner_message": "確認"})
        with patch(
            "services.ayue_agent.proactive_care.generate_chat_completion",
            return_value='{"message":"合掌村旅行準備得如何了？","focus":"recent_context","grounding_span":"合掌村旅行","confidence":0.93}',
        ) as model:
            result = generate_proactive_care(context)
        self.assertIsNotNone(result)
        self.assertNotIn('"latest_owner_message": "確認"', model.call_args.args[0])

    def test_candidate_care_must_keep_followup_focus(self):
        context = self.context.model_copy(update={"followup_grounding_span": "週末想去看展"})
        with patch(
            "services.ayue_agent.proactive_care.generate_chat_completion",
            return_value='{"message":"旅行準備得如何了？","focus":"latest_message","grounding_span":"我最近想去合掌村旅行","confidence":0.93}',
        ):
            self.assertIsNone(generate_proactive_care(context))

    def test_memory_wording_preserves_preference_stance(self):
        cursor = Mock()
        cursor.sort.return_value.limit.return_value = []
        with patch(
            "services.ayue_agent.proactive_care.messages_coll.find",
            return_value=cursor,
        ):
            liked = build_proactive_care_context(
                "owner",
                {"profile_memory_preview": [{"label": "熱鬧場所", "stance": "like"}]},
                room_id="room",
            )
            avoided = build_proactive_care_context(
                "owner",
                {"profile_memory_preview": [{"label": "熱鬧場所", "stance": "avoid"}]},
                room_id="room",
            )
        self.assertEqual(liked.relevant_memories, ["喜歡：熱鬧場所"])
        self.assertEqual(avoided.relevant_memories, ["避免：熱鬧場所"])

    def test_followup_evidence_near_end_of_source_message_is_preserved(self):
        cursor = Mock()
        cursor.sort.return_value.limit.return_value = []
        evidence = "下個月考完證照想去吃燒肉"
        source = {
            "content": "前情" * 130 + evidence,
            "metadata": {"message_use": metadata_for_use("ordinary")},
        }
        candidate = {
            "source_message_id": "64b64c8f0000000000000001",
            "evidence_span": evidence,
        }
        with patch(
            "services.ayue_agent.proactive_care.messages_coll.find",
            return_value=cursor,
        ), patch(
            "services.ayue_agent.proactive_care.messages_coll.find_one",
            return_value=source,
        ):
            context = build_proactive_care_context(
                "owner", {}, room_id="room", candidate=candidate,
            )
        self.assertEqual(context.followup_grounding_span, evidence)

    def test_frequency_values_are_canonical_and_legacy_safe(self):
        self.assertEqual(normalize_proactive_frequency("normal"), "3600")
        self.assertEqual(proactive_frequency_seconds("60"), 60)
        self.assertIsNone(proactive_frequency_seconds("unexpected"))

    def test_claim_and_finalize_are_activity_scoped(self):
        with patch(
            "services.ayue_agent.proactive_care.profiles_coll.find_one_and_update",
            return_value={"user_id": "owner"},
        ) as claim, patch(
            "services.ayue_agent.proactive_care.profiles_coll.update_one",
            return_value=Mock(modified_count=1),
        ) as finalize:
            claim_id = claim_proactive_care("owner", 123.0, now=124.0)
            self.assertTrue(claim_id)
            self.assertTrue(finalize_proactive_care_claim("owner", claim_id, 123.0, delivered=False, now=125.0))
        self.assertEqual(claim.call_args.args[0]["last_user_activity_at"], 123.0)
        self.assertEqual(finalize.call_args.args[0]["proactive_care_claim_id"], claim_id)
        self.assertEqual(finalize.call_args.args[1]["$set"]["last_followup_activity_at"], 123.0)

    def test_calendar_turn_does_not_fall_back_to_an_older_care_topic(self):
        cursor = Mock()
        cursor.sort.return_value.limit.return_value = [
            {
                "sender_id": "owner",
                "content": "幫我新增週五聚餐",
                "metadata": {"message_use": metadata_for_use("calendar_operation")},
            },
            {
                "sender_id": "owner",
                "content": "我最近想爬山",
                "metadata": {"message_use": metadata_for_use("ordinary")},
            },
        ]
        with patch(
            "services.ayue_agent.proactive_care.most_recent_ai_room",
            return_value="room",
        ), patch(
            "services.ayue_agent.proactive_care.messages_coll.find",
            return_value=cursor,
        ):
            context = build_proactive_care_context(
                "owner", {"current_context": "近期想爬山"},
            )
        self.assertEqual(context.latest_owner_message, "")
        self.assertEqual(context.recent_context, "")


if __name__ == "__main__":
    unittest.main()
