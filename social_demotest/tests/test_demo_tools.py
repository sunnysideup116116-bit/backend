import unittest
from unittest.mock import patch

from routers.system import get_demo_status, get_recent_context_status


class DemoToolsTests(unittest.TestCase):
    @patch("routers.system.web_enabled", return_value=True)
    @patch("routers.system.agent_mode_for_user_v3", return_value="on")
    @patch("routers.system.profiles_coll")
    def test_status_is_minimal_and_reflects_v3_capabilities(
        self, profiles, _agent_mode, _web_enabled,
    ):
        profiles.find_one.return_value = {
            "current_context": "最近想去駁二看展",
            "profile_location": {"city": "高雄市", "district": "鹽埕區"},
            "match_search": {"status": "waiting_other", "request_id": "private"},
            "agentic_pending_confirmation": {"tool_name": "match.start_search"},
        }

        result = get_demo_status("owner")

        self.assertEqual(result["agent_version"], "v3")
        self.assertTrue(result["web_search_ready"])
        self.assertEqual(result["location"]["display_name"], "高雄市鹽埕區")
        self.assertEqual(result["recent_context"], "最近想去駁二看展")
        self.assertEqual(result["match_search_status"], "waiting_other")
        self.assertTrue(result["has_pending_confirmation"])
        self.assertNotIn("request_id", result)
        self.assertNotIn("agentic_pending_confirmation", result)

    @patch("routers.system.web_enabled", return_value=False)
    @patch("routers.system.agent_mode_for_user_v3", return_value="off")
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

    @patch("routers.system.profiles_coll")
    def test_recent_context_process_status_is_privacy_safe(self, profiles):
        profiles.find_one.return_value = {
            "current_context": "最近想去駁二看展",
            "current_context_revision": 4,
            "recent_context_updated_at": 123.0,
            "agentic_profile_process": {
                "run_key": "a" * 32,
                "state": "completed",
                "outcome": "updated",
                "raw_result": "seed_user_08 private",
            },
        }

        result = get_recent_context_status("owner", "a" * 32)

        self.assertEqual(result["process"], {"state": "completed", "outcome": "updated"})
        self.assertEqual(result["revision"], 4)
        self.assertNotIn("run_key", result)
        self.assertNotIn("seed_user_08", str(result))

    @patch("routers.system.profiles_coll")
    def test_recent_context_process_status_distinguishes_superseded_and_timeout(self, profiles):
        profiles.find_one.return_value = {
            "agentic_profile_process": {
                "run_key": "b" * 32, "state": "processing", "expires_at": 50.0,
            },
        }
        self.assertEqual(
            get_recent_context_status("owner", "a" * 32)["process"]["state"],
            "superseded",
        )
        with patch("routers.system.time.time", return_value=51.0):
            self.assertEqual(
                get_recent_context_status("owner", "b" * 32)["process"]["state"],
                "timeout",
            )

    @patch("routers.system.profiles_coll")
    def test_recent_context_legacy_poll_shape_stays_compatible(self, profiles):
        profiles.find_one.return_value = {
            "current_context": "最近去游泳",
            "current_context_revision": 2,
            "recent_context_updated_at": 10.0,
            "agentic_profile_process": {"run_key": "a" * 32, "state": "queued"},
        }
        result = get_recent_context_status("owner")
        self.assertEqual(set(result), {"current_context", "revision", "updated_at"})

    @patch("routers.system.profiles_coll")
    def test_recent_context_poll_tolerates_malformed_legacy_numbers(self, profiles):
        profiles.find_one.return_value = {
            "current_context_revision": "unknown",
            "recent_context_updated_at": {},
            "agentic_profile_process": {
                "run_key": "a" * 32, "state": "queued", "expires_at": "unknown",
            },
        }
        result = get_recent_context_status("owner", "a" * 32)
        self.assertEqual(result["revision"], 0)
        self.assertEqual(result["updated_at"], 0.0)
        self.assertEqual(result["process"]["state"], "queued")


if __name__ == "__main__":
    unittest.main()
