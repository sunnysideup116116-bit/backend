import unittest
from types import SimpleNamespace
from unittest.mock import patch

from models import SettingsRequest
from routers import system


class ProactiveSettingsTests(unittest.TestCase):
    def test_boolean_setting_disables_candidates_and_keeps_legacy_projection(self):
        with patch.object(system.profiles_coll, "find_one", return_value={"user_id": "owner"}), \
             patch.object(system.profiles_coll, "update_one", return_value=SimpleNamespace(modified_count=1)) as update, \
             patch.object(system, "cancel_pending_delivery_slot") as cancel_delivery, \
             patch.object(system, "cancel_pending_followups", return_value=2) as cancel:
            result = system.update_settings(SettingsRequest(user_id="owner", proactive_care_enabled=False))
        self.assertFalse(result["proactive_care_enabled"])
        self.assertEqual(result["proactive_frequency"], "none")
        self.assertFalse(update.call_args.args[1]["$set"]["proactive_care_enabled"])
        cancel_delivery.assert_called_once_with("owner")
        cancel.assert_called_once_with("owner", reason="user_disabled")

    def test_legacy_unknown_frequency_does_not_opt_in(self):
        with patch.object(system.profiles_coll, "find_one", return_value={"user_id": "owner"}), \
             patch.object(system.profiles_coll, "update_one", return_value=SimpleNamespace(modified_count=1)), \
             patch.object(system, "cancel_pending_delivery_slot") as cancel_delivery, \
             patch.object(system, "cancel_pending_followups") as cancel:
            result = system.update_settings(SettingsRequest(user_id="owner", proactive_frequency="surprise"))
        self.assertFalse(result["proactive_care_enabled"])
        cancel_delivery.assert_called_once_with("owner")
        cancel.assert_called_once()

    def test_legacy_known_frequency_maps_to_enabled_boolean(self):
        with patch.object(system.profiles_coll, "find_one", return_value={"user_id": "owner"}), \
             patch.object(system.profiles_coll, "update_one", return_value=SimpleNamespace(modified_count=1)):
            result = system.update_settings(SettingsRequest(user_id="owner", proactive_frequency="86400"))
        self.assertTrue(result["proactive_care_enabled"])
        self.assertEqual(result["proactive_frequency"], "86400")


if __name__ == "__main__":
    unittest.main()
