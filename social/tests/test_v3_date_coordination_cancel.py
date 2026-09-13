import os
import time
import unittest
from unittest.mock import MagicMock, patch

from services.ayue_agent.contracts import AgentTurnContext, PublicAgentTurnContext, TurnClockV1
from services.ayue_agent.public_relationship_projection import ContactNameResolution
from services.ayue_agent.v3.contracts import (
    DATE_COORDINATION_CANCEL_WRITE_INTENT,
    Plan,
    SubTask,
    ToolProposal,
)
from services.ayue_agent.v3.date_coordination_references import (
    authority_projection,
    clear_runtime_state,
    get_reference,
    public_projection,
    remember_date_coordination,
)
from services.ayue_agent.v3.planner import PlannerMetrics, plan_turn
from services.ayue_agent.v3.scheduler import run_public_agent_turn_v3
from services.ayue_agent.v3.sub_agents.base import SubAgentMetrics
from services.ayue_agent.v3.synthesizer import SynthesizerMetrics
from services.ayue_agent.v3.test_store import MemoryCollection
from services.ayue_agent.v3.write_executors import (
    execute_write,
    prepare_write_confirmation,
)
from services.ai_service import ToolCallResult


def _clock():
    return TurnClockV1(
        timezone="Asia/Taipei",
        utc_iso="2026-09-06T12:00:00+00:00",
        local_iso="2026-09-06T20:00:00+08:00",
        local_date="2026-09-06",
        local_time="20:00",
        weekday_zh_tw="星期日",
    )


def _match(status="pending_partner", revision=2):
    return {
        "_id": "match-1",
        "from_user": "owner",
        "to_user": "other",
        "status": "accepted",
        "date_coordination": {
            "coordination_id": "coord-1",
            "status": status,
            "revision": revision,
            "form": {"activity": "喝咖啡"},
        },
    }


class DateCoordinationCancelTests(unittest.TestCase):
    def tearDown(self):
        clear_runtime_state()

    def test_recent_action_reference_is_owner_and_room_scoped(self):
        match = _match()
        with patch.dict(os.environ, {"AYUE_TEST_MODE": "on"}), patch(
            "services.ayue_agent.v3.date_coordination_references.matches_coll.find_one",
            return_value=match,
        ):
            remember_date_coordination(
                "owner",
                "source-room",
                match,
                match["date_coordination"],
                other_id="other",
                safe_label="小宇",
            )

            self.assertIsNotNone(get_reference("owner", "source-room"))
            self.assertIsNone(get_reference("owner", "different-room"))
            self.assertIsNone(get_reference("another-owner", "source-room"))

    def test_recent_action_reference_expires_without_reading_canonical_state(self):
        match = _match()
        with patch.dict(os.environ, {"AYUE_TEST_MODE": "on"}), patch(
            "services.ayue_agent.v3.date_coordination_references.time.time",
            return_value=100.0,
        ):
            remember_date_coordination(
                "owner",
                "room",
                match,
                match["date_coordination"],
                other_id="other",
            )

        with patch.dict(os.environ, {"AYUE_TEST_MODE": "on"}), patch(
            "services.ayue_agent.v3.date_coordination_references.time.time",
            return_value=1901.0,
        ), patch(
            "services.ayue_agent.v3.date_coordination_references.matches_coll.find_one",
        ) as canonical_read:
            self.assertIsNone(get_reference("owner", "room"))

        canonical_read.assert_not_called()

    def test_public_projection_keeps_authority_private(self):
        record = {
            "match_id": "match-1",
            "coordination_id": "coord-1",
            "other_id": "other",
            "safe_label": "小宇",
            "status": "pending_partner",
            "revision": 2,
            "created_at": 10,
            "expires_at": time.time() + 120,
        }
        public = public_projection(record)
        private = authority_projection(record)
        self.assertEqual(public["kind"], "date_coordination")
        self.assertEqual(public["display_name"], "小宇")
        self.assertEqual(public["allowed_actions"], ["cancel"])
        self.assertNotIn("coordination_id", public)
        self.assertEqual(private["expected_status"], "pending_partner")
        self.assertEqual(private["expected_revision"], 2)

    def test_cancel_plan_uses_relationship_only(self):
        plan = Plan(
            write_intent=DATE_COORDINATION_CANCEL_WRITE_INTENT,
            tasks=[
                SubTask(id="r1", agent="relationship", task_brief="取消約會卡"),
                SubTask(id="s1", agent="synthesizer", depends_on=["r1"], task_brief="呈現確認"),
            ],
        )
        self.assertEqual([task.agent for task in plan.tasks], ["relationship", "synthesizer"])

    def test_cancel_preflight_resolves_public_name_and_preview(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="幫我取消小宇的約會卡")
        turn = MagicMock(
            _mentioned_ids=[],
            mentioned_contact_overflow=False,
            _focused_match_authority=None,
        )
        match = _match()
        with patch(
            "services.ayue_agent.v3.write_executors.resolve_accepted_contact_name",
            return_value=ContactNameResolution(
                "resolved_exact", other_id="other", display_name="小宇", kind="exact",
            ),
        ), patch(
            "services.ayue_agent.v3.write_executors._date_coordination_candidates",
            return_value=[match],
        ):
            payload, preview = prepare_write_confirmation(
                "relationship.cancel_date_coordination",
                {"target_source": "name", "target_evidence_span": "小宇"},
                ctx,
                turn,
            )
        self.assertEqual(payload["data"]["coordination_id"], "coord-1")
        self.assertEqual(payload["data"]["expected_status"], "pending_partner")
        self.assertEqual(payload["data"]["expected_revision"], 2)
        self.assertEqual(preview, "要撤回剛傳給小宇的約會邀請嗎？")

    def test_cancel_capability_question_is_read_only(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="約會卡可以取消嗎")
        turn = MagicMock(
            _mentioned_ids=[],
            mentioned_contact_overflow=False,
            _focused_match_authority=None,
        )
        with patch(
            "services.ayue_agent.v3.write_executors._date_coordination_candidates",
        ) as candidates:
            payload, reply = prepare_write_confirmation(
                "relationship.cancel_date_coordination",
                {"target_source": "summary_singleton"},
                ctx,
                turn,
            )
        self.assertIsNone(payload)
        self.assertIn("雙人聊天室", reply)
        candidates.assert_not_called()

    def test_cancel_executor_passes_status_and_revision_cas(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="確認")
        payload = {
            "match_id": "match-1",
            "coordination_id": "coord-1",
            "other_id": "other",
            "expected_status": "active",
            "expected_revision": 4,
            "expected_coordination_revision": 4,
            "expected_event_revision": 9,
            "calendar_event_id": "event-1",
            "safe_label": "小宇",
        }
        with patch(
            "services.date_coordination_service.find_accepted_match",
            return_value=_match(status="active", revision=4),
        ), patch(
            "services.date_coordination_service.cancel_coordination_or_event",
            return_value={"status": "cancelled"},
        ) as cancel, patch(
            "services.ayue_agent.v3.write_executors.TOOL_CALLS.find_one_and_update",
            return_value=None,
        ), patch(
            "services.ayue_agent.v3.write_executors.TOOL_CALLS.update_one",
        ):
            ok, reply, code = execute_write(
                "relationship.cancel_date_coordination",
                {},
                ctx,
                MagicMock(),
                "run-1",
                0,
                confirmation_id="choice-1",
                payload=payload,
            )
        self.assertTrue(ok)
        self.assertIsNone(code)
        self.assertIn("共同約會已取消", reply)
        self.assertEqual(cancel.call_args.kwargs["expected_status"], "active")
        self.assertEqual(cancel.call_args.kwargs["expected_coordination_revision"], 4)
        self.assertEqual(cancel.call_args.kwargs["expected_revision"], 9)

    def test_scheduler_creates_date_cancel_confirmation_without_match_routing(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="幫我取消約會卡")
        turn = PublicAgentTurnContext(
            user_id="owner",
            room_id="room",
            message=ctx.message,
            recent_action_reference={
                "kind": "date_coordination",
                "display_name": "小宇",
                "status": "pending_partner",
                "allowed_actions": ["cancel"],
            },
            clock=_clock(),
        )
        plan = Plan(
            write_intent=DATE_COORDINATION_CANCEL_WRITE_INTENT,
            tasks=[
                SubTask(id="r1", agent="relationship", task_brief="取消最近的約會卡"),
                SubTask(
                    id="s1",
                    agent="synthesizer",
                    depends_on=["r1"],
                    task_brief="呈現取消確認",
                ),
            ],
        )
        preview = "要撤回剛傳給小宇的約會邀請嗎？"
        confirmations = MemoryCollection()
        with patch(
            "services.ayue_agent.v3.scheduler.plan_turn",
            return_value=(plan, PlannerMetrics()),
        ), patch(
            "services.ayue_agent.v3.scheduler.build_public_agent_turn_context",
            return_value=turn,
        ), patch(
            "services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS",
            {
                "relationship": MagicMock(return_value=(
                    [ToolProposal(
                        tool_name="relationship.cancel_date_coordination",
                        arguments={"target_source": "recent_action"},
                    )],
                    SubAgentMetrics(),
                )),
            },
        ), patch(
            "services.ayue_agent.v3.scheduler.prepare_write_confirmation",
            return_value=(
                {
                    "action": "relationship.cancel_date_coordination",
                    "arguments": {},
                    "data": {
                        "match_id": "match-1",
                        "coordination_id": "coord-1",
                        "other_id": "other",
                        "expected_status": "pending_partner",
                        "expected_coordination_revision": 2,
                    },
                },
                preview,
            ),
        ), patch(
            "services.ayue_agent.v3.scheduler._CONFIRMATIONS",
            confirmations,
        ), patch(
            "services.ayue_agent.v3.synthesizer.synthesize",
            return_value=(
                "模型不應改寫確認。",
                None,
                SynthesizerMetrics(),
            ),
        ):
            result = run_public_agent_turn_v3(ctx)

        self.assertEqual(result.reply, preview)
        self.assertIsNotNone(result.choice_prompt)
        self.assertEqual(result.choice_prompt["confirm_label"], "確認取消")
        record = confirmations.find({})[0]
        self.assertEqual(record["tool_name"], "relationship.cancel_date_coordination")
        self.assertEqual(record["room_id"], "room")

    def test_planner_does_not_rewrite_match_semantics_from_keywords(self):
        turn = PublicAgentTurnContext(
            user_id="owner",
            room_id="room",
            message="配對那邊我不懂",
            clock=_clock(),
        )
        args = {
            "mode": "tasks",
            "write_intent": "none",
            "tasks": [
                {"id": "m", "agent": "match", "match_intent": "status", "task_brief": "說明配對卡"},
                {"id": "s", "agent": "synthesizer", "depends_on": ["m"], "task_brief": "回覆"},
            ],
        }
        result = ToolCallResult(
            content="",
            tool_calls=[{"name": "decompose_tasks", "arguments": args}],
        )
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=result,
        ):
            plan, metrics = plan_turn(turn)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.tasks[0].match_intent, "status")
        self.assertEqual(metrics.failure_code, "")


if __name__ == "__main__":
    unittest.main()
