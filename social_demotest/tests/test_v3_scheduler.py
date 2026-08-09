# social_demotest/tests/test_v3_scheduler.py
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from services.ayue_agent.contracts import AgentTurnContext, AgentResult, PublicAgentTurnContext, TurnClockV1
from services.ayue_agent.v3.calendar_commands import CalendarCommand
from services.ayue_agent.v3.calendar_drafts import clear_draft, get_draft, public_projection
from services.ayue_agent.v3.sub_agents.calendar_agent import CalendarAgentResult, run as run_calendar_agent
from services.ayue_agent.v3.contracts import Plan, SubTask, SubTaskResult, SubTaskStatus, ToolProposal
from services.ayue_agent.v3.planner import PlannerMetrics
from services.ayue_agent.v3.confirmation import ConfirmationManager
from services.ayue_agent.v3.synthesizer import SynthesizerMetrics
from services.ayue_agent.v3.sub_agents.base import SubAgentMetrics
from services.ai_service import ToolCallResult
from services.ayue_agent.v3.scheduler import (
    _apply_card_decision, _assessment_start_confirmation_requested,
    _direct_chat_block_reason, _prior_observations_for, _public_place_cards,
    run_public_agent_turn_v3,
)


def _sub_metrics():
    return SubAgentMetrics(input_tokens=10, output_tokens=20, duration_ms=100)


def _proposal(tool, args=None):
    return [ToolProposal(tool_name=tool, arguments=args or {})]


def _planner_metrics():
    return PlannerMetrics(input_tokens=50, output_tokens=60, duration_ms=200)


def _synth_metrics():
    return SynthesizerMetrics(input_tokens=30, output_tokens=40, duration_ms=150)


class V3SchedulerTests(unittest.TestCase):
    def _ctx(self, message="幫我看看行程和附近餐廳"):
        return AgentTurnContext(user_id="owner", room_id="room", message=message)

    def _direct_turn(self, message="哈囉", **updates):
        turn = PublicAgentTurnContext(
            user_id="owner", room_id="room", message=message,
            clock=TurnClockV1(
                timezone="Asia/Taipei", utc_iso="2026-08-04T12:00:00+00:00",
                local_iso="2026-08-04T20:00:00+08:00", local_date="2026-08-04",
                local_time="20:00", weekday_zh_tw="星期二",
            ),
        )
        return turn.model_copy(update=updates)

    def test_direct_chat_returns_without_synthesizer(self):
        ctx = self._ctx("哈囉")
        plan = Plan(mode="direct_chat", tasks=[], direct_reply="这是簡體中文。")
        with patch.dict("os.environ", {"AYUE_V3_SIMPLE_CHAT_FAST_PATH": "on"}), \
             patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context",
                   return_value=self._direct_turn()), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.active_guidance_offer", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.active_assessment_session", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.awaiting_assessment_commit", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.synthesizer.synthesize") as synth, \
             patch("services.ayue_agent.v3.scheduler._persist_trace") as persist_trace:
            result = run_public_agent_turn_v3(ctx)
        self.assertEqual(result.reply, "這是簡體中文。")
        self.assertEqual(len(result.llm_call_metrics), 1)
        self.assertEqual(result.llm_call_metrics[0]["agent"], "planner")
        trace = persist_trace.call_args.args[2]
        self.assertEqual(trace["execution_mode"], "direct_chat")
        self.assertEqual(trace["llm_call_count"], 1)
        synth.assert_not_called()

    def test_matching_principles_product_info_answers_without_generic_identity_copy(self):
        ctx = self._ctx("你們到底怎麼配對的？")
        plan = Plan(mode="product_info", tasks=[], product_info_topics=["matching_principles"])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context",
                   return_value=self._direct_turn(ctx.message)), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.active_guidance_offer", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.active_assessment_session", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.awaiting_assessment_commit", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.synthesizer.synthesize", return_value=(
                 "不是抽籤。我會綜合你的近況與偏好，再讓媒合系統排序。",
                 None,
                 SynthesizerMetrics(
                     input_tokens=20, output_tokens=18, duration_ms=40,
                     used_llm=True, reply_source="llm",
                     presentation_messages=["不是抽籤。我會綜合你的近況與偏好，再讓媒合系統排序。"],
                     presentation_class="product_info",
                 ),
             )) as synth, \
             patch("services.ayue_agent.v3.scheduler._persist_trace"):
            result = run_public_agent_turn_v3(ctx)

        self.assertIn("不是抽籤", result.reply)
        self.assertIn("媒合系統排序", result.reply)
        self.assertNotIn("我是阿月", result.reply)
        self.assertEqual(result.conversation_intent, "product_info")
        observation = synth.call_args.args[0].payload["observations"][0]
        self.assertEqual(observation["result"]["product_info"]["topics"], ["matching_principles"])
        self.assertIn("matching", observation["result"]["product_info"]["facts"])

    def test_direct_chat_is_blocked_by_calendar_draft_and_uses_synthesizer(self):
        ctx = self._ctx("早上九點")
        plan = Plan(mode="direct_chat", tasks=[], direct_reply="好的")
        with patch.dict("os.environ", {"AYUE_V3_SIMPLE_CHAT_FAST_PATH": "on"}), \
             patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context",
                   return_value=self._direct_turn(
                       "早上九點", calendar_draft={"action": "create", "missing_fields": ["date"]},
                   )), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.active_guidance_offer", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.active_assessment_session", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.awaiting_assessment_commit", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.synthesizer.synthesize",
                   return_value=("我會依照正常流程處理。", None, _synth_metrics())) as synth, \
             patch("services.ayue_agent.v3.scheduler._persist_trace"):
            result = run_public_agent_turn_v3(ctx)
        self.assertEqual(result.reply, "我會依照正常流程處理。")
        synth.assert_called_once()

    def test_direct_chat_gate_rejects_pending_and_match_workflow_state(self):
        plan = Plan(mode="direct_chat", tasks=[], direct_reply="嗨")
        turn = self._direct_turn()
        with patch.dict("os.environ", {"AYUE_V3_SIMPLE_CHAT_FAST_PATH": "off"}):
            self.assertEqual(_direct_chat_block_reason(plan, turn, [], None), "feature_disabled")
        with patch.dict("os.environ", {"AYUE_V3_SIMPLE_CHAT_FAST_PATH": "on"}):
            self.assertEqual(_direct_chat_block_reason(
                plan, turn, [{"tool_name": "calendar.submit_commands"}], None,
            ), "pending_confirmation")
            self.assertEqual(_direct_chat_block_reason(
                plan, turn, [], {"fingerprint": "fp"}), "active_match_guidance")
            self.assertEqual(_direct_chat_block_reason(
                plan, turn.model_copy(update={"active_proposal": {"status": "pending"}}), [], None,
            ), "active_match_proposal")

    def test_assessment_start_wording_only_confirms_assessment_pending(self):
        self.assertTrue(_assessment_start_confirmation_requested(
            "開始阿", [{"tool_name": "profile.start_assessment"}],
        ))
        self.assertFalse(_assessment_start_confirmation_requested(
            "開始阿", [{"tool_name": "calendar.submit_commands"}],
        ))
        self.assertFalse(_assessment_start_confirmation_requested(
            "開始阿", [],
        ))

    def test_assessment_intent_routes_to_profile_write_confirmation(self):
        ctx = self._ctx("那我來做基本性格")
        plan = Plan(tasks=[
            SubTask(id="p1", agent="profile", depends_on=[], task_brief="開始基本性格探索"),
            SubTask(id="s1", agent="synthesizer", depends_on=["p1"], task_brief="回覆確認預覽"),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "profile": MagicMock(return_value=(
                     _proposal("profile.start_assessment", {"kind": "basic"}), _sub_metrics(),
                 )),
             }), \
             patch("services.ayue_agent.v3.scheduler.prepare_write_confirmation", return_value=(
                 {"action": "profile.start_assessment", "arguments": {"kind": "basic"}, "data": {}},
                 "要重新開始基本性格嗎？回覆「確認」就開始。",
             )), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.create_confirmation") as create_confirmation, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=(
                 "要重新開始基本性格嗎？回覆「確認」就開始。", None, _synth_metrics(),
             )):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        self.assertIn("重新開始基本性格", result.reply)
        create_confirmation.assert_called_once()
        self.assertEqual(create_confirmation.call_args.kwargs["tool_name"], "profile.start_assessment")

    def test_steak_example_full_flow(self):
        ctx = self._ctx("我下週五晚上想吃牛排，幫我找餐廳並看看我那天有沒有空")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="查詢使用者下週五晚上的行程空檔"),
            SubTask(id="t2", agent="places", depends_on=[], task_brief="搜尋附近的牛排餐廳"),
            SubTask(id="t3", agent="places", depends_on=["t2"], task_brief="依 t2 結果篩選推薦餐廳"),
            SubTask(id="t4", agent="synthesizer", depends_on=["t1", "t2", "t3"], task_brief="彙整"),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "calendar": MagicMock(return_value=([ToolProposal(tool_name="calendar.list_my_events", arguments={})], _sub_metrics())),
                 "places": MagicMock(side_effect=[
                     ([ToolProposal(tool_name="places.search_nearby", arguments={"anchor": "牛排餐廳", "categories": ["restaurant"]})], _sub_metrics()),
                     ([ToolProposal(tool_name="places.search_nearby", arguments={"anchor": "附近咖啡廳", "categories": ["cafe"]})], _sub_metrics()),
                 ]),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool") as mock_exec, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("這週末有家庭聚餐，我也找到附近一家義式料理餐廳，要不要一起去？", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_exec.return_value = MagicMock(ok=True, data={"events": []}, error_code=None)
            result = run_public_agent_turn_v3(ctx)
        self.assertIsInstance(result, AgentResult)
        self.assertTrue(result.handled)
        self.assertEqual(result.agent_mode, "v3")

    def test_recent_calendar_mutation_challenge_uses_read_verification_not_new_write(self):
        plan = Plan(tasks=[
            SubTask(id="c1", agent="calendar", depends_on=[], task_brief="確認剛才的行事曆操作"),
            SubTask(id="s1", agent="synthesizer", depends_on=["c1"], task_brief="整理驗證結果"),
        ])
        runner = MagicMock(return_value=(
            CalendarAgentResult(commands=[], reads=[ToolProposal(
                tool_name="calendar.verify_recent_mutation", arguments={},
            )]),
            _sub_metrics(),
        ))
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {"calendar": runner}), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.create_confirmation") as create_confirmation, \
             patch("services.ayue_agent.v3.scheduler.execute_tool", return_value=MagicMock(
                 ok=True,
                 data={"calendar_mutation_verification": {
                     "status": "verified_success", "action": "cancel",
                     "label": "看牙醫", "outcome": "success",
                 }},
                 error_code=None,
                 private_data={},
             )) as execute_tool, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=(
                 "我確認過了，剛才已取消「看牙醫」。", None, _synth_metrics(),
             )):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(self._ctx("剛才取消有成功嗎？"))

        self.assertTrue(result.handled)
        self.assertEqual(execute_tool.call_args.args[0].name, "calendar.verify_recent_mutation")
        create_confirmation.assert_not_called()

    def test_failed_sub_agent_skipped_and_synthesizer_handles_gap(self):
        ctx = self._ctx("你好嗎")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="查詢本人行程"),
            SubTask(id="t2", agent="places", depends_on=[], task_brief="找地點"),
            SubTask(id="t3", agent="synthesizer", depends_on=["t1", "t2"], task_brief="彙整"),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "calendar": MagicMock(return_value=([ToolProposal(tool_name="calendar.list_my_events", arguments={})], _sub_metrics())),
                 "places": MagicMock(return_value=([], _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", return_value=MagicMock(ok=True, data={}, error_code=None)), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("部分資訊ok，但部分沒找到", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)

    def test_planner_returns_none_yields_fail_closed(self):
        ctx = self._ctx("嗨")
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(None, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        self.assertEqual(result.agent_mode, "v3")
        self.assertIsNotNone(result.fallback_reason)

    def test_calendar_two_turn_missing_time_continuation_keeps_canonical_draft(self):
        """Exercise Planner → Calendar → preflight across the real turn boundary."""
        clear_draft("owner")
        first_plan = Plan(tasks=[
            SubTask(id="c1", agent="calendar", depends_on=[], task_brief="新增下禮拜三看牙醫"),
            SubTask(id="s1", agent="synthesizer", depends_on=["c1"], task_brief="彙整行事曆結果"),
        ])
        second_plan = Plan(tasks=[
            SubTask(id="c2", agent="calendar", depends_on=[], task_brief="補上行程時間"),
            SubTask(id="s2", agent="synthesizer", depends_on=["c2"], task_brief="彙整行事曆結果"),
        ])
        fixed_clock = TurnClockV1(
            timezone="Asia/Taipei", utc_iso="2026-08-08T12:00:00+00:00",
            local_iso="2026-08-08T20:00:00+08:00", local_date="2026-08-08",
            local_time="20:00", weekday_zh_tw="星期六",
            temporal_references={"下禮拜三": "2026-08-12"},
        )

        def build_context(raw_ctx, *, clock):
            draft = public_projection(get_draft(raw_ctx.user_id))
            return PublicAgentTurnContext(
                user_id=raw_ctx.user_id, room_id=raw_ctx.room_id,
                message=raw_ctx.message, calendar_draft=draft, clock=clock,
            )

        first_command = CalendarCommand(action="create", title="看牙醫", date="下禮拜三")
        second_command = CalendarCommand(
            action="create", start_time="15:00", end_time="16:00", draft_mode="replace",
        )
        runner = MagicMock(side_effect=[
            (CalendarAgentResult(commands=[first_command]), _sub_metrics()),
            (CalendarAgentResult(commands=[second_command]), _sub_metrics()),
        ])
        ctx1 = self._ctx("幫我新增一個行事曆 我下禮拜三看牙醫")
        ctx2 = self._ctx("下午三點到四點")
        with patch("services.ayue_agent.v3.scheduler.plan_turn", side_effect=[
                 (first_plan, _planner_metrics()), (second_plan, _planner_metrics()),
             ]), \
             patch("services.ayue_agent.v3.scheduler.build_turn_clock", return_value=fixed_clock), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context", side_effect=build_context), \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {"calendar": runner}), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.create_confirmation", return_value={"confirmation_id": "c"}) as create_confirmation, \
             patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.calendar_commands.conflicts_for_viewer", return_value=[]), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("確認行程", None, _synth_metrics())):
            first_result = run_public_agent_turn_v3(ctx1)
            draft_after_first = get_draft("owner")
            second_result = run_public_agent_turn_v3(ctx2)

        self.assertTrue(first_result.handled)
        self.assertEqual((draft_after_first or {}).get("command", {}).get("date"), "2026-08-12")
        self.assertTrue(second_result.handled)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(create_confirmation.call_count, 1)
        payload = create_confirmation.call_args.kwargs["payload"]
        plan = payload["plans"][0]
        self.assertEqual(plan["form"]["title"], "看牙醫")
        self.assertEqual(plan["form"]["date"], "2026-08-12")
        self.assertEqual(plan["form"]["start_time"], "15:00")
        self.assertEqual(plan["form"]["end_time"], "16:00")
        self.assertIsNone(get_draft("owner"))

    def test_calendar_two_turn_nested_fields_provider_output_keeps_date_and_derives_end(self):
        """Provider fields wrapper must survive draft continuation end-to-end."""
        clear_draft("owner")
        self.addCleanup(clear_draft, "owner")
        first_plan = Plan(tasks=[
            SubTask(id="c1", agent="calendar", depends_on=[], task_brief="新增下週六一筆行事曆"),
            SubTask(id="s1", agent="synthesizer", depends_on=["c1"], task_brief="彙整行事曆結果"),
        ])
        second_plan = Plan(tasks=[
            SubTask(id="c2", agent="calendar", depends_on=[], task_brief="補上逛霧時代晚上九點一小時"),
            SubTask(id="s2", agent="synthesizer", depends_on=["c2"], task_brief="彙整行事曆結果"),
        ])
        fixed_clock = TurnClockV1(
            timezone="Asia/Taipei", utc_iso="2026-08-09T04:00:00+00:00",
            local_iso="2026-08-09T12:00:00+08:00", local_date="2026-08-09",
            local_time="12:00", weekday_zh_tw="星期日",
            temporal_references={"下週六": "2026-08-15"},
        )

        def build_context(raw_ctx, *, clock):
            return PublicAgentTurnContext(
                user_id=raw_ctx.user_id, room_id=raw_ctx.room_id,
                message=raw_ctx.message,
                calendar_draft=public_projection(get_draft(raw_ctx.user_id)),
                clock=clock,
            )

        provider_results = [
            ToolCallResult(content="", tool_calls=[{
                "name": "calendar.submit_commands",
                "arguments": {"commands": [{
                    "action": "create", "title": "行事曆", "date": "下週六",
                }]},
            }]),
            ToolCallResult(content="", tool_calls=[{
                "name": "calendar.submit_commands",
                "arguments": {"commands": [{
                    "action": "create",
                    "fields": {
                        "title": "逛霧時代",
                        "start_time": "21:00",
                        "duration_minutes": 60,
                    },
                }]},
            }]),
        ]
        with patch("services.ayue_agent.v3.scheduler.plan_turn", side_effect=[
                 (first_plan, _planner_metrics()), (second_plan, _planner_metrics()),
             ]), \
             patch("services.ayue_agent.v3.scheduler.build_turn_clock", return_value=fixed_clock), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context", side_effect=build_context), \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {"calendar": run_calendar_agent}), \
             patch("services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools", side_effect=provider_results), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.create_confirmation", return_value={"confirmation_id": "c"}) as create_confirmation, \
             patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.calendar_commands.conflicts_for_viewer", return_value=[]), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("確認行程", None, _synth_metrics())):
            first_result = run_public_agent_turn_v3(self._ctx("下週六幫我新增一筆行事曆"))
            draft_after_first = get_draft("owner")
            second_result = run_public_agent_turn_v3(self._ctx("逛霧時代 晚上九點逛一個小時左右"))

        self.assertTrue(first_result.handled)
        self.assertEqual((draft_after_first or {}).get("command", {}).get("date"), "2026-08-15")
        self.assertTrue(second_result.handled)
        self.assertEqual(create_confirmation.call_count, 1)
        payload = create_confirmation.call_args.kwargs["payload"]
        form = payload["plans"][0]["form"]
        self.assertEqual(form["date"], "2026-08-15")
        self.assertEqual(form["start_time"], "21:00")
        self.assertEqual(form["end_time"], "22:00")
        self.assertEqual(form["title"], "逛霧時代")
        preview = create_confirmation.call_args.kwargs["preview"]
        self.assertIn("8/15", preview)
        self.assertIn("21:00–22:00", preview)
        self.assertIn("逛霧時代", preview)
        self.assertNotIn("日期", preview)

    def test_calendar_two_turn_lowercase_two_weeks_duration_keeps_title_and_date(self):
        """Real regression: date/title from turn one survive a time-only turn."""
        clear_draft("owner")
        first_plan = Plan(tasks=[
            SubTask(id="c1", agent="calendar", depends_on=[], task_brief="新增下下周四去駁二玩"),
            SubTask(id="s1", agent="synthesizer", depends_on=["c1"], task_brief="彙整行事曆結果"),
        ])
        second_plan = Plan(tasks=[
            SubTask(id="c2", agent="calendar", depends_on=[], task_brief="補上早上九點一小時"),
            SubTask(id="s2", agent="synthesizer", depends_on=["c2"], task_brief="彙整行事曆結果"),
        ])
        fixed_clock = TurnClockV1(
            timezone="Asia/Taipei", utc_iso="2026-08-08T12:00:00+00:00",
            local_iso="2026-08-08T20:00:00+08:00", local_date="2026-08-08",
            local_time="20:00", weekday_zh_tw="星期六",
            temporal_references={"下下周四": "2026-08-20"},
        )

        def build_context(raw_ctx, *, clock):
            draft = public_projection(get_draft(raw_ctx.user_id))
            return PublicAgentTurnContext(
                user_id=raw_ctx.user_id, room_id=raw_ctx.room_id,
                message=raw_ctx.message, calendar_draft=draft, clock=clock,
            )

        first_command = CalendarCommand(action="create", title="去駁二玩", date="下下周四")
        second_command = CalendarCommand(
            action="create", start_time="09:00", duration_minutes=60, draft_mode="replace",
        )
        runner = MagicMock(side_effect=[
            (CalendarAgentResult(commands=[first_command]), _sub_metrics()),
            (CalendarAgentResult(commands=[second_command]), _sub_metrics()),
        ])
        ctx1 = self._ctx("下下周四我要去駁二玩 幫我新增到行事曆")
        ctx2 = self._ctx("早上9點 大概一小時")
        with patch("services.ayue_agent.v3.scheduler.plan_turn", side_effect=[
                 (first_plan, _planner_metrics()), (second_plan, _planner_metrics()),
             ]), \
             patch("services.ayue_agent.v3.scheduler.build_turn_clock", return_value=fixed_clock), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context", side_effect=build_context), \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {"calendar": runner}), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.create_confirmation", return_value={"confirmation_id": "c"}) as create_confirmation, \
             patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.calendar_commands.conflicts_for_viewer", return_value=[]), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("確認行程", None, _synth_metrics())):
            first_result = run_public_agent_turn_v3(ctx1)
            draft_after_first = get_draft("owner")
            second_result = run_public_agent_turn_v3(ctx2)

        self.assertTrue(first_result.handled)
        self.assertEqual((draft_after_first or {}).get("command", {}).get("title"), "去駁二玩")
        self.assertEqual((draft_after_first or {}).get("command", {}).get("date"), "2026-08-20")
        self.assertTrue(second_result.handled)
        create_confirmation.assert_called_once()
        payload = create_confirmation.call_args.kwargs["payload"]
        form = payload["plans"][0]["form"]
        self.assertEqual(form["title"], "去駁二玩")
        self.assertEqual(form["date"], "2026-08-20")
        self.assertEqual(form["start_time"], "09:00")
        self.assertEqual(form["end_time"], "10:00")
        self.assertIsNone(get_draft("owner"))

    def test_calendar_update_target_binding_survives_time_shift_followup(self):
        """A resolved update target remains bound across a time-only clarification."""
        clear_draft("owner")
        first_plan = Plan(tasks=[
            SubTask(id="c1", agent="calendar", depends_on=[], task_brief="修改看牙醫行程"),
            SubTask(id="s1", agent="synthesizer", depends_on=["c1"], task_brief="整理結果"),
        ])
        second_plan = Plan(tasks=[
            SubTask(id="c2", agent="calendar", depends_on=[], task_brief="時間延後一小時"),
            SubTask(id="s2", agent="synthesizer", depends_on=["c2"], task_brief="整理結果"),
        ])
        fixed_clock = TurnClockV1(
            timezone="Asia/Taipei", utc_iso="2026-08-08T12:00:00+00:00",
            local_iso="2026-08-08T20:00:00+08:00", local_date="2026-08-08",
            local_time="20:00", weekday_zh_tw="星期六", temporal_references={},
        )
        event = {
            "event_id": "dentist-1", "revision": 4, "source_type": "personal",
            "participants": ["owner"], "title": "看牙醫",
            "start_at": datetime(2026, 8, 10, 7, 0, tzinfo=timezone.utc),
            "end_at": datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc),
            "timezone": "Asia/Taipei", "location": "", "notes": "", "status": "confirmed",
        }

        def build_context(raw_ctx, *, clock):
            return PublicAgentTurnContext(
                user_id=raw_ctx.user_id, room_id=raw_ctx.room_id,
                message=raw_ctx.message, calendar_draft=public_projection(get_draft(raw_ctx.user_id)),
                clock=clock,
            )

        first_command = CalendarCommand(action="update", target_hint="看牙醫")
        second_command = CalendarCommand(action="update", time_shift_minutes=60)
        runner = MagicMock(side_effect=[
            (CalendarAgentResult(commands=[first_command]), _sub_metrics()),
            (CalendarAgentResult(commands=[second_command]), _sub_metrics()),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", side_effect=[
                 (first_plan, _planner_metrics()), (second_plan, _planner_metrics()),
             ]), \
             patch("services.ayue_agent.v3.scheduler.build_turn_clock", return_value=fixed_clock), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context", side_effect=build_context), \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {"calendar": runner}), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.create_confirmation", return_value={"confirmation_id": "c"}) as create_confirmation, \
             patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.calendar_commands.resolve_owned_event", return_value=(event, None)) as resolve_hint, \
             patch("services.calendar_service.resolve_owned_event_reference", return_value=(event, None)) as resolve_reference, \
             patch("services.ayue_agent.v3.calendar_commands.get_owned_event_resolution_candidates", return_value=[]), \
             patch("services.ayue_agent.v3.calendar_commands.get_owned_event_resolution_kind", return_value="exact"), \
             patch("services.ayue_agent.v3.calendar_commands.conflicts_for_viewer", return_value=[]), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("完成", None, _synth_metrics())):
            first_result = run_public_agent_turn_v3(self._ctx("我牙醫那筆行程想修改"))
            draft_after_first = get_draft("owner")
            second_result = run_public_agent_turn_v3(self._ctx("日期一樣，時間延後一小時"))

        self.assertTrue(first_result.handled)
        self.assertTrue((draft_after_first or {}).get("resolved_target", {}).get("bound"))
        self.assertTrue(second_result.handled)
        self.assertEqual(resolve_hint.call_count, 1)
        self.assertEqual(resolve_reference.call_count, 1)
        create_confirmation.assert_called_once()
        form = create_confirmation.call_args.kwargs["payload"]["plans"][0]["form"]
        self.assertEqual(form["date"], "2026-08-10")
        self.assertEqual(form["start_time"], "16:00")
        self.assertEqual(form["end_time"], "17:00")
        self.assertIsNone(get_draft("owner"))

    def test_calendar_one_turn_relative_weekday_reaches_confirmation(self):
        clear_draft("owner")
        plan = Plan(tasks=[
            SubTask(id="c1", agent="calendar", depends_on=[], task_brief="新增下禮拜三看牙醫"),
            SubTask(id="s1", agent="synthesizer", depends_on=["c1"], task_brief="彙整行事曆結果"),
        ])
        fixed_clock = TurnClockV1(
            timezone="Asia/Taipei", utc_iso="2026-08-08T12:00:00+00:00",
            local_iso="2026-08-08T20:00:00+08:00", local_date="2026-08-08",
            local_time="20:00", weekday_zh_tw="星期六",
            temporal_references={"下禮拜三": "2026-08-12"},
        )
        command = CalendarCommand(
            action="create", title="看牙醫", date="下禮拜三", start_time="15:00", end_time="16:00",
        )

        def build_context(raw_ctx, *, clock):
            return PublicAgentTurnContext(
                user_id=raw_ctx.user_id, room_id=raw_ctx.room_id,
                message=raw_ctx.message, clock=clock,
            )

        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_turn_clock", return_value=fixed_clock), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context", side_effect=build_context), \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "calendar": MagicMock(return_value=(CalendarAgentResult(commands=[command]), _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.create_confirmation", return_value={"confirmation_id": "c"}) as create_confirmation, \
             patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.calendar_commands.conflicts_for_viewer", return_value=[]), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("確認行程", None, _synth_metrics())):
            result = run_public_agent_turn_v3(
                self._ctx("我下禮拜三三點到四點要看牙醫 幫我加到行事曆"),
            )

        self.assertTrue(result.handled)
        create_confirmation.assert_called_once()
        payload = create_confirmation.call_args.kwargs["payload"]
        self.assertEqual(payload["plans"][0]["form"]["date"], "2026-08-12")
        self.assertIsNone(get_draft("owner"))

    def test_failed_dependency_skips_dependent_task(self):
        ctx = self._ctx("幫我找餐廳並篩選")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="places", depends_on=[], task_brief="搜尋附近餐廳"),
            SubTask(id="t2", agent="places", depends_on=["t1"], task_brief="依 t1 結果篩選"),
            SubTask(id="t3", agent="synthesizer", depends_on=["t1", "t2"], task_brief="彙整"),
        ])
        places_runner = MagicMock(return_value=([], _sub_metrics()))
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {"places": places_runner}), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", return_value=MagicMock(ok=True, data={}, error_code=None)), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("沒找到適合的餐廳", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        self.assertEqual(places_runner.call_count, 1)

    def test_write_task_with_not_found_prior_is_ok_not_failed(self):
        # 寫入任務因候選查詢 not_found 而沒有提出任何寫入時，應標 OK +
        # no_write_proposed（不是 sub_agent_no_proposal 失敗），讓 synthesizer
        # 優雅回「找不到」。
        ctx = self._ctx("移除出國行程")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="查詢出國行程"),
            SubTask(id="t2", agent="calendar", depends_on=["t1"], task_brief="移除出國"),
            SubTask(id="t3", agent="synthesizer", depends_on=["t2"], task_brief="彙整"),
        ])
        seen_obs = {}
        def fake_synth(slice_payload, candidate_cards=None):
            seen_obs["observations"] = slice_payload.payload.get("observations", [])
            return ("好", None, _synth_metrics())
        def fake_runner(context_slice, *, task_brief):
            if task_brief == "查詢出國行程":
                return ([ToolProposal(tool_name="calendar.find_my_event",
                                      arguments={"event_hint": "出國"})], _sub_metrics())
            return ([], _sub_metrics())  # write task: no proposal
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "calendar": MagicMock(side_effect=fake_runner),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool",
                   return_value=MagicMock(ok=True, data={
                       "status": "not_found", "reason_code": "event_not_found",
                       "query": "出國", "candidates": [],
                   }, error_code=None)), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", side_effect=fake_synth):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value._mentioned_ids = []
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        obs = seen_obs.get("observations", [])
        write_obs = [o for o in obs if o.get("task_id") == "t2"]
        self.assertEqual(len(write_obs), 1)
        self.assertEqual(write_obs[0]["status"], "ok")
        self.assertTrue(write_obs[0]["result"].get("no_write_proposed"))
        self.assertEqual(write_obs[0]["result"].get("not_found_queries"), ["出國"])

    def test_progress_events_emitted(self):
        ctx = self._ctx("我下週五想吃牛排，看看行程")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="查詢行程"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        events: list[dict] = []
        def capture(event):
            events.append(event)
        cal_runner = MagicMock(return_value=([ToolProposal(tool_name="calendar.list_my_events", arguments={})], _sub_metrics()))
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {"calendar": cal_runner}), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", return_value=MagicMock(ok=True, data={"events": []}, error_code=None)), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("行程ok", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            run_public_agent_turn_v3(ctx, on_progress=capture)
        event_types = [e["type"] for e in events]
        self.assertIn("run_started", event_types)
        self.assertIn("tool_started", event_types)
        self.assertIn("tool_finished", event_types)
        tool_started_events = [e for e in events if e["type"] == "tool_started"]
        self.assertTrue(len(tool_started_events) >= 1)
        self.assertIn("text", tool_started_events[0])

    def test_progress_events_not_emitted_for_planner_failure(self):
        ctx = self._ctx("壞掉")
        events: list[dict] = []
        def capture(event):
            events.append(event)
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(None, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            run_public_agent_turn_v3(ctx, on_progress=capture)
        event_types = [e["type"] for e in events]
        self.assertIn("run_started", event_types)
        self.assertNotIn("tool_started", event_types)
        self.assertNotIn("tool_finished", event_types)

    def test_same_layer_tasks_run_in_parallel(self):
        ctx = self._ctx("幫我同時查行程、餐廳和聯絡人")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="查行程"),
            SubTask(id="t2", agent="places", depends_on=[], task_brief="找餐廳"),
            SubTask(id="t3", agent="relationship", depends_on=[], task_brief="查關係"),
            SubTask(id="t4", agent="synthesizer", depends_on=["t1", "t2", "t3"], task_brief="彙整"),
        ])
        active = []
        active_lock = threading.Lock()
        max_active = 0

        def slow_runner(proposals, metrics):
            nonlocal max_active
            with active_lock:
                active.append(1)
                max_active = max(max_active, len(active))
            time.sleep(0.2)
            with active_lock:
                active.pop()
            return proposals, metrics

        proposals = {
            "calendar": [ToolProposal(tool_name="calendar.list_my_events", arguments={})],
            "places": [ToolProposal(tool_name="places.search_nearby", arguments={"anchor": "中壢", "categories": ["restaurant"]})],
            "relationship": [ToolProposal(tool_name="relationship.list_accepted_contacts", arguments={})],
        }
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "calendar": lambda slc, task_brief: slow_runner(proposals["calendar"], _sub_metrics()),
                 "places": lambda slc, task_brief: slow_runner(proposals["places"], _sub_metrics()),
                 "relationship": lambda slc, task_brief: slow_runner(proposals["relationship"], _sub_metrics()),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", return_value=MagicMock(ok=True, data={}, error_code=None)), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("都查好了", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            run_public_agent_turn_v3(ctx)
        self.assertGreaterEqual(max_active, 2, "same-layer tasks should overlap in time")

    def test_parallelism_respects_max_parallel_flag(self):
        ctx = self._ctx("查很多")
        plan = Plan(tasks=[
            SubTask(id=f"t{i}", agent="calendar", depends_on=[], task_brief=f"查{i}") for i in range(3)
        ] + [SubTask(id="syn", agent="synthesizer", depends_on=[f"t{i}" for i in range(3)], task_brief="彙整")])
        active = []
        active_lock = threading.Lock()
        max_active = 0

        def slow_runner(slc, task_brief):
            nonlocal max_active
            with active_lock:
                active.append(1)
                max_active = max(max_active, len(active))
            time.sleep(0.15)
            with active_lock:
                active.pop()
            return [ToolProposal(tool_name="calendar.list_my_events", arguments={})], _sub_metrics()

        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.MAX_PARALLEL", 2), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {"calendar": slow_runner}), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", return_value=MagicMock(ok=True, data={}, error_code=None)), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("ok", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            run_public_agent_turn_v3(ctx)
        self.assertLessEqual(max_active, 2, "parallelism must be capped by AYUE_SUBAGENT_MAX_PARALLEL")

    def test_prior_observations_only_include_declared_dependencies(self):
        results = {
            "t1": [SubTaskResult(task_id="t1", status=SubTaskStatus.OK,
                                 tool_name="calendar.list_my_events",
                                 observation={"events": [{"date": "2026-08-11", "activity": "看電視"}]})],
            "t2": [SubTaskResult(task_id="t2", status=SubTaskStatus.OK,
                                 tool_name="places.search_nearby",
                                 observation={"places": [{"name": "青埔香雞排", "map_url": "https://x"}]})],
            "t3": [SubTaskResult(task_id="t3", status=SubTaskStatus.FAILED,
                                 tool_name="relationship.list_accepted_contacts", error_code="tool_error")],
        }
        rel_task = SubTask(id="r", agent="relationship", depends_on=[], task_brief="查關係")
        prior = _prior_observations_for(rel_task, results)
        self.assertEqual(prior, [])
        dep_task = SubTask(id="p2", agent="places", depends_on=["t2"], task_brief="依 t2 查")
        prior = _prior_observations_for(dep_task, results)
        self.assertEqual([p["task_id"] for p in prior], ["t2"])
        self.assertEqual(prior[0]["result"]["places"][0]["name"], "青埔香雞排")
        self.assertNotIn("t1", [p["task_id"] for p in prior])
        self.assertNotIn("t3", [p["task_id"] for p in prior])

    @unittest.skip("legacy direct Calendar proposal contract removed; typed command flow is covered below")
    def test_find_candidates_flow_into_dependent_calendar_write_task(self):
        """修改/取消行程兩階段：read task 的 find_my_event 候選必須進入
        write task 的 context slice（含 ambiguous 的 candidates 陣列）。"""
        ctx = self._ctx("把8/12的吃牛排改到8/15")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="查詢原本行程"),
            SubTask(id="t2", agent="calendar", depends_on=["t1"], task_brief="提出修改"),
            SubTask(id="t3", agent="synthesizer", depends_on=["t2"], task_brief="彙整"),
        ])
        seen_slices: list[dict] = []

        def fake_runner(context_slice, *, task_brief):
            seen_slices.append({"slice": context_slice, "brief": task_brief})
            if task_brief == "查詢原本行程":
                return ([ToolProposal(tool_name="calendar.find_my_event",
                                      arguments={"event_hint": "吃牛排"})], _sub_metrics())
            return ([ToolProposal(tool_name="calendar.update_my_event", arguments={
                "event_hint": "8月12日18:00到20:00吃牛排",
                "date": "2026-08-15", "start_time": "18:00", "end_time": "20:00",
            })], _sub_metrics())

        find_result = {
            "status": "found", "reason_code": "", "activity": "吃牛排",
            "date": "2026-08-12", "start_time": "18:00", "end_time": "20:00",
            "event_kind": "personal", "companion_known": False,
            "companion_display_name": "對方", "companion_safe_summary": "",
            "candidates": [],
        }
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "calendar": MagicMock(side_effect=fake_runner),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool",
                   return_value=MagicMock(ok=True, data=find_result, error_code=None)) as mock_exec, \
             patch("services.ayue_agent.v3.scheduler.prepare_write_confirmation",
                   return_value=({"action": "calendar.update_my_event", "arguments": {}, "data": {}},
                                 "要把「吃牛排」改成8/15嗎？回覆「確認」")) as prepare, \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.update_many"), \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.insert_one"), \
             patch("services.ayue_agent.v3.synthesizer.synthesize",
                   return_value=("好，等你確認", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value._mentioned_ids = []
            mock_build.return_value.user_id = "owner"
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        # t1 read 執行一次；t2 write 的 context 必須帶入 t1 的 find_my_event observation
        mock_exec.assert_called_once()
        self.assertEqual(len(seen_slices), 2)
        write_slice = seen_slices[1]["slice"]
        prior = write_slice.payload.get("prior_observations") or []
        self.assertEqual(len(prior), 1)
        self.assertEqual(prior[0]["tool"], "calendar.find_my_event")
        self.assertEqual(prior[0]["result"]["date"], "2026-08-12")
        prepare.assert_called_once()

    def test_multi_call_sub_agent_executes_every_proposal(self):
        """A sub-agent emitting two tool calls must execute both, not just the first."""
        ctx = self._ctx("在高雄市三民區找牛排餐廳和冰店")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="places", depends_on=[], task_brief="查牛排與冰店"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        executed: list[str] = []

        def fake_exec(call, ctx, clock=None):
            executed.append(call.name)
            return MagicMock(ok=True, data={"places": []}, error_code=None)

        multi = [
            ToolProposal(tool_name="places.search_nearby",
                         arguments={"anchor": "高雄市三民區", "categories": ["restaurant"], "cuisine": "牛排"}),
            ToolProposal(tool_name="places.search_nearby",
                         arguments={"anchor": "高雄市三民區", "categories": ["cafe"], "cuisine": "冰"}),
        ]
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "places": MagicMock(return_value=(multi, _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", side_effect=fake_exec), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("牛排和冰都查好了", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            run_public_agent_turn_v3(ctx)
        self.assertEqual(len(executed), 2, "both tool calls must execute")

    def test_parallel_tasks_with_identical_calls_are_globally_deduped(self):
        """Identical tool+args across parallel tasks execute once per turn."""
        ctx = self._ctx("分開查兩次附近餐廳")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="places", depends_on=[], task_brief="查餐廳 A"),
            SubTask(id="t2", agent="places", depends_on=[], task_brief="查餐廳 B"),
            SubTask(id="t3", agent="synthesizer", depends_on=["t1", "t2"], task_brief="彙整"),
        ])
        executed: list[str] = []

        def fake_exec(call, ctx, clock=None):
            executed.append(call.name)
            return MagicMock(ok=True, data={"places": []}, error_code=None)

        runner = MagicMock(return_value=(
            [ToolProposal(tool_name="places.search_nearby",
                          arguments={"anchor": "三民區", "categories": ["restaurant"]})],
            _sub_metrics(),
        ))
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {"places": runner}), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", side_effect=fake_exec), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("兩邊都查好了", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            run_public_agent_turn_v3(ctx)
        self.assertEqual(len(executed), 1, "identical calls in separate tasks must execute once")

    def test_duplicate_calls_within_same_task_are_deduped(self):
        """Same tool+args twice in ONE task: second call must be rejected as duplicate."""
        ctx = self._ctx("查兩次同樣的")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="查行程"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        executed: list[str] = []

        def fake_exec(call, ctx, clock=None):
            executed.append(call.name)
            return MagicMock(ok=True, data={"events": []}, error_code=None)

        multi = [
            ToolProposal(tool_name="calendar.list_my_events", arguments={}),
            ToolProposal(tool_name="calendar.list_my_events", arguments={}),
        ]
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "calendar": MagicMock(return_value=(multi, _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", side_effect=fake_exec), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("行程查好了", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            run_public_agent_turn_v3(ctx)
        self.assertEqual(len(executed), 1, "duplicate call within one task must be rejected")

    def test_mentioned_tool_without_mention_fails_cleanly(self):
        """MENTIONED tool with no @ mention must fail as mentioned_required, not crash the run."""
        ctx = self._ctx("查一下小晴的資料")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="relationship", depends_on=[], task_brief="查小晴的互動脈絡"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        seen_observations = {}

        def fake_synth(slice_payload, candidate_cards=None):
            seen_observations["observations"] = slice_payload.payload.get("observations", [])
            return ("小晴的資料這次沒查到", None, _synth_metrics())

        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "relationship": MagicMock(return_value=(
                     [ToolProposal(tool_name="relationship.get_mentioned_contact_summary",
                                   arguments={})],
                     _sub_metrics(),
                 )),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool") as mock_exec, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", side_effect=fake_synth):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value._mentioned_ids = []
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        self.assertIsNone(result.fallback_reason)
        mock_exec.assert_not_called()
        obs = seen_observations.get("observations", [])
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["status"], "failed")
        self.assertEqual(obs[0]["error_code"], "mentioned_required")

    def test_sub_agent_exception_does_not_crash_run(self):
        """A sub-agent raising an unexpected exception must not crash the whole run."""
        ctx = self._ctx("查行程和關係")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="查行程"),
            SubTask(id="t2", agent="relationship", depends_on=[], task_brief="查關係"),
            SubTask(id="t3", agent="synthesizer", depends_on=["t1", "t2"], task_brief="彙整"),
        ])
        seen_observations = {}

        def fake_synth(slice_payload, candidate_cards=None):
            seen_observations["observations"] = slice_payload.payload.get("observations", [])
            return ("行程查到了，關係資料這次沒查到", None, _synth_metrics())

        def boom(slc, task_brief):
            raise RuntimeError("boom")

        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "calendar": MagicMock(return_value=(
                     [ToolProposal(tool_name="calendar.list_my_events", arguments={})], _sub_metrics())),
                 "relationship": boom,
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", return_value=MagicMock(ok=True, data={"events": []}, error_code=None)), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", side_effect=fake_synth):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value._mentioned_ids = []
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        self.assertIsNone(result.fallback_reason)
        obs = {o["task_id"]: o for o in seen_observations.get("observations", [])}
        self.assertEqual(obs["t1"]["status"], "ok")
        self.assertEqual(obs["t2"]["status"], "failed")
        self.assertEqual(obs["t2"]["error_code"], "sub_agent_exception")

    def _cards(self):
        return [
            {"name": "店A", "category": "restaurant", "distance_label": "100 公尺"},
            {"name": "店B", "category": "cafe", "distance_label": "200 公尺"},
            {"name": "店C", "category": "bar", "distance_label": "300 公尺"},
        ]

    def test_card_decision_show_all_returns_all(self):
        cards = self._cards()
        self.assertEqual(_apply_card_decision(cards, {"mode": "show_all", "indices": []}), cards)

    def test_card_decision_none_returns_empty(self):
        self.assertEqual(_apply_card_decision(self._cards(), {"mode": "none", "indices": []}), [])

    def test_card_decision_select_filters_indices(self):
        result = _apply_card_decision(self._cards(), {"mode": "select", "indices": [0, 2]})
        self.assertEqual([c["name"] for c in result], ["店A", "店C"])

    def test_card_decision_select_out_of_range_falls_back_to_all(self):
        result = _apply_card_decision(self._cards(), {"mode": "select", "indices": [99]})
        self.assertEqual(len(result), 3)

    def test_card_decision_select_dedupes_indices(self):
        result = _apply_card_decision(self._cards(), {"mode": "select", "indices": [1, 1, 1]})
        self.assertEqual([c["name"] for c in result], ["店B"])

    def test_card_decision_none_decision_returns_all(self):
        self.assertEqual(_apply_card_decision(self._cards(), None), self._cards())

    def test_card_decision_no_candidates_returns_empty(self):
        self.assertEqual(_apply_card_decision([], {"mode": "show_all"}), [])

    def _place_result(self, category, count, prefix="店"):
        offset = {"restaurant": 0, "cafe": 10, "bar": 20, "attraction": 30, "park": 40}.get(category, 0)
        return SubTaskResult(
            task_id=f"t_{category}", status=SubTaskStatus.OK,
            tool_name="places.search_nearby",
            observation={"places": [
                {"name": f"{prefix}{category}{i}", "category": category,
                 "distance_m": 100 + i,
                 "map_url": f"https://www.openstreetmap.org/?mlat=25.{offset + i}&mlon=121.{offset + i}#map=18/25.{offset + i}/121.{offset + i}",
                 "provider": "openstreetmap"}
                for i in range(count)
            ]},
        )

    def test_place_cards_balanced_across_two_categories(self):
        """牛排+冰 兩種查詢 → 候選必須 4+4 平衡，不能只有第一種。"""
        results = [self._place_result("restaurant", 8, "牛排"), self._place_result("cafe", 8, "冰")]
        cards = _public_place_cards(results)
        self.assertEqual(len(cards), 8)
        cats = [c["category"] for c in cards]
        self.assertEqual(cats.count("restaurant"), 4)
        self.assertEqual(cats.count("cafe"), 4)

    def test_place_cards_single_category_capped_at_five(self):
        """單一查詢結果最多 5 張。"""
        results = [self._place_result("restaurant", 8)]
        cards = _public_place_cards(results)
        self.assertEqual(len(cards), 5)
        self.assertTrue(all(c["category"] == "restaurant" for c in cards))

    def test_place_cards_three_categories_balanced(self):
        results = [self._place_result("restaurant", 8), self._place_result("cafe", 8), self._place_result("bar", 8)]
        cards = _public_place_cards(results)
        self.assertEqual(len(cards), 8)
        cats = [c["category"] for c in cards]
        self.assertEqual(cats.count("restaurant"), 3)
        self.assertEqual(cats.count("cafe"), 3)
        self.assertEqual(cats.count("bar"), 2)

    def test_place_cards_round_robin_order(self):
        """round-robin 順序：restaurant, cafe, restaurant, cafe…"""
        results = [self._place_result("restaurant", 8), self._place_result("cafe", 8)]
        cards = _public_place_cards(results)
        self.assertEqual([c["category"] for c in cards[:4]], ["restaurant", "cafe", "restaurant", "cafe"])

    def test_place_cards_skips_failed_and_non_places(self):
        results = [
            self._place_result("restaurant", 3),
            SubTaskResult(task_id="t_fail", status=SubTaskStatus.FAILED,
                          tool_name="places.search_nearby", error_code="tool_error"),
            SubTaskResult(task_id="t_cal", status=SubTaskStatus.OK,
                          tool_name="calendar.list_my_events", observation={"events": []}),
        ]
        cards = _public_place_cards(results)
        self.assertEqual(len(cards), 3)
        self.assertTrue(all(c["category"] == "restaurant" for c in cards))


class V3SchedulerWriteTests(unittest.TestCase):
    def _ctx(self, message="確認"):
        return AgentTurnContext(user_id="owner", room_id="room", message=message)

    def test_write_proposal_creates_confirmation_with_preview(self):
        ctx = self._ctx("幫我找人")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="match", depends_on=[], task_brief="開始找人"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        seen_obs = {}
        def fake_synth(slice_payload, candidate_cards=None):
            seen_obs["observations"] = slice_payload.payload.get("observations", [])
            return ("好", None, _synth_metrics())
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "match": MagicMock(return_value=(
                     [ToolProposal(tool_name="match.start_search", arguments={})], _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.prepare_write_confirmation",
                   return_value=({"action": "match.start_search", "arguments": {}, "data": {}}, "要開始找人嗎？回覆「確認」")) as prepare, \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.update_many"), \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.insert_one") as insert, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", side_effect=fake_synth):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value._mentioned_ids = []
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        prepare.assert_called_once()
        insert.assert_called_once()
        obs = seen_obs.get("observations", [])
        self.assertEqual(obs[0]["status"], "ok")
        self.assertTrue(obs[0]["result"]["pending_confirmation"])
        self.assertIn("確認", obs[0]["result"]["preview"])

    def test_confirm_path_executes_write_and_relays_reply(self):
        ctx = self._ctx("確認")
        seen_obs = {}
        def fake_synth(slice_payload, candidate_cards=None):
            seen_obs["observations"] = slice_payload.payload.get("observations", [])
            return ("好，我開始幫你找", None, _synth_metrics())
        with patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS") as coll, \
             patch("services.ayue_agent.v3.scheduler.execute_write",
                   return_value=(True, "好，我開始幫你找，通常約需要 1–3 分鐘。", None)) as exec_write, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", side_effect=fake_synth):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            origin_run_id = "run-preview"
            request_fingerprint = ConfirmationManager._request_fingerprint(
                tool_name="match.start_search",
                arguments={},
                payload={},
                origin_run_id=origin_run_id,
            )
            coll.find.return_value = [{
                "_id": "c1", "user_id": "owner", "agent_name": "match",
                "tool_name": "match.start_search", "arguments": {},
                "payload": {}, "status": "pending",
                "created_at": 0, "expires_at": 1e18,
                "origin_run_id": origin_run_id,
                "request_fingerprint": request_fingerprint,
                "preview_fingerprint": "preview-digest",
            }]
            coll.update_one.return_value = MagicMock(modified_count=1)
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        exec_write.assert_called_once()
        self.assertEqual(exec_write.call_args.kwargs["payload"]["_confirmation_id"], "c1")

    @unittest.skip("legacy direct Calendar proposal contract removed; typed batch confirmation is covered above")
    def test_only_one_write_confirmation_created_per_subtask(self):
        # 同一 sub-task 提出兩個寫入工具時，calendar 寫入合併進同一 confirmation；
        # 非 calendar 寫入（match）仍一回合最多一筆。
        ctx = self._ctx("幫我取消A和新增B")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="處理行程"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        seen_obs = {}
        def fake_synth(slice_payload, candidate_cards=None):
            seen_obs["observations"] = slice_payload.payload.get("observations", [])
            return ("好", None, _synth_metrics())
        def fake_prepare(tool_name, arguments, ctx_obj, turn_obj):
            return ({"action": tool_name, "arguments": arguments, "data": {}},
                    f"要執行{tool_name}嗎？回覆「確認」")
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "calendar": MagicMock(return_value=(
                     [
                         ToolProposal(tool_name="calendar.cancel_my_event", arguments={"event_hint": "A"}),
                         ToolProposal(tool_name="calendar.create_my_event", arguments={
                             "title": "B", "date": "2026-08-20", "start_time": "10:00", "end_time": "11:00",
                         }),
                     ], _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.prepare_write_confirmation", side_effect=fake_prepare), \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.update_many"), \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.insert_one") as insert, \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.update_one") as update_one, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", side_effect=fake_synth):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value._mentioned_ids = []
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        insert.assert_called_once()
        inserted = insert.call_args[0][0]
        self.assertEqual(inserted["tool_name"], "calendar.cancel_my_event")
        # 第二筆 create 併入同一 confirmation 的 batch（新格式 {tool, arguments, data}）
        update_one.assert_not_called()
        obs = seen_obs.get("observations", [])
        confirmed = [o for o in obs if o.get("tool") == "calendar.create_my_event"]
        self.assertEqual(len(confirmed), 1)
        self.assertFalse(confirmed[0]["result"].get("pending_confirmation"))
        self.assertEqual(confirmed[0]["result"].get("ignored"), "one_write_per_turn")

    @unittest.skip("legacy direct Calendar proposal contract removed; typed batch confirmation is covered above")
    def test_two_create_proposals_keep_only_first_confirmation(self):
        # 同一 sub-task 提出兩筆 calendar.create_my_event → 一次 confirmation + batch 陣列；
        # 一次「確認」即新增兩筆（需求：一次確認變更多個行程）。
        ctx = self._ctx("幫我新增8/12吃牛排和8/9看醫生")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="新增兩筆行程"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "calendar": MagicMock(return_value=(
                     [
                         ToolProposal(tool_name="calendar.create_my_event", arguments={
                             "title": "吃牛排", "date": "2026-08-12", "start_time": "18:00", "end_time": "20:00",
                         }),
                         ToolProposal(tool_name="calendar.create_my_event", arguments={
                             "title": "看醫生", "date": "2026-08-09", "start_time": "08:30", "end_time": "12:05",
                         }),
                     ], _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.prepare_write_confirmation",
                   side_effect=lambda tn, args, c, t: (
                       {"action": tn, "arguments": args, "data": {}},
                       f"要新增{args['title']}嗎？回覆「確認」",
                   )), \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.insert_one") as insert, \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.update_one") as update_one, \
             patch("services.ayue_agent.v3.synthesizer.synthesize",
                   return_value=("好，等你確認", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value._mentioned_ids = []
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        # 只建立一筆 confirmation；第二筆 create 是 push 到同一 confirmation 的 batch 欄位
        insert.assert_called_once()
        inserted = insert.call_args[0][0]
        self.assertEqual(inserted["tool_name"], "calendar.create_my_event")
        self.assertEqual(inserted["arguments"]["title"], "吃牛排")
        # 第二筆 create 透過 $push 加入 batch（新格式 {tool, arguments, data}）
        update_one.assert_not_called()


class V3SchedulerTraceTests(unittest.TestCase):
    def test_local_debug_trace_marks_direct_chat_fast_path(self):
        from services.ayue_agent.v3.debug_trace import get_run

        ctx = AgentTurnContext(user_id="owner", room_id="room", message="嗨")
        plan = Plan(mode="direct_chat", tasks=[], direct_reply="嗨～怎麼啦？")
        planner_metrics = _planner_metrics()
        planner_metrics.decision_mode = "direct_chat"
        planner_metrics.prompt_raw = "planner prompt"
        planner_metrics.tool_calls_raw = [{"name": "decompose_tasks", "arguments": {}}]
        planner_metrics.tools_raw = [{"type": "function"}]
        with patch.dict("os.environ", {
            "AYUE_LOCAL_DEBUG_TRACE": "on",
            "AYUE_V3_SIMPLE_CHAT_FAST_PATH": "on",
        }), \
             patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, planner_metrics)), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.active_guidance_offer", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.active_assessment_session", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.awaiting_assessment_commit", return_value=None), \
             patch("services.ayue_agent.v3.scheduler._direct_chat_block_reason", return_value=None):
            mock_build.return_value = MagicMock()
            result = run_public_agent_turn_v3(ctx, debug_enabled=True)
            debug_run = get_run(result.agent_run_id, "owner")
        plan_event = next(event for event in debug_run["events"] if event["type"] == "plan_created")
        self.assertEqual(plan_event["mode"], "direct_chat")
        self.assertEqual(plan_event["execution_mode"], "direct_chat")
        self.assertEqual(plan_event["direct_reply"], result.reply)
        self.assertTrue(any(
            event["type"] == "direct_reply_selected" and event.get("mode") == "direct_chat"
            for event in debug_run["events"]
        ))
        self.assertFalse(any(event["type"] == "subagent_started" for event in debug_run["events"]))

    def test_local_debug_trace_records_planner_dag_and_synthesizer(self):
        from services.ayue_agent.v3.debug_trace import get_run

        ctx = AgentTurnContext(user_id="owner", room_id="room", message="嗨")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="synthesizer", depends_on=[], task_brief="回覆問候"),
        ])
        planner_metrics = _planner_metrics()
        planner_metrics.prompt_raw = "planner prompt"
        planner_metrics.tool_calls_raw = [{"name": "decompose_tasks", "arguments": {}}]
        planner_metrics.tools_raw = [{"type": "function"}]
        synth_metrics = _synth_metrics()
        synth_metrics.prompt_raw = "synth prompt"
        synth_metrics.input_payload = {"message": "嗨"}
        synth_metrics.reply_source = "llm"
        synth_metrics.used_llm = True
        with patch.dict("os.environ", {"AYUE_LOCAL_DEBUG_TRACE": "on"}), \
             patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, planner_metrics)), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("你好", None, synth_metrics)):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda **_kwargs: {})
            result = run_public_agent_turn_v3(ctx, debug_enabled=True)
            debug_run = get_run(result.agent_run_id, "owner")
        event_types = [event["type"] for event in debug_run["events"]]
        self.assertEqual(debug_run["status"], "completed")
        self.assertIn("planner_completed", event_types)
        self.assertIn("plan_created", event_types)
        self.assertIn("subagent_finished", event_types)
        self.assertEqual(debug_run["events"][-1]["type"], "final")
        synth_event = next(
            event for event in debug_run["events"]
            if event["type"] == "subagent_finished" and event.get("agent") == "synthesizer"
        )
        self.assertEqual(synth_event["status"], "ok")
        self.assertEqual(synth_event["reply_source"], "llm")
        self.assertTrue(synth_event["used_llm"])
        self.assertEqual(synth_event["input_payload"], {"message": "嗨"})
        self.assertEqual(synth_event["results"][0]["reply"], result.reply)

    def test_local_debug_trace_marks_synth_fallback_as_degraded(self):
        from services.ayue_agent.v3.debug_trace import get_run

        ctx = AgentTurnContext(user_id="owner", room_id="room", message="你可以幹嘛")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="synthesizer", depends_on=[], task_brief="回覆能力問題"),
        ])
        synth_metrics = _synth_metrics()
        synth_metrics.reply_source = "general_fallback"
        synth_metrics.fallback_reason = "provider_error"
        synth_metrics.error_code = "synthesizer_provider_error"
        with patch.dict("os.environ", {"AYUE_LOCAL_DEBUG_TRACE": "on"}), \
             patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.synthesizer.synthesize",
                   return_value=("我剛剛沒接好，但你不用整段重講。把最想先說的那一點丟給我，我從那裡接。", None, synth_metrics)):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda **_kwargs: {})
            result = run_public_agent_turn_v3(ctx, debug_enabled=True)
            debug_run = get_run(result.agent_run_id, "owner")
        synth_event = next(
            event for event in debug_run["events"]
            if event["type"] == "subagent_finished" and event.get("agent") == "synthesizer"
        )
        self.assertEqual(synth_event["status"], "degraded")
        self.assertEqual(synth_event["fallback_reason"], "provider_error")
        self.assertEqual(synth_event["error_code"], "synthesizer_provider_error")
        self.assertEqual(synth_event["results"][0]["reply"], result.reply)

    def test_trace_persisted_with_allowlisted_fields(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="嗨")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="查行程"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "calendar": MagicMock(return_value=(
                     [ToolProposal(tool_name="calendar.list_my_events", arguments={})], _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool",
                   return_value=MagicMock(ok=True, data={"events": []}, error_code=None)), \
             patch("services.ayue_agent.v3.synthesizer.synthesize",
                   return_value=("行程ok", None, _synth_metrics())), \
             patch("services.ayue_agent.v3.scheduler._persist_trace") as persist:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value.user_id = "owner"
            run_public_agent_turn_v3(ctx)
        self.assertEqual(persist.call_count, 1)
        payload = persist.call_args.args[2]
        self.assertIn("plan", payload)
        self.assertIn("tool_results", payload)
        self.assertIn("event_sequence", payload)
        self.assertNotIn("message", payload)
        self.assertNotIn("prompt", payload)


class V3SchedulerOpportunityTests(unittest.TestCase):
    def test_accepting_soft_offer_enters_normal_match_confirmation(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="好")
        synth_metrics = _synth_metrics()
        with patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.active_guidance_offer",
                   return_value={"fingerprint": "fp1", "expires_at": 1e18}), \
             patch("services.ayue_agent.v3.scheduler.accept_guidance_offer", return_value=True), \
             patch("services.ayue_agent.v3.scheduler.prepare_write_confirmation",
                   return_value=({"data": {}, "arguments": {}}, "要依你的近況開始找合適人選嗎？")), \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.insert_one") as insert, \
             patch("services.ayue_agent.v3.synthesizer.synthesize",
                   return_value=("要依你的近況開始找合適人選嗎？", None, synth_metrics)):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value.user_profile = {}
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        self.assertEqual(result.conversation_intent, "match_confirmation")
        insert.assert_called_once()

    def test_social_opening_creates_soft_guidance_without_confirmation(self):
        from services.ayue_agent.v3.contracts import OpportunitySignal
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="一個人去有點孤單")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="synthesizer", depends_on=[], task_brief="回覆"),
        ], opportunity=OpportunitySignal(signal="social_opening", evidence_span="一個人去有點孤單", confidence=0.9))
        seen_obs = {}
        def fake_synth(slice_payload, candidate_cards=None):
            seen_obs["observations"] = slice_payload.payload.get("observations", [])
            return ("好", None, _synth_metrics())
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.assess_match_opportunity") as assess, \
             patch("services.ayue_agent.v3.scheduler.claim_guidance_offer", return_value=True), \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.insert_one") as insert, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", side_effect=fake_synth):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value.user_profile = {}
            mock_build.return_value.message = "一個人去有點孤單"
            assess.return_value = MagicMock(state="ready", reason_codes=(), fingerprint="fp1")
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        insert.assert_not_called()
        self.assertTrue(result.match_guidance_shown)
        obs = seen_obs.get("observations", [])
        self.assertTrue(any(
            isinstance(o.get("result"), dict)
            and o["result"].get("match_opportunity_offer")
            for o in obs
        ))

    def test_social_opening_not_ready_does_not_start_match(self):
        from services.ayue_agent.v3.contracts import OpportunitySignal
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="一個人去有點孤單")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="synthesizer", depends_on=[], task_brief="回覆"),
        ], opportunity=OpportunitySignal(signal="social_opening", evidence_span="一個人去有點孤單", confidence=0.9))
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.assess_match_opportunity") as assess:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value.user_profile = {}
            mock_build.return_value.message = "一個人去有點孤單"
            assess.return_value = MagicMock(state="not_ready", reason_codes=("profile_basis_insufficient",),
                                            missing_basis=("preferences",))
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        self.assertIsNone(result.match_readiness_state)
        self.assertFalse(result.match_guidance_shown)


class V3SchedulerAssessmentTests(unittest.TestCase):
    def test_typed_assessment_cancel_cancels_active_without_planner(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room", message="退出測驗", assessment_action="cancel",
        )
        session = {
            "session_id": "s1", "kind": "deep_profile", "status": "active",
            "expires_at": 1e18, "revision": 4,
        }
        with patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.awaiting_assessment_commit", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.active_assessment_session", return_value=session), \
             patch("services.ayue_agent.v3.scheduler.cancel_assessment_session", return_value={
                 "status": "cancelled", "session_state": "cancelled", "kind": "deep_profile",
                 "revision": 5, "reply": "這段探索已取消。",
             }) as cancel, \
             patch("services.ayue_agent.v3.scheduler.plan_turn") as plan:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value.user_profile = {}
            result = run_public_agent_turn_v3(ctx)
        self.assertEqual(result.assessment_state, "cancelled")
        self.assertEqual(result.assessment_kind, "deep_profile")
        cancel.assert_called_once_with("owner", "s1", "deep_profile")
        plan.assert_not_called()

    def test_typed_assessment_cancel_handles_awaiting_commit_without_planner(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room", message="退出測驗", assessment_action="cancel",
        )
        session = {
            "session_id": "s1", "kind": "big_five", "status": "awaiting_commit",
            "expires_at": 1e18, "revision": 3,
        }
        with patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.awaiting_assessment_commit", return_value=session), \
             patch("services.ayue_agent.v3.scheduler.active_assessment_session", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.cancel_assessment_session", return_value={
                 "status": "cancelled", "session_state": "cancelled", "kind": "big_five",
                 "revision": 4, "reply": "這段探索已取消。",
             }) as cancel, \
             patch("services.ayue_agent.v3.scheduler.plan_turn") as plan:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value.user_profile = {}
            result = run_public_agent_turn_v3(ctx)
        self.assertEqual(result.assessment_state, "cancelled")
        cancel.assert_called_once_with("owner", "s1", "big_five")
        plan.assert_not_called()

    def test_typed_assessment_cancel_without_session_is_deterministic(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room", message="退出測驗", assessment_action="cancel",
        )
        with patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.awaiting_assessment_commit", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.active_assessment_session", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.plan_turn") as plan:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value.user_profile = {}
            result = run_public_agent_turn_v3(ctx)
        self.assertEqual(result.reply, "目前沒有正在進行的測驗。")
        self.assertIsNone(result.assessment_state)
        plan.assert_not_called()

    def test_typed_assessment_cancel_expires_stale_session_without_cancel_overwrite(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room", message="退出測驗", assessment_action="cancel",
        )
        session = {
            "session_id": "s1", "kind": "big_five", "status": "active",
            "expires_at": 1, "revision": 2,
        }
        with patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.awaiting_assessment_commit", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.active_assessment_session", return_value=session), \
             patch("services.ayue_agent.v3.scheduler.expire_assessment_session", return_value={
                 "status": "expired", "session_state": "expired", "kind": "big_five",
                 "revision": 3, "reply": "這段探索已過期。",
             }) as expire, \
             patch("services.ayue_agent.v3.scheduler.cancel_assessment_session") as cancel, \
             patch("services.ayue_agent.v3.scheduler.plan_turn") as plan:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value.user_profile = {}
            result = run_public_agent_turn_v3(ctx)
        self.assertEqual(result.assessment_state, "expired")
        expire.assert_called_once_with("owner", "s1", "big_five")
        cancel.assert_not_called()
        plan.assert_not_called()

    def test_active_assessment_advances_without_planner(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="我喜歡戶外活動")
        with patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.active_assessment_session",
                   return_value={"session_id": "s1", "kind": "big_five", "expires_at": 1e18, "revision": 1}), \
             patch("services.ayue_agent.v3.scheduler.advance_assessment_session",
                   return_value={"status": "active", "session_state": "active", "kind": "big_five",
                                 "revision": 2, "reply": "好的，那假日你通常怎麼安排？"}), \
             patch("services.ayue_agent.v3.scheduler.plan_turn") as plan:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        self.assertEqual(result.assessment_state, "active")
        self.assertEqual(result.assessment_kind, "big_five")
        plan.assert_not_called()

    def test_awaiting_commit_confirm_commits(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="確認")
        with patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.awaiting_assessment_commit",
                   return_value={"session_id": "s1", "kind": "big_five", "revision": 3, "expires_at": 1e18}), \
             patch("services.ayue_agent.v3.scheduler.assessment_commit_choice", return_value="confirm"), \
             patch("services.ayue_agent.v3.scheduler.commit_assessment_session",
                   return_value={"status": "committed", "session_state": "completed",
                                 "kind": "big_five", "revision": 4, "reply": "已套用新的基本性格資料。"}), \
             patch("services.ayue_agent.v3.scheduler.plan_turn") as plan:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        self.assertEqual(result.assessment_state, "completed")
        plan.assert_not_called()


class V3SchedulerMetadataTests(unittest.TestCase):
    def test_sources_and_llm_metrics_populated(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="查一下附近餐廳")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="places", depends_on=[], task_brief="找餐廳"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "places": MagicMock(return_value=(
                     [ToolProposal(tool_name="places.search_nearby",
                                   arguments={"anchor": "中壢", "categories": ["restaurant"]})], _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool",
                   return_value=MagicMock(ok=True, data={
                       "places": [{"name": "店A", "map_url": "https://www.openstreetmap.org/?mlat=25&mlon=121#map=18/25/121"}],
                   }, error_code=None)), \
             patch("services.ayue_agent.v3.synthesizer.synthesize",
                   return_value=("找到店A", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.sources)
        self.assertEqual(result.sources[0]["title"], "店A")
        self.assertTrue(result.llm_call_metrics)
        self.assertIn("input_tokens", result.llm_call_metrics[0])


class V3SchedulerReuseTests(unittest.TestCase):
    def test_duplicate_distance_within_task_reuses_observation(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="中壢到台北多遠")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="places", depends_on=[], task_brief="量距離"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        executed: list[str] = []
        def fake_exec(call, ctx, clock=None):
            executed.append(call.name)
            return MagicMock(ok=True, data={
                "origin_label": "中壢", "destination_label": "台北",
                "origin_kind": "explicit", "distance_m": 40000,
                "distance_basis": "straight_line", "attribution": "OSM", "attribution_url": "https://x",
            }, error_code=None)
        multi = [
            ToolProposal(tool_name="places.measure_distance",
                         arguments={"origin": "中壢", "destination": "台北"}),
            ToolProposal(tool_name="places.measure_distance",
                         arguments={"origin": "中壢市", "destination": "台北市"}),
        ]
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "places": MagicMock(return_value=(multi, _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", side_effect=fake_exec), \
             patch("services.ayue_agent.v3.synthesizer.synthesize",
                   return_value=("約 40 公里", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            run_public_agent_turn_v3(ctx)
        self.assertEqual(len(executed), 1, "paraphrased distance call must reuse the first observation")

    def test_web_extract_url_not_bound_fails(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="幫我看看這個網頁")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="places", depends_on=[], task_brief="讀網頁"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        seen_obs = {}
        def fake_synth(slice_payload, candidate_cards=None):
            seen_obs["observations"] = slice_payload.payload.get("observations", [])
            return ("沒查到", None, _synth_metrics())
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "places": MagicMock(return_value=(
                     [ToolProposal(tool_name="web.extract",
                                   arguments={"urls": ["https://evil.example.com/x"]})], _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool") as mock_exec, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", side_effect=fake_synth):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value.message = "幫我看看這個網頁"
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        mock_exec.assert_not_called()
        obs = seen_obs.get("observations", [])
        self.assertEqual(obs[0]["status"], "failed")
        self.assertEqual(obs[0]["error_code"], "web_extract_url_not_bound")


if __name__ == "__main__":
    unittest.main()
