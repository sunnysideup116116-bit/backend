import json
import unittest
from pathlib import Path
from unittest.mock import patch

from services.ayue_agent.contracts import (
    AgentDecision,
    AgentTurnContext,
    AgentTurnContextV2,
    ToolResult,
)
from services.ayue_agent.runtime import _save_trace, run_public_agent_turn


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ayue_public_trajectories.json"
TRAJECTORIES = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class AyueAgentTrajectoryTests(unittest.TestCase):
    def test_deidentified_trajectories_replay_without_mongo_or_model(self):
        for fixture in TRAJECTORIES:
            with self.subTest(fixture=fixture["id"]):
                ctx = AgentTurnContext(
                    user_id="fixture_owner",
                    room_id="fixture_public_room",
                    message=fixture["message"],
                )
                turn = AgentTurnContextV2(
                    user_id=ctx.user_id,
                    room_id=ctx.room_id,
                    message=ctx.message,
                )
                decisions = [AgentDecision.model_validate(item) for item in fixture["decisions"]]
                tool_results = [ToolResult.model_validate(item) for item in fixture["tool_results"]]
                events: list[dict] = []
                captured_trace: dict = {}

                def capture_trace(_run_id, _ctx, trace):
                    captured_trace.update(trace)

                with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
                     patch("services.ayue_agent.runtime.plan_turn_v2", side_effect=decisions), \
                     patch("services.ayue_agent.runtime.execute_tool", side_effect=tool_results) as execute_tool, \
                     patch("services.ayue_agent.runtime.generate_final_reply_v2", return_value=fixture["reply"]), \
                     patch("services.ayue_agent.runtime._save_trace", side_effect=capture_trace):
                    result = run_public_agent_turn(ctx, mode="on", on_progress=events.append)

                self.assertEqual(result.reply, fixture["reply"])
                self.assertEqual(
                    [call.args[0].name for call in execute_tool.call_args_list],
                    fixture["expected_tools"],
                )
                self.assertEqual(
                    [event["type"] for event in events],
                    fixture["expected_events"],
                )
                self.assertEqual(captured_trace["event_sequence"], fixture["expected_events"] + ["final"])
                self.assertEqual(captured_trace["composer_outcome"]["reason"], fixture["expected_composer_reason"])
                self.assertEqual(captured_trace["tool_cache_hits"], fixture.get("expected_cache_hits", []))

    def test_trace_persistence_allowlist_discards_prompt_and_tool_contents(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="私密原句")
        payload = {
            "context_version": "v2",
            "visible_tools": ["calendar.list_my_events"],
            "planner_decisions": [{"kind": "tool_call", "tool_name": "calendar.list_my_events", "confidence": 0.97, "arguments": {"user_id": "seed_user_08"}}],
            "guard_results": ["allowed"],
            "tool_results": [{"tool": "calendar.list_my_events", "ok": True, "code": None, "data": {"activity": "private event"}}],
            "event_sequence": ["run_started", "tool_started", "tool_finished"],
            "tool_cache_hits": [],
            "composer_outcome": {
                "reason": "planner_final", "observation_count": 1,
                "result_code": "llm_reply", "reply": "private reply",
            },
            "public_progress_result_codes": [
                "run_started:emitted", "tool_started:emitted", "tool_finished:emitted",
            ],
            "prompt": "private full prompt",
            "observations": [{"result": {"revision": 9}}],
            "latency_ms": 12,
            "result": {"handled": True, "conversation_intent": "calendar", "fallback_reason": None},
        }
        stored: dict = {}
        with patch("services.ayue_agent.runtime.RUNS.insert_one", side_effect=lambda document: stored.update(document)):
            _save_trace("trace-1", ctx, payload)
        serialized = json.dumps(stored, ensure_ascii=False)
        for forbidden in ("private full prompt", "private event", "private reply", "seed_user_08", "revision", "observations", "私密原句"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(stored["event_sequence"], ["run_started", "tool_started", "tool_finished"])
        self.assertEqual(stored["public_progress_result_codes"], [
            "run_started:emitted", "tool_started:emitted", "tool_finished:emitted",
        ])
        self.assertEqual(stored["composer_outcome"], {
            "reason": "planner_final", "observation_count": 1, "result_code": "llm_reply",
        })

    def test_malformed_trace_metadata_never_breaks_the_agent_turn(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="你好")
        with patch("services.ayue_agent.runtime.RUNS.insert_one") as insert, \
             patch("builtins.print"):
            _save_trace("trace-malformed", ctx, {
                "planner_decisions": [{"confidence": {"not": "numeric"}}],
                "composer_outcome": {"observation_count": {"not": "numeric"}},
            })
        insert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
