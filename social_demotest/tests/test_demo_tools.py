import unittest
from unittest.mock import patch

from routers.system import get_demo_status


class DemoToolsTests(unittest.TestCase):
    @patch("routers.system.web_enabled", return_value=True)
    @patch("routers.system.agent_mode_for_user", return_value="on")
    @patch("routers.system.profiles_coll")
    def test_status_is_minimal_and_reflects_v2_capabilities(
        self, profiles, _agent_mode, _web_enabled,
    ):
        profiles.find_one.return_value = {
            "current_context": "最近想去駁二看展",
            "profile_location": {"city": "高雄市", "district": "鹽埕區"},
            "match_search": {"status": "waiting_other", "request_id": "private"},
            "agentic_pending_confirmation": {"tool_name": "match.start_search"},
        }

        result = get_demo_status("owner")

        self.assertEqual(result["agent_version"], "v2")
        self.assertTrue(result["web_search_ready"])
        self.assertEqual(result["location"]["display_name"], "高雄市鹽埕區")
        self.assertEqual(result["recent_context"], "最近想去駁二看展")
        self.assertEqual(result["match_search_status"], "waiting_other")
        self.assertTrue(result["has_pending_confirmation"])
        self.assertNotIn("request_id", result)
        self.assertNotIn("agentic_pending_confirmation", result)

    @patch("routers.system.web_enabled", return_value=False)
    @patch("routers.system.agent_mode_for_user", return_value="off")
    @patch("routers.system.profiles_coll")
    def test_status_handles_missing_profile(
        self, profiles, _agent_mode, _web_enabled,
    ):
        profiles.find_one.return_value = None

        result = get_demo_status("owner")

        self.assertFalse(result["profile_exists"])
        self.assertEqual(result["agent_version"], "legacy")
        self.assertFalse(result["web_search_ready"])
        self.assertEqual(result["location"], {})
        self.assertEqual(result["recent_context"], "尚無近期情境")
        self.assertEqual(result["match_search_status"], "idle")
        self.assertFalse(result["has_pending_confirmation"])


if __name__ == "__main__":
    unittest.main()
