import os
import unittest

from services.ayue_agent.private_v2 import PrivateAgentTurnContextV2, _plan


def _context(message: str) -> PrivateAgentTurnContextV2:
    return PrivateAgentTurnContextV2(
        user_id="owner",
        other_id="other",
        room_id="private",
        message=message,
        pair_revision=1,
        viewer_profile={},
        counterparty_shareable={"display_name": "對方"},
        counterparty_advisory={},
        shared_history=[],
        private_history=[],
        shared_facts=[],
        local_time="2026-08-09 12:00",
    )


@unittest.skipUnless(
    os.getenv("AYUE_RUN_PRIVATE_SCOPE_LIVE") == "1",
    "provider-backed Private scope evaluation is opt-in",
)
class PrivateScopeLiveTests(unittest.TestCase):
    def test_semantic_paraphrases_route_by_goal_not_prompt_vocabulary(self):
        cases = (
            ("幫我看看明天台積電會不會漲", "redirect", None),
            ("她好像很喜歡日料，我約她去吃無菜單料理會不會太正式？", "stay", None),
            ("幫我查一下附近咖啡廳", "redirect", None),
            ("我們第一次單獨出去，咖啡廳會不會比餐廳自然？", "stay", None),
            ("下禮拜我哪天沒事？", "redirect", None),
            ("下禮拜我哪天比較適合找她出去？", "viewer", "private.calendar.get_viewer_availability"),
        )
        for message, expected, expected_tool in cases:
            with self.subTest(message=message):
                decision = _plan(_context(message), [])
                self.assertIsNotNone(decision)
                if expected == "redirect":
                    self.assertEqual(decision.kind, "redirect")
                    self.assertEqual(decision.intent, "out_of_scope")
                    self.assertEqual(decision.redirect_target, "public_ayue")
                elif expected == "viewer":
                    self.assertEqual(decision.kind, "tool_call")
                    self.assertEqual(decision.tool_name, expected_tool)
                else:
                    self.assertNotEqual(decision.kind, "redirect")


if __name__ == "__main__":
    unittest.main()
