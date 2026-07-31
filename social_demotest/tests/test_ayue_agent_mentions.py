import unittest
from unittest.mock import patch

from services.ayue_agent.contracts import AgentTurnContext, AgentTurnContextV2, ToolCall
from services.ayue_agent.router import tool_policy_for_turn
from services.ayue_agent.tools import execute_tool
from services.ayue_agent.public_relationship_projection import validated_mentioned_contact_ids


class AyueAgentMentionTests(unittest.TestCase):
    def test_mentioned_contact_tool_requires_a_validated_entity_reference(self):
        no_mention = AgentTurnContextV2(user_id="owner", room_id="room", message="她最近如何")
        with_mention = AgentTurnContextV2(
            user_id="owner", room_id="room", message="她最近如何",
            mentioned_contacts=[{"display_name": "小安"}],
        )
        self.assertNotIn("relationship.get_mentioned_contact_summary", tool_policy_for_turn(no_mention))
        self.assertIn("relationship.get_mentioned_contact_summary", tool_policy_for_turn(with_mention))

    def test_more_than_three_mentions_do_not_silently_inspect_a_subset(self):
        with patch(
            "services.ayue_agent.public_relationship_projection.matches_coll.find_one",
            return_value={"_id": "accepted"},
        ):
            ids, overflow = validated_mentioned_contact_ids("owner", ["a", "b", "c", "d"])
        self.assertEqual(ids, ["a", "b", "c"])
        self.assertTrue(overflow)
        turn = AgentTurnContextV2(
            user_id="owner", room_id="room", message="幫我比較他們",
            mentioned_contacts=[{"display_name": "甲"}, {"display_name": "乙"}, {"display_name": "丙"}],
            mentioned_contact_overflow=True,
        )
        self.assertNotIn("relationship.get_mentioned_contact_summary", tool_policy_for_turn(turn))

    def test_mentioned_summary_exposes_public_fields_only(self):
        match = {
            "from_user": "owner", "to_user": "seed_user_08", "status": "accepted",
            "reason_items": [{"kind": "shared_context", "text": "你們都想去居酒屋"}],
            "distinctive_tags": ["小酌"],
        }
        profile = {
            "display_name": "小安", "current_context": "最近想去居酒屋小酌",
            "initial_interest": "喜歡慢慢認識", "big_five": {"summary": "溫和而願意傾聽"},
            "profile_memory_summary": "私人內容", "calendar": "私人行程",
        }
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="@小安最近在做什麼")
        with patch(
            "services.ayue_agent.public_relationship_projection.matches_coll.find_one", return_value=match,
        ), patch(
            "services.ayue_agent.public_relationship_projection.profiles_coll.find_one", return_value=profile,
        ):
            result = execute_tool(ToolCall(
                name="relationship.get_mentioned_contact_summary",
                arguments={"other_ids": ["seed_user_08"]},
            ), ctx)
        self.assertTrue(result.ok)
        contact = result.data["contacts"][0]
        self.assertEqual(contact["display_name"], "小安")
        self.assertIn("居酒屋", contact["recent_context"])
        self.assertNotIn("seed_user_08", str(result.data))
        self.assertNotIn("私人內容", str(result.data))
        self.assertNotIn("私人行程", str(result.data))


if __name__ == "__main__":
    unittest.main()
