import os
import unittest
from unittest.mock import patch

from services.ayue_agent.v3.calendar_references import (
    clear_runtime_state,
    recent_mutation_projection,
    remember_recent_mutation,
    verify_recent_mutation,
)
from services.ayue_agent.contracts import AgentTurnContext, ToolCall
from services.ayue_agent.tool_registry import ToolRisk, get_tool_spec
from services.ayue_agent.tools import execute_tool


class CalendarMutationVerificationTests(unittest.TestCase):
    def setUp(self):
        self._previous_test_mode = os.environ.get("AYUE_TEST_MODE")
        os.environ["AYUE_TEST_MODE"] = "on"
        clear_runtime_state()

    def tearDown(self):
        clear_runtime_state()
        if self._previous_test_mode is None:
            os.environ.pop("AYUE_TEST_MODE", None)
        else:
            os.environ["AYUE_TEST_MODE"] = self._previous_test_mode

    def test_successful_cancel_verifies_authoritative_cancelled_state(self):
        remember_recent_mutation(
            "owner", action="cancel", outcome="success", operations=[{
                "action": "cancel", "event_id": "event-1", "revision": 4,
                "source_type": "personal", "expected_status": "cancelled",
                "safe_label": "8/12 15:00–16:00 看牙醫",
            }],
        )
        with patch("services.calendar_service.get_owned_event_by_id", return_value={"status": "cancelled"}):
            verification = verify_recent_mutation("owner")
        self.assertEqual(verification["status"], "verified_success")
        projection = recent_mutation_projection(
            {"action": "cancel", "outcome": "success", "operations": [{
                "event_id": "secret", "revision": 9, "safe_label": "看牙醫",
            }]},
        )
        self.assertNotIn("event_id", projection)
        self.assertNotIn("revision", projection)

    def test_failed_mutation_is_reported_without_retrieving_cancelled_events(self):
        remember_recent_mutation(
            "owner", action="cancel", outcome="failed", operations=[{
                "action": "cancel", "event_id": "event-1", "source_type": "personal",
                "safe_label": "看牙醫",
            }],
        )
        with patch("services.calendar_service.get_owned_event_by_id") as lookup:
            verification = verify_recent_mutation("owner")
        lookup.assert_not_called()
        self.assertEqual(verification["status"], "failed")

    def test_verification_is_a_read_tool_without_destructive_arguments(self):
        spec = get_tool_spec("calendar.verify_recent_mutation")
        self.assertIsNotNone(spec)
        self.assertIs(spec.risk, ToolRisk.READ)
        with patch(
            "services.ayue_agent.v3.calendar_references.verify_recent_mutation",
            return_value={
                "status": "verified_success", "action": "cancel",
                "label": "看牙醫", "outcome": "success",
            },
        ):
            result = execute_tool(
                ToolCall(name="calendar.verify_recent_mutation", arguments={}),
                AgentTurnContext(user_id="owner", room_id="room", message="剛才有成功嗎？"),
            )
        self.assertTrue(result.ok)
        self.assertIn("calendar_mutation_verification", result.data)


if __name__ == "__main__":
    unittest.main()
