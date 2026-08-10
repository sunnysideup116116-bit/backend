import json
import unittest
from pathlib import Path
from unittest.mock import patch

from services.ayue_agent.capabilities import (
    CAPABILITY_MANIFEST,
    CAPABILITY_MANIFEST_VERSION,
    product_info_answer,
    product_info_projection,
)
from services.ayue_agent.onboarding import (
    PUBLIC_AYUE_ONBOARDING_MESSAGES,
    public_ayue_onboarding_state,
)
from services.ayue_agent.v3.public_reply import build_presentation


class ProductSurfaceContractTests(unittest.TestCase):
    def test_manifest_describes_same_identity_and_bounded_surfaces(self):
        self.assertEqual(CAPABILITY_MANIFEST_VERSION, "v5")
        self.assertEqual(CAPABILITY_MANIFEST["surfaces"]["public"]["identity"], "same_ayue")
        self.assertFalse(CAPABILITY_MANIFEST["surfaces"]["context_boundary"]["raw_full_chat_cross_surface"])
        self.assertFalse(CAPABILITY_MANIFEST["surfaces"]["context_boundary"]["private_messages_update_public_profile"])
        self.assertFalse(CAPABILITY_MANIFEST["surfaces"]["public"]["can_read_current_pair_chat"])
        self.assertTrue(CAPABILITY_MANIFEST["surfaces"]["private"]["can_read_current_pair_chat"])

    def test_product_info_is_bounded_and_deterministic(self):
        messages = product_info_answer(["same_identity", "cross_surface_context", "where_to_ask"])
        self.assertLessEqual(len(messages), 2)
        self.assertTrue(any("同一位阿月" in message for message in messages))

    def test_matching_principles_answers_the_question_instead_of_repeating_identity_copy(self):
        messages = product_info_answer(["matching_principles"])
        joined = " ".join(messages)
        self.assertIn("不會隨機配對", joined)
        self.assertIn("排序", joined)
        self.assertIn("確認", joined)
        self.assertNotIn("我是阿月", joined)

    def test_product_info_projection_contains_only_selected_typed_facts(self):
        projection = product_info_projection(["same_identity", "surface_scope"])
        self.assertEqual(projection["manifest_version"], "v5")
        self.assertEqual(projection["topics"], ["same_identity", "surface_scope"])
        self.assertEqual(set(projection["facts"]), {"identity", "surface_scope"})
        self.assertNotIn("matching", projection["facts"])

    def test_relationship_chat_access_projection_distinguishes_public_and_private(self):
        projection = product_info_projection(["relationship_chat_access", "where_to_ask"])
        access = projection["facts"]["relationship_chat_access"]
        self.assertFalse(access["public_can_read_current_pair_chat"])
        self.assertTrue(access["private_can_read_current_pair_chat"])
        self.assertTrue(access["use_private_for_contextual_chat_advice"])
        self.assertIn("近期聊天紀錄", projection["facts"]["where_to_ask"]["private"])

    def test_presentation_limits_reject_transaction_split(self):
        self.assertIsNone(build_presentation(["第一則", "第二則"], "transaction"))
        self.assertIsNotNone(build_presentation(["第一則", "第二則"], "product_info"))

    def test_onboarding_has_three_fixed_bubbles_only_for_empty_public_room(self):
        with patch("services.ayue_agent.onboarding.profiles_coll.find_one", return_value={}), \
             patch("services.ayue_agent.onboarding.messages_coll.count_documents", return_value=0):
            state = public_ayue_onboarding_state("owner")
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["messages"], list(PUBLIC_AYUE_ONBOARDING_MESSAGES))
        self.assertEqual(len(state["messages"]), 3)

    def test_voice_fixture_has_forty_cases_and_fourteen_identity_cases(self):
        fixture = Path(__file__).parent / "fixtures" / "ayue_voice_eval_v1.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(len(data["cases"]), 40)
        identity_categories = {"same_identity", "cross_surface_context", "where_to_ask", "matching_scope", "capabilities", "surface_scope", "private_message_visibility"}
        self.assertEqual(sum(item["category"] in identity_categories for item in data["cases"]), 14)


if __name__ == "__main__":
    unittest.main()
