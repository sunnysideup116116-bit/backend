import unittest

from services.ayue_agent.contracts import AgentTurnContext, ToolCall
from services.ayue_agent.tool_registry import (
    READ_ONLY_TOOLS,
    TOOL_REGISTRY,
    ToolRisk,
    executor_arguments_for_turn,
    tool_call_key,
)
from services.ayue_agent.tools import execute_tool


class AyueAgentRegistryTests(unittest.TestCase):
    def test_each_registered_read_tool_has_a_runtime_executor_key(self):
        executor_keys = {
            "calendar_events", "calendar_event_find", "current_time", "match_status",
            "counterparty_summary", "recent_context", "relationship_evidence", "mentioned_contact_summary", "accepted_contact_list", "memory_profile",
            "self_profile",
        }
        for tool_name in READ_ONLY_TOOLS:
            spec = TOOL_REGISTRY[tool_name]
            self.assertEqual(spec.risk, ToolRisk.READ)
            self.assertIn(spec.executor_key, executor_keys)
            self.assertIsNotNone(spec.executor_arguments_model)
            self.assertIsNotNone(spec.output_model)

    def test_write_tool_cannot_bypass_runtime_executor(self):
        result = execute_tool(
            ToolCall(name="match.start_search"),
            AgentTurnContext(user_id="owner", room_id="room", message="幫我找人"),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "tool_not_allowed")

    def test_duplicate_key_uses_executor_owned_arguments_not_planner_noise(self):
        spec = TOOL_REGISTRY["system.get_current_time"]
        arguments = executor_arguments_for_turn(spec, [])
        self.assertEqual(arguments, {})
        self.assertEqual(tool_call_key(spec, arguments), tool_call_key(spec, arguments))

    def test_proposal_decision_keeps_only_the_typed_planner_enum(self):
        spec = TOOL_REGISTRY["match.decide_active_proposal"]
        arguments = executor_arguments_for_turn(spec, [], {"decision": "interested"})
        self.assertEqual(arguments, {"decision": "interested"})
        with self.assertRaises(Exception):
            executor_arguments_for_turn(spec, [], {"decision": "interested", "proposal_id": "nope"})

    def test_calendar_find_accepts_an_omitted_or_null_date_hint(self):
        spec = TOOL_REGISTRY["calendar.find_my_event"]
        self.assertEqual(
            executor_arguments_for_turn(spec, [], {"event_hint": "看電影"}),
            {"event_hint": "看電影", "date_hint": None, "companion_hint": None},
        )
        self.assertEqual(
            executor_arguments_for_turn(spec, [], {"event_hint": "看電影", "date_hint": None}),
            {"event_hint": "看電影", "date_hint": None, "companion_hint": None},
        )


if __name__ == "__main__":
    unittest.main()
