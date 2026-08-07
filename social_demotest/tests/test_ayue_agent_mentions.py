import unittest
from unittest.mock import patch

from services.ayue_agent.contracts import AgentTurnContext, AgentTurnContextV2, ToolCall
from services.ayue_agent.tools import execute_tool
from services.ayue_agent.public_relationship_projection import (
    accepted_contact_ids_by_display_name,
    accepted_contact_summaries,
    validated_mentioned_contact_ids,
)


class AyueAgentMentionTests(unittest.TestCase):
    def test_display_name_resolver_keeps_duplicate_accepted_contacts_for_executor_disambiguation(self):
        matches = [
            {"from_user": "owner", "to_user": "contact-a"},
            {"from_user": "contact-b", "to_user": "owner"},
        ]
        with patch(
            "services.ayue_agent.public_relationship_projection.matches_coll.find",
            return_value=matches,
        ) as find, patch(
            "services.ayue_agent.public_relationship_projection.profiles_coll.find_one",
            side_effect=[{"display_name": "小葵"}, {"display_name": "小葵"}],
        ):
            resolved = accepted_contact_ids_by_display_name("owner", " 小 葵 ")
        self.assertEqual(resolved, ["contact-a", "contact-b"])
        self.assertIn("$and", find.call_args.args[0])

    def test_display_name_resolver_fails_closed_when_relationship_read_fails(self):
        with patch(
            "services.ayue_agent.public_relationship_projection.matches_coll.find",
            side_effect=RuntimeError("database unavailable"),
        ):
            self.assertEqual(accepted_contact_ids_by_display_name("owner", "小葵"), [])

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

    def test_accepted_contact_list_is_bounded_and_public_only(self):
        matches = [
            {"from_user": "owner", "to_user": f"contact-{index}", "status": "accepted"}
            for index in range(10)
        ]
        with patch(
            "services.ayue_agent.public_relationship_projection.matches_coll.find", return_value=matches,
        ), patch(
            "services.ayue_agent.public_relationship_projection.safe_public_profile",
            return_value={"recent_context": "想吃飯", "initial_interest": "聊天", "personality_summary": "溫和"},
        ), patch(
            "services.ayue_agent.public_relationship_projection.display_name", side_effect=lambda item: item.replace("contact-", "小")
        ), patch(
            "services.ayue_agent.public_relationship_projection.safe_match_reason", return_value="公開理由",
        ), patch(
            "services.ayue_agent.public_relationship_projection.verified_common_ground", return_value=["共同點"],
        ):
            contacts, truncated = accepted_contact_summaries("owner")
        self.assertEqual(len(contacts), 8)
        self.assertTrue(truncated)
        self.assertNotIn("contact-", str(contacts))

    def test_accepted_contact_tool_returns_no_private_fields(self):
        with patch(
            "services.ayue_agent.tools.accepted_contact_summaries",
            return_value=([{"display_name": "小安", "recent_context": "想吃飯", "initial_interest": "聊天", "personality_summary": "溫和", "safe_match_reason": "公開理由", "verified_common_ground": [], "distinctive_tags": []}], False),
        ):
            result = execute_tool(ToolCall(name="relationship.list_accepted_contacts"), AgentTurnContext(
                user_id="owner", room_id="room", message="我可以約誰？",
            ))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["contacts"][0]["display_name"], "小安")
        self.assertNotIn("calendar", str(result.data))


if __name__ == "__main__":
    unittest.main()
