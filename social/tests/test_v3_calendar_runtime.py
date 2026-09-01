import inspect
import unittest

from services.ayue_agent.v3 import scheduler
from services.ayue_agent.v3.runtime_registry import RuntimeRegistration, TaskRunnerResult


class V3CalendarRuntimeBoundaryTests(unittest.TestCase):
    def test_scheduler_does_not_interpret_calendar_domain_shapes(self):
        source = inspect.getsource(scheduler)
        for forbidden in (
            "CalendarCommand",
            "CalendarAgentResult",
            "calendar_drafts",
            "calendar_references",
            "preflight_calendar_commands",
            "_calendar_reference_for_command",
        ):
            self.assertNotIn(forbidden, source)

    def test_runner_result_is_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            TaskRunnerResult()
        with self.assertRaises(ValueError):
            TaskRunnerResult(proposals=[], completed_results=[])
        proposals = TaskRunnerResult.from_proposals([])
        completed = TaskRunnerResult.from_completed([])
        self.assertIsNotNone(proposals.proposals)
        self.assertIsNotNone(completed.completed_results)

    def test_calendar_registration_owns_blocker_and_result_projection(self):
        registration = scheduler._SUB_AGENT_RUNNERS["calendar"]
        self.assertIsInstance(registration, RuntimeRegistration)
        self.assertIsNotNone(registration.direct_chat_blocker)
        self.assertIsNotNone(registration.confirmed_result_projector)


if __name__ == "__main__":
    unittest.main()
