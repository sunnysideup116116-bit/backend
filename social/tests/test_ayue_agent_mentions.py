import unittest
from unittest.mock import patch

from services.ayue_agent.contracts import AgentTurnContext, PublicAgentTurnContext, ToolCall
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
        self.assertEqual(result.data["total_count"], 1)
        self.assertNotIn("calendar", str(result.data))

    def test_my_relationship_views_returns_owner_facets_without_evidence(self):
        ctx = AgentTurnContext(
            user_id="owner",
            room_id="room",
            message="@小安，我之前怎麼看他？",
            mentioned_ids=["contact-a"],
        )
        rows = [{
            "topic": "相處感受",
            "category": "impression",
            "facets": [
                {"facet_id": "private-id", "text": "我覺得對方聊天偏冷淡",
                 "updated_at": 100},
                {"facet_id": "private-id-2", "text": "我覺得對方外表很帥",
                 "updated_at": 110},
            ],
        }]
        with patch(
            "services.ayue_agent.tools.validated_mentioned_contact_ids",
            return_value=(["contact-a"], False),
        ), patch(
            "services.ayue_agent.tools._display_name", return_value="小安",
        ), patch(
            "services.relationship_memory_service.relationship_memory_access_allowed",
            return_value=True,
        ), patch(
            "services.relationship_memory_service.list_relationship_memories",
            return_value=rows,
        ):
            result = execute_tool(
                ToolCall(
                    name="relationship.get_my_views",
                    arguments={
                        "target_source": "mention",
                        "target_evidence_span": "",
                        "contact_refs": [],
                    },
                ),
                ctx,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "ok")
        self.assertEqual(
            [item["text"] for item in result.data["contacts"][0]["groups"][0]["views"]],
            ["我覺得對方聊天偏冷淡", "我覺得對方外表很帥"],
        )
        self.assertNotIn("facet_id", str(result.data))
        self.assertNotIn("evidence", str(result.data))

    def test_my_relationship_views_fails_closed_for_blocked_pair(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room", message="@小安我以前怎麼看他",
            mentioned_ids=["contact-a"],
        )
        with patch(
            "services.ayue_agent.tools.validated_mentioned_contact_ids",
            return_value=(["contact-a"], False),
        ), patch(
            "services.relationship_memory_service.relationship_memory_access_allowed",
            return_value=False,
        ), patch(
            "services.relationship_memory_service.list_relationship_memories",
        ) as memories:
            result = execute_tool(
                ToolCall(
                    name="relationship.get_my_views",
                    arguments={
                        "target_source": "mention",
                        "target_evidence_span": "",
                        "contact_refs": [],
                    },
                ),
                ctx,
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "relationship_not_accepted")
        memories.assert_not_called()


if __name__ == "__main__":
    unittest.main()
