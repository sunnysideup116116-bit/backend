import unittest

from services.ayue_agent.v3.contracts import ToolProposal, GuardResultCode
from services.ayue_agent.v3.guard import guard_proposal


class V3GuardTests(unittest.TestCase):
    def test_passes_valid_read_tool(self):
        p = ToolProposal(tool_name="calendar.list_my_events", arguments={})
        d = guard_proposal(p, agent_name="calendar", seen_keys=set(), step_count=0, max_reads=3)
        self.assertTrue(d.ok)
        self.assertEqual(d.code, GuardResultCode.PASSED)

    def test_rejects_unknown_tool(self):
        p = ToolProposal(tool_name="nope.bad", arguments={})
        d = guard_proposal(p, agent_name="calendar", seen_keys=set(), step_count=0, max_reads=3)
        self.assertFalse(d.ok)
        self.assertEqual(d.code, GuardResultCode.TOOL_NOT_REGISTERED)

    def test_rejects_forbidden_arg_field(self):
        # ToolProposal's own validator should reject, but guard is a backstop.
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            ToolProposal(tool_name="calendar.submit_commands", arguments={"revision": 1})

    def test_rejects_schema_invalid_args(self):
        p = ToolProposal(
            tool_name="calendar.submit_commands",
            arguments={"title": "約會"},  # missing date, start_time, end_time
        )
        d = guard_proposal(p, agent_name="calendar", seen_keys=set(), step_count=0, max_reads=3)
        self.assertFalse(d.ok)
        self.assertEqual(d.code, GuardResultCode.SCHEMA_INVALID)

    def test_rejects_duplicate_call(self):
        from services.ayue_agent.tool_registry import TOOL_REGISTRY, tool_call_key
        spec = TOOL_REGISTRY["calendar.list_my_events"]
        key = tool_call_key(spec, {})
        p = ToolProposal(tool_name="calendar.list_my_events", arguments={})
        d = guard_proposal(p, agent_name="calendar", seen_keys={key}, step_count=0, max_reads=3)
        self.assertFalse(d.ok)
        self.assertEqual(d.code, GuardResultCode.DUPLICATE_CALL)

    def test_rejects_step_limit_exceeded(self):
        p = ToolProposal(tool_name="calendar.list_my_events", arguments={})
        d = guard_proposal(p, agent_name="calendar", seen_keys=set(), step_count=3, max_reads=3)
        self.assertFalse(d.ok)
        self.assertEqual(d.code, GuardResultCode.STEP_LIMIT_EXCEEDED)

    def test_write_tool_requires_confirmation(self):
        p = ToolProposal(
            tool_name="calendar.submit_commands",
            arguments={"commands": [{"action": "create", "title": "x", "date": "2026-08-09", "start_time": "20:00", "end_time": "22:00"}]},
        )
        d = guard_proposal(p, agent_name="calendar", seen_keys=set(), step_count=0, max_reads=3)
        self.assertFalse(d.ok)
        self.assertEqual(d.code, GuardResultCode.WRITE_REQUIRES_CONFIRMATION)


if __name__ == "__main__":
    unittest.main()
