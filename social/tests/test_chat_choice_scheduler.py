import unittest
from unittest.mock import MagicMock, patch

from services.ayue_agent.contracts import AgentTurnContext
from services.ayue_agent.v3.contracts import Plan, SubTask
from services.ayue_agent.v3.contracts import ToolProposal
from services.ayue_agent.v3.test_store import MemoryCollection
from services.ayue_agent.v3.scheduler import run_public_agent_turn_v3
from services.ayue_agent.v3.sub_agents.base import SubAgentMetrics
from services.ayue_agent.v3.synthesizer import SynthesizerMetrics
from services.ayue_agent.v3.planner import PlannerMetrics


def _planner_metrics():
    return PlannerMetrics()


def _synth_metrics():
    return SynthesizerMetrics(
        presentation_messages=["一般回答"],
        presentation_class="conversation",
    )


class ChatChoiceSchedulerTests(unittest.TestCase):
    def test_conversational_write_returns_button_prompt_without_typing_instruction(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="幫我找人")
        turn = MagicMock(
            user_id="owner", room_id="room",
            calendar_draft=None, message=ctx.message, recent_messages=[],
            active_proposal=None, active_event_invitation=None,
            recent_context_draft=None, place_reference_resolution=None,
            mentioned_contact_overflow=False,
        )
        plan = Plan(tasks=[
            SubTask(id="m1", agent="match", depends_on=[], task_brief="開始搜尋"),
            SubTask(id="s1", agent="synthesizer", depends_on=["m1"], task_brief="呈現確認"),
        ])
        store = MemoryCollection()
        preview = "要我現在開始找就回覆「確認」；也可以先補充條件。"
        metrics = SynthesizerMetrics(
            presentation_messages=[preview], presentation_class="transaction",
        )
        with patch(
            "services.ayue_agent.v3.scheduler._CONFIRMATIONS", store,
        ), patch(
            "services.ayue_agent.v3.scheduler.build_public_agent_turn_context",
            return_value=turn,
        ), patch(
            "services.ayue_agent.v3.scheduler.plan_turn",
            return_value=(plan, _planner_metrics()),
        ), patch(
            "services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS",
            {"match": MagicMock(return_value=(
                [ToolProposal(tool_name="match.start_search", arguments={})],
                SubAgentMetrics(),
            ))},
        ), patch(
            "services.ayue_agent.v3.scheduler.prepare_write_confirmation",
            return_value=({
                "action": "match.start_search", "arguments": {}, "data": {},
            }, preview),
        ), patch(
            "services.ayue_agent.v3.scheduler.synthesizer.synthesize",
            return_value=(preview, None, metrics),
        ):
            result = run_public_agent_turn_v3(ctx)

        self.assertEqual(result.choice_prompt["state"], "pending")
        self.assertNotIn("回覆「確認」", result.reply)
        self.assertIn("要我現在開始找嗎", result.reply)

    def test_typed_confirmation_auto_cancels_button_choice_and_reaches_planner(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="確認")
        resolution = {
            "id": "choice-1", "state": "auto_cancelled",
            "selected": "cancel", "expires_at": 1e18,
        }
        turn = MagicMock(
            calendar_draft=None, message="確認", recent_messages=[],
            active_proposal=None, active_event_invitation=None,
            recent_context_draft=None, place_reference_resolution=None,
            mentioned_contact_overflow=False,
        )
        plan = Plan(tasks=[
            SubTask(id="s1", agent="synthesizer", depends_on=[], task_brief="正常回答"),
        ])
        with patch(
            "services.ayue_agent.v3.scheduler.ConfirmationManager.resolve_for_continuation",
            return_value=resolution,
        ), patch(
            "services.ayue_agent.v3.scheduler.awaiting_assessment_commit",
            return_value=None,
        ), patch(
            "services.ayue_agent.v3.scheduler.build_public_agent_turn_context",
            return_value=turn,
        ), patch(
            "services.ayue_agent.v3.scheduler.plan_turn",
            return_value=(plan, _planner_metrics()),
        ) as planner, patch(
            "services.ayue_agent.v3.scheduler.synthesizer.synthesize",
            return_value=("你想確認哪一件事？", None, _synth_metrics()),
        ), patch(
            "services.ayue_agent.v3.scheduler.execute_write",
        ) as execute_write:
            result = run_public_agent_turn_v3(ctx)

        self.assertEqual(result.choice_resolution["state"], "auto_cancelled")
        self.assertEqual(result.reply, "一般回答")
        planner.assert_called_once()
        execute_write.assert_not_called()

    def test_card_owned_legacy_confirmation_still_uses_text_token(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="確認")
        pending = {"_id": "legacy", "tool_name": "match.decide_active_proposal"}
        turn = MagicMock(calendar_draft=None)
        results = [{"ok": True, "data": {"reply": "已接受提案"}}]
        with patch(
            "services.ayue_agent.v3.scheduler.ConfirmationManager.resolve_for_continuation",
            return_value=None,
        ), patch(
            "services.ayue_agent.v3.scheduler.awaiting_assessment_commit",
            return_value=None,
        ), patch(
            "services.ayue_agent.v3.scheduler.build_public_agent_turn_context",
            return_value=turn,
        ), patch(
            "services.ayue_agent.v3.scheduler.ConfirmationManager.list_active",
            return_value=[pending],
        ), patch(
            "services.ayue_agent.v3.scheduler.ConfirmationManager.execute_confirmed",
            return_value=results,
        ) as execute_confirmed, patch(
            "services.ayue_agent.v3.scheduler.synthesizer.synthesize",
            return_value=("已接受提案", None, SynthesizerMetrics(
                presentation_messages=["已接受提案"],
                presentation_class="transaction",
            )),
        ):
            result = run_public_agent_turn_v3(ctx)

        self.assertEqual(result.reply, "已接受提案")
        self.assertIsNone(result.choice_prompt)
        self.assertEqual(
            execute_confirmed.call_args.kwargs["interaction_mode"],
            "legacy_text",
        )


if __name__ == "__main__":
    unittest.main()
