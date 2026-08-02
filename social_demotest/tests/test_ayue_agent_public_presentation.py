import unittest
from unittest.mock import patch

from services.ayue_agent.contracts import AgentDecision, AgentIntent, AgentTurnContext, AgentTurnContextV2, DecisionKind, ToolCall
from services.ayue_agent.public_relationship_projection import safe_public_profile
from services.ayue_agent.router import _planner_prompt, planner_final_reply_v2
from services.ayue_agent.runtime import _public_place_cards
from services.ayue_agent.tools import execute_tool


class AyueAgentPublicPresentationTests(unittest.TestCase):
    def test_coarse_location_is_opt_in_and_never_exposes_address(self):
        profile = {
            "profile_location": {"city": "嘉義市", "district": "西區"},
            "address": "not public",
        }
        with patch(
            "services.ayue_agent.public_relationship_projection.profiles_coll.find_one", return_value=profile,
        ):
            self.assertNotIn("location", safe_public_profile("contact"))
            projected = safe_public_profile("contact", include_location=True)
        self.assertEqual(projected["location"], "嘉義市西區")
        self.assertNotIn("address", projected)

    def test_accepted_mention_projection_includes_only_coarse_location(self):
        match = {"from_user": "owner", "to_user": "contact", "status": "accepted"}
        profile = {
            "display_name": "小文",
            "profile_location": {"city": "嘉義市", "district": "西區"},
            "address": "not public",
        }
        with patch(
            "services.ayue_agent.public_relationship_projection.matches_coll.find_one", return_value=match,
        ), patch(
            "services.ayue_agent.public_relationship_projection.profiles_coll.find_one", return_value=profile,
        ):
            result = execute_tool(
                ToolCall(name="relationship.get_mentioned_contact_summary", arguments={"other_ids": ["contact"]}),
                AgentTurnContext(user_id="owner", room_id="room", message="@小文住哪裡？"),
            )
        contact = result.data["contacts"][0]
        self.assertEqual(contact["location"], "嘉義市西區")
        self.assertNotIn("address", str(contact))

    def test_pending_counterparty_projection_does_not_include_location(self):
        match = {"from_user": "owner", "to_user": "contact", "status": "pending"}
        profile = {
            "display_name": "小文",
            "profile_location": {"city": "嘉義市", "district": "西區"},
        }
        with patch(
            "services.ayue_agent.tools.get_counterparty_match_source", return_value={"match": match},
        ), patch(
            "services.ayue_agent.public_relationship_projection.profiles_coll.find_one", return_value=profile,
        ):
            result = execute_tool(
                ToolCall(name="match.get_counterparty_summary"),
                AgentTurnContext(user_id="owner", room_id="room", message="對方住哪裡？"),
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["location"], "")

    def test_external_observation_allows_detailed_planner_reply(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="幫我查活動")
        decision = AgentDecision(
            kind=DecisionKind.FINAL,
            intent=AgentIntent.WEB,
            reply="第一點。第二點。第三點。第四點。第五點。第六點。",
        )
        reply = planner_final_reply_v2(ctx, decision, [{"tool": "web.search", "result": {}}])
        self.assertIsNotNone(reply)
        self.assertIn("第五點", reply)
        self.assertNotIn("第六點", reply)

    def test_planner_prompt_has_no_conflicting_external_reply_limit(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="幫我查活動")
        prompt = _planner_prompt(ctx, frozenset(), [{"tool": "web.search", "result": {}}])
        self.assertIn("最多 5 句、約 240 個中文字", prompt)
        self.assertIn("一般聊天以繁體中文、最多 2 句", prompt)

    def test_place_cards_only_render_places_named_in_the_reply(self):
        observations = [{
            "tool": "places.search_nearby",
            "result": {"places": [
                {"provider": "google", "place_id": "place_alpha", "name": "Alpha Cafe"},
                {"provider": "google", "place_id": "place_beta", "name": "Beta Bar"},
                {"provider": "google", "place_id": "place_gamma", "name": "Gamma Park"},
            ]},
        }]
        cards = _public_place_cards(observations, reply="我會選 Beta Bar，再考慮 Alpha Cafe。")
        self.assertEqual([card["name"] for card in cards], ["Beta Bar", "Alpha Cafe"])

    def test_place_cards_are_empty_when_reply_does_not_name_a_place(self):
        observations = [{
            "tool": "places.search_nearby",
            "result": {"places": [
                {"provider": "google", "place_id": "place_alpha", "name": "Alpha Cafe"},
                {"provider": "google", "place_id": "place_beta", "name": "Beta Bar"},
            ]},
        }]
        self.assertEqual(_public_place_cards(observations, reply="我找到幾個選項。"), [])

    def test_unmentioned_duplicate_does_not_hide_the_named_card(self):
        observations = [{
            "tool": "places.search_nearby",
            "result": {"places": [
                {"provider": "google", "place_id": "same_place", "name": "Old Alias"},
                {"provider": "google", "place_id": "same_place", "name": "Current Name"},
            ]},
        }]
        cards = _public_place_cards(observations, reply="我推薦 Current Name。")
        self.assertEqual([card["name"] for card in cards], ["Current Name"])

    def test_runtime_card_projection_rejects_missing_safe_name(self):
        observations = [{
            "tool": "places.resolve_place",
            "result": {"place": {"provider": "google", "place_id": "unnamed_place"}},
        }]
        self.assertEqual(_public_place_cards(observations, reply="我找到一個地點。"), [])


if __name__ == "__main__":
    unittest.main()
