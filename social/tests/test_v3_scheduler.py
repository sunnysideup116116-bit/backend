# tests/test_v3_scheduler.py
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from services.ayue_agent.contracts import AgentTurnContext, AgentResult, PublicAgentTurnContext, TurnClockV1
from services.ayue_agent.v3.calendar_commands import CalendarCommand
from services.ayue_agent.v3.calendar_drafts import clear_draft, get_draft, public_projection
from services.ayue_agent.v3.sub_agents.calendar_agent import CalendarAgentResult
from services.ayue_agent.v3 import calendar_runtime
from services.ayue_agent.v3.contracts import Plan, SubTask, SubTaskResult, SubTaskStatus, ToolProposal, RunCondition
from services.ayue_agent.v3.planner import PlannerMetrics
from services.ayue_agent.v3.confirmation import ConfirmationManager
from services.ayue_agent.v3.synthesizer import SynthesizerMetrics
from services.ayue_agent.v3.place_references import (
    clear_runtime_state as clear_place_reference_state,
    get_candidate_set as get_place_candidate_set,
    replace_presented_candidates,
)
from services.ayue_agent.v3.sub_agents.base import SubAgentMetrics
from services.ai_service import ToolCallResult
from services.ayue_agent.v3.scheduler import (
    _apply_card_decision, _assessment_start_confirmation_requested,
    _direct_chat_block_reason, _prior_observations_for, _public_place_cards,
    _resolve_presentation_blocks,
    _condition_skip_reason, _topological_layers,
    run_public_agent_turn_v3,
)


def _sub_metrics():
    return SubAgentMetrics(input_tokens=10, output_tokens=20, duration_ms=100, llm_call_count=1)


def _proposal(tool, args=None):
    return [ToolProposal(tool_name=tool, arguments=args or {})]


def _planner_metrics():
    return PlannerMetrics(input_tokens=50, output_tokens=60, duration_ms=200, llm_call_count=1)


def _synth_metrics():
    return SynthesizerMetrics(input_tokens=30, output_tokens=40, duration_ms=150)


def _place_cards(*names):
    return [
        {
            "name": name,
            "category": "cafe",
            "address_summary": "高雄市鹽埕區",
            "distance_m": index * 100,
            "map_url": f"https://www.google.com/maps/place/{index}",
            "provider": "google",
            "place_id": f"ChIJplace{index}",
        }
        for index, name in enumerate(names, start=1)
    ]


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

    def test_calendar_busy_skips_hard_gated_task_without_runner(self):
        calendar = SubTask(
            id="c1", agent="calendar", depends_on=[], task_brief="查詢",
            outcome_contract="calendar.availability.v1",
        )
        web = SubTask(
            id="w1", agent="web", depends_on=[], task_brief="找活動",
            run_if=RunCondition(source_task_id="c1", required_outcome="calendar.no_scheduled_events"),
        )
        synth = SubTask(id="s1", agent="synthesizer", depends_on=["w1"], task_brief="彙整")
        plan = Plan(tasks=[calendar, web, synth])
        results = {
            "c1": [SubTaskResult(
                task_id="c1", status=SubTaskStatus.OK,
                outcome_codes=["calendar.has_scheduled_events"],
            )],
        }
        self.assertEqual(_condition_skip_reason(plan.tasks[1], results), "condition_not_met")
        self.assertEqual([t.id for layer in _topological_layers(plan) for t in layer], ["c1", "w1", "s1"])

    def test_calendar_free_satisfies_hard_gate_and_task_finished_allows_failure(self):
        calendar = SubTask(
            id="c1", agent="calendar", depends_on=[], task_brief="查詢",
            outcome_contract="calendar.availability.v1",
        )
        web = SubTask(
            id="w1", agent="web", depends_on=[], task_brief="找活動",
            run_if=RunCondition(source_task_id="c1", required_outcome="calendar.no_scheduled_events"),
        )
        ordinary = SubTask(
            id="r1", agent="relationship", depends_on=[], task_brief="列出聯絡人",
            run_if=RunCondition(source_task_id="c1", required_outcome="task.finished"),
        )
        synth = SubTask(id="s1", agent="synthesizer", depends_on=["w1", "r1"], task_brief="彙整")
        plan = Plan(tasks=[calendar, web, ordinary, synth])
        free = {"c1": [SubTaskResult(
            task_id="c1", status=SubTaskStatus.OK,
            outcome_codes=["calendar.no_scheduled_events"],
        )]}
        failed = {"c1": [SubTaskResult(task_id="c1", status=SubTaskStatus.FAILED, error_code="calendar_denied")]}
        self.assertIsNone(_condition_skip_reason(plan.tasks[1], free))
        self.assertIsNone(_condition_skip_reason(plan.tasks[2], failed))
        mixed = {
            "c1": [
                free["c1"][0],
                SubTaskResult(task_id="c1", status=SubTaskStatus.FAILED, error_code="calendar_timeout"),
            ]
        }
        self.assertEqual(_condition_skip_reason(plan.tasks[1], mixed), "condition_unavailable")

    def test_calendar_availability_result_emits_closed_outcome_without_replacing_events(self):
        task = SubTask(
            id="c1", agent="calendar", depends_on=[], task_brief="查詢",
            outcome_contract="calendar.availability.v1",
        )
        results = [SubTaskResult(
            task_id="c1", status=SubTaskStatus.OK,
            tool_name="calendar.list_my_events",
            observation={"events": [], "range": "2026-08-15"},
        )]
        projected = calendar_runtime._attach_availability_outcome(task, results)
        self.assertEqual(projected[0].outcome_codes, ["calendar.no_scheduled_events"])
        self.assertEqual(projected[0].observation["events"], [])
        self.assertEqual(projected[0].observation["availability"]["event_count"], 0)

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
        self.assertEqual(result.llm_call_metrics[0]["call_count"], 1)
        self.assertEqual(result.llm_call_metrics[0]["requested_model_tier"], "fast")
        trace = persist_trace.call_args.args[2]
        self.assertEqual(trace["execution_mode"], "direct_chat")
        self.assertEqual(trace["llm_call_count"], 1)
        synth.assert_not_called()

    def test_stream_tokens_replay_only_the_normalized_final_reply(self):
        ctx = self._ctx("哈囉")
        plan = Plan(mode="direct_chat", tasks=[], direct_reply="这是簡體中文。")
        tokens: list[str] = []
        with patch.dict("os.environ", {"AYUE_V3_SIMPLE_CHAT_FAST_PATH": "on"}), \
             patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context",
                   return_value=self._direct_turn()), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.active_guidance_offer", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.active_assessment_session", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.awaiting_assessment_commit", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.time.sleep"), \
             patch("services.ayue_agent.v3.scheduler._persist_trace"):
            result = run_public_agent_turn_v3(ctx, on_token=tokens.append)
        self.assertEqual(result.reply, "這是簡體中文。")
        self.assertTrue(tokens)
        self.assertEqual("".join(tokens), result.reply)
        self.assertTrue(all(len(fragment) <= 120 for fragment in tokens))

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
             patch("services.ayue_agent.v3.calendar_runtime.run_calendar", side_effect=runner), \
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

    def test_places_failure_observation_reaches_synthesizer_with_safe_context(self):
        ctx = self._ctx("鹽埕埔站附近的餐廳")
        plan = Plan(tasks=[
            SubTask(id="places1", agent="places", depends_on=[], task_brief="搜尋鹽埕埔站附近餐廳"),
            SubTask(id="synth", agent="synthesizer", depends_on=["places1"], task_brief="整理結果"),
        ])
        seen_observations = {}
        failure_data = {
            "failure": {
                "code": "location_not_found",
                "subject": "鹽埕埔站",
                "message": "無法解析這個地點",
            },
        }

        def fake_synth(slice_payload, candidate_cards=None, on_token=None):
            seen_observations["items"] = slice_payload.payload.get("observations", [])
            return ("目前無法解析鹽埕埔站", None, _synth_metrics())

        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "places": MagicMock(return_value=(
                     _proposal("places.search_nearby", {
                         "anchor": "鹽埕埔站", "categories": ["restaurant"],
                     }),
                     _sub_metrics(),
                 )),
             }), \
             patch(
                 "services.ayue_agent.v3.scheduler.execute_tool",
                 return_value=MagicMock(
                     ok=False,
                     data=failure_data,
                     error_code="location_not_found",
                 ),
             ), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", side_effect=fake_synth):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value._mentioned_ids = []
            result = run_public_agent_turn_v3(ctx)

        self.assertTrue(result.handled)
        observation = next(item for item in seen_observations["items"] if item["task_id"] == "places1")
        self.assertEqual(observation["status"], "failed")
        self.assertEqual(observation["tool"], "places.search_nearby")
        self.assertEqual(observation["error_code"], "location_not_found")
        self.assertEqual(observation["result"], failure_data)
        self.assertIn("鹽埕埔站", str(observation["result"]))
        self.assertNotIn("ObjectId", str(observation["result"]))
        self.assertNotIn("Traceback", str(observation["result"]))

    def test_presented_places_replace_server_owned_set_in_public_order(self):
        clear_place_reference_state()
        ctx = self._ctx("幫我找附近飲料")
        places = _place_cards("樺達奶茶", "不二 TEA&NO.1", "鹽埕小熊奶茶")
        plan = Plan(tasks=[
            SubTask(id="places1", agent="places", depends_on=[], task_brief="搜尋附近飲料"),
            SubTask(id="synth", agent="synthesizer", depends_on=["places1"], task_brief="整理結果"),
        ])

        def fake_synth(slice_payload, candidate_cards=None, on_token=None):
            metrics = _synth_metrics()
            metrics.presented_candidate_refs = [
                candidate_cards[1]["candidate_ref"],
                candidate_cards[0]["candidate_ref"],
                candidate_cards[2]["candidate_ref"],
            ]
            return ("1. 不二 TEA&NO.1\n2. 樺達奶茶\n3. 鹽埕小熊奶茶", None, metrics)

        try:
            with patch(
                "services.ayue_agent.v3.scheduler.plan_turn",
                return_value=(plan, _planner_metrics()),
            ), patch(
                "services.ayue_agent.v3.scheduler.build_public_agent_turn_context",
                return_value=self._direct_turn(ctx.message),
            ), patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                "places": MagicMock(return_value=(
                    _proposal("places.search_nearby", {
                        "anchor": "高雄", "categories": ["cafe"],
                    }),
                    _sub_metrics(),
                )),
            }), patch(
                "services.ayue_agent.v3.scheduler.execute_tool",
                return_value=MagicMock(
                    ok=True,
                    data={
                        "places": places,
                        "attribution": "Google Maps",
                        "attribution_url": "https://www.google.com/maps",
                    },
                    error_code=None,
                ),
            ), patch(
                "services.ayue_agent.v3.synthesizer.synthesize",
                side_effect=fake_synth,
            ):
                result = run_public_agent_turn_v3(ctx)
            self.assertTrue(result.handled)
            record = get_place_candidate_set("owner", "room")
            self.assertEqual(
                [item["label"] for item in record["candidates"]],
                ["不二 TEA&NO.1", "樺達奶茶", "鹽埕小熊奶茶"],
            )
            self.assertNotIn("provider_place_id", str(record["candidates"][0]["reference"]))
        finally:
            clear_place_reference_state()

    def test_repaired_places_plan_runs_normal_places_synthesizer_trajectory(self):
        """A Planner compatibility repair must not become planner_invalid fallback."""
        ctx = self._ctx("找附近適合約會的地方")
        plan = Plan(tasks=[
            SubTask(id="places1", agent="places", depends_on=[], task_brief="找附近地方"),
            SubTask(id="synth", agent="synthesizer", depends_on=["places1"], task_brief="整理回覆"),
        ])
        planner_metrics = _planner_metrics()
        planner_metrics.attempts = [{
            "attempt": 1,
            "status": "repaired",
            "failure_code": "",
            "repair_codes": ["non_web_evidence_policy_removed"],
        }]
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, planner_metrics)), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "places": MagicMock(return_value=(
                     _proposal("places.search_nearby", {
                         "anchor": "高雄", "categories": ["restaurant"],
                     }),
                     _sub_metrics(),
                 )),
             }), \
             patch(
                 "services.ayue_agent.v3.scheduler.execute_tool",
                 return_value=MagicMock(
                     ok=True,
                     data={"places": [{"name": "一間餐廳", "category": "restaurant"}]},
                     error_code=None,
                 ),
             ) as execute_tool, \
             patch(
                 "services.ayue_agent.v3.synthesizer.synthesize",
                 return_value=("這裡有一間可以看看。", None, _synth_metrics()),
             ) as synthesize:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx)

        self.assertTrue(result.handled)
        self.assertIsNone(result.fallback_reason)
        execute_tool.assert_called_once()
        synthesize.assert_called_once()

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

    def test_planner_unexpected_exception_fails_closed_before_any_tool(self):
        ctx = self._ctx("第二個好了，幫我安排明天 5 點到 6 點")
        with patch(
            "services.ayue_agent.v3.scheduler.plan_turn",
            side_effect=RuntimeError("private provider payload"),
        ), patch(
            "services.ayue_agent.v3.scheduler.build_public_agent_turn_context",
        ) as mock_build, patch(
            "services.ayue_agent.v3.scheduler.execute_tool",
        ) as execute_tool, patch(
            "services.ayue_agent.v3.scheduler.execute_write",
        ) as execute_write, patch(
            "services.ayue_agent.v3.synthesizer.synthesize",
        ) as synthesize, patch(
            "services.ayue_agent.v3.scheduler._LOGGER.error",
        ) as log_error:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx)

        self.assertTrue(result.handled)
        self.assertEqual(result.fallback_reason, "planner_invalid")
        self.assertNotIn("卡住了", result.reply)
        execute_tool.assert_not_called()
        execute_write.assert_not_called()
        synthesize.assert_not_called()
        log_error.assert_called_once()
        self.assertIn("RuntimeError", repr(log_error.call_args))
        self.assertNotIn("private provider payload", repr(log_error.call_args))

    def test_match_planner_failure_returns_closed_match_clarification_and_trace(self):
        ctx = self._ctx("我要怎麼配對人？")
        turn = PublicAgentTurnContext(
            user_id="owner", room_id="room", message=ctx.message,
            match_search={"status": "idle", "cancellable": False},
        )
        metrics = _planner_metrics()
        metrics.failure_code = "invalid_arguments"
        metrics.retry_count = 1
        metrics.retry_reason = "invalid_arguments"
        metrics.attempts = [{
            "attempt": 2, "status": "protocol_error",
            "failure_code": "invalid_arguments",
            "validation_fields": ["write_intent"],
            "repair_codes": [],
            "raw_content": "must not persist",
        }]
        with patch(
            "services.ayue_agent.v3.scheduler.plan_turn",
            return_value=(None, metrics),
        ), patch(
            "services.ayue_agent.v3.scheduler.build_public_agent_turn_context",
            return_value=turn,
        ), patch(
            "services.ayue_agent.v3.scheduler._persist_trace",
        ) as persist:
            result = run_public_agent_turn_v3(ctx)

        self.assertIn("了解配對方式", result.reply)
        self.assertNotIn("沒接好", result.reply)
        trace = persist.call_args.args[2]
        self.assertEqual(trace["planner_failure"]["attempts"][0]["validation_fields"], ["write_intent"])
        self.assertNotIn("raw_content", trace["planner_failure"]["attempts"][0])

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
             patch("services.ayue_agent.v3.calendar_runtime.run_calendar", side_effect=runner), \
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
             patch("services.ayue_agent.v3.calendar_runtime.run_calendar", side_effect=runner), \
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
             patch("services.ayue_agent.v3.calendar_runtime.run_calendar", side_effect=runner), \
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
             patch("services.ayue_agent.v3.calendar_runtime.run_calendar",
                   return_value=(CalendarAgentResult(commands=[command]), _sub_metrics())), \
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

    def test_calendar_smoke_missing_end_time_reaches_clarification(self):
        """The exact smoke phrase must preserve the old end-time clarification."""
        clear_draft("owner")
        self.addCleanup(clear_draft, "owner")
        plan = Plan(tasks=[
            SubTask(id="c1", agent="calendar", depends_on=[], task_brief="幫我新增明天下午三點看牙醫"),
            SubTask(id="s1", agent="synthesizer", depends_on=["c1"], task_brief="彙整行事曆結果"),
        ])
        fixed_clock = TurnClockV1(
            timezone="Asia/Taipei", utc_iso="2026-08-10T04:00:00+00:00",
            local_iso="2026-08-10T12:00:00+08:00", local_date="2026-08-10",
            local_time="12:00", weekday_zh_tw="星期一",
            temporal_references={"明天": "2026-08-11"},
        )

        def build_context(raw_ctx, *, clock):
            return PublicAgentTurnContext(
                user_id=raw_ctx.user_id, room_id=raw_ctx.room_id,
                message=raw_ctx.message,
                calendar_draft=public_projection(get_draft(raw_ctx.user_id)),
                clock=clock,
            )

        provider_result = ToolCallResult(content="", tool_calls=[{
            "name": "calendar.submit_commands",
            "arguments": {"commands": [{
                "action": "create", "title": "看牙醫", "date": "明天",
                "start_time": "15:00",
            }]},
        }])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_turn_clock", return_value=fixed_clock), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context", side_effect=build_context), \
             patch("services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools", return_value=provider_result), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.create_confirmation", return_value={"confirmation_id": "c"}) as create_confirmation, \
             patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.calendar_commands.conflicts_for_viewer", return_value=[]), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=(
                 "請補上結束時間。", None, _synth_metrics(),
             )) as synth:
            result = run_public_agent_turn_v3(
                self._ctx("幫我新增明天下午三點看牙醫"), debug_enabled=True,
            )

        self.assertTrue(result.handled)
        create_confirmation.assert_not_called()
        observation = synth.call_args.args[0].payload["observations"][0]
        command_result = observation["result"]["calendar_command_result"]
        self.assertEqual(command_result["status"], "needs_clarification")
        self.assertEqual(command_result["clarification"]["missing_fields"], ["end_time"])
        self.assertEqual((get_draft("owner") or {}).get("missing_fields"), ["end_time"])

    def test_calendar_distinct_create_replaces_incomplete_create_draft(self):
        """A new create must not inherit fields from an earlier create draft."""
        clear_draft("owner")
        self.addCleanup(clear_draft, "owner")
        first_plan = Plan(tasks=[
            SubTask(id="c1", agent="calendar", depends_on=[], task_brief="幫我新增明天下午三點看牙醫"),
            SubTask(id="s1", agent="synthesizer", depends_on=["c1"], task_brief="彙整行事曆結果"),
        ])
        second_plan = Plan(tasks=[
            SubTask(id="c2", agent="calendar", depends_on=[], task_brief="下禮拜三去駁二看展 幫我加到行事曆"),
            SubTask(id="s2", agent="synthesizer", depends_on=["c2"], task_brief="彙整行事曆結果"),
        ])
        fixed_clock = TurnClockV1(
            timezone="Asia/Taipei", utc_iso="2026-08-10T04:00:00+00:00",
            local_iso="2026-08-10T12:00:00+08:00", local_date="2026-08-10",
            local_time="12:00", weekday_zh_tw="星期一",
            temporal_references={"明天": "2026-08-11", "下禮拜三": "2026-08-12"},
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
                    "action": "create", "title": "看牙醫", "date": "明天",
                    "start_time": "15:00",
                }]},
            }]),
            ToolCallResult(content="", tool_calls=[{
                "name": "calendar.submit_commands",
                "arguments": {"commands": [{
                    "action": "create", "title": "去駁二看展", "date": "下禮拜三",
                    # The provider copied the previous draft's time even
                    # though the new user request contains no time.
                    "start_time": "15:00",
                    "draft_mode": "continue",
                }]},
            }]),
        ]
        with patch("services.ayue_agent.v3.scheduler.plan_turn", side_effect=[
                 (first_plan, _planner_metrics()), (second_plan, _planner_metrics()),
             ]), \
             patch("services.ayue_agent.v3.scheduler.build_turn_clock", return_value=fixed_clock), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context", side_effect=build_context), \
             patch("services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools", side_effect=provider_results), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.create_confirmation", return_value={"confirmation_id": "c"}) as create_confirmation, \
             patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.calendar_commands.conflicts_for_viewer", return_value=[]), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=(
                 "請補上時間。", None, _synth_metrics(),
             )):
            first_result = run_public_agent_turn_v3(
                self._ctx("幫我新增明天下午三點看牙醫"), debug_enabled=True,
            )
            first_draft = get_draft("owner")
            second_result = run_public_agent_turn_v3(
                self._ctx("下禮拜三去駁二看展 幫我加到行事曆"), debug_enabled=True,
            )
            second_draft = get_draft("owner")

        self.assertTrue(first_result.handled)
        self.assertEqual((first_draft or {}).get("command", {}).get("title"), "看牙醫")
        self.assertEqual((first_draft or {}).get("command", {}).get("start_time"), "15:00")
        self.assertTrue(second_result.handled)
        command = (second_draft or {}).get("command", {})
        self.assertEqual(command.get("title"), "去駁二看展")
        self.assertEqual(command.get("date"), "2026-08-12")
        self.assertNotIn("start_time", command)
        self.assertNotIn("end_time", command)
        self.assertEqual((second_draft or {}).get("missing_fields"), ["start_time", "end_time"])
        self.assertEqual((second_draft or {}).get("resolved_target"), None)
        followup_projection = public_projection(second_draft)
        self.assertEqual(
            (followup_projection or {}).get("fields"),
            {"title": "去駁二看展", "date": "2026-08-12"},
        )
        create_confirmation.assert_not_called()

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
        def fake_synth(slice_payload, candidate_cards=None, on_token=None):
            seen_obs["observations"] = slice_payload.payload.get("observations", [])
            return ("好", None, _synth_metrics())
        def fake_runner(context_slice, *, task_brief):
            if task_brief == "查詢出國行程":
                return ([ToolProposal(tool_name="calendar.find_my_event",
                                      arguments={"event_hint": "出國"})], _sub_metrics())
            return ([], _sub_metrics())  # write task: no proposal
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.calendar_runtime.run_calendar",
                   side_effect=fake_runner), \
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

    def test_busy_calendar_hard_gate_does_not_start_downstream_runner(self):
        ctx = self._ctx("下週六有事就算了，沒事才幫我找活動")
        plan = Plan(tasks=[
            SubTask(
                id="c1", agent="calendar", depends_on=[], task_brief="查詢下週六行程",
                outcome_contract="calendar.availability.v1",
            ),
            SubTask(
                id="w1", agent="web", depends_on=[], task_brief="找活動",
                run_if={"source_task_id": "c1", "required_outcome": "calendar.no_scheduled_events"},
            ),
            SubTask(id="s1", agent="synthesizer", depends_on=["w1"], task_brief="彙整"),
        ])
        events: list[dict] = []
        calendar_runner = MagicMock(return_value=(
            [ToolProposal(tool_name="calendar.list_my_events", arguments={})], _sub_metrics(),
        ))
        web_runner = MagicMock(return_value=([], _sub_metrics()))
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch.dict("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "web": web_runner,
             }, clear=False), \
             patch("services.ayue_agent.v3.calendar_runtime.run_calendar", side_effect=calendar_runner), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", return_value=MagicMock(
                 ok=True, data={"events": [{"title": "已有安排"}], "range": "下週六"}, error_code=None,
             )), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("有事，先不安排", None, _synth_metrics())):
            mock_build.return_value = self._direct_turn(ctx.message)
            result = run_public_agent_turn_v3(ctx, on_progress=events.append)
        self.assertTrue(result.handled)
        web_runner.assert_not_called()
        self.assertFalse(any(
            event.get("type") == "tool_started" and event.get("task_id") == "w1"
            for event in events
        ))

    def test_product_info_progress_events_are_user_visible(self):
        ctx = self._ctx("所以一定會配到剛好要跟我做同一件事的人嗎")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="product_info", depends_on=[], task_brief=ctx.message),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        events: list[dict] = []

        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context",
                   return_value=self._direct_turn(ctx.message)), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.active_guidance_offer", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.active_assessment_session", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.awaiting_assessment_commit", return_value=None), \
             patch("services.ayue_agent.v3.synthesizer.synthesize",
                   return_value=("不一定。", None, _synth_metrics())):
            run_public_agent_turn_v3(ctx, on_progress=events.append)

        product_started = [
            event for event in events
            if event.get("type") == "tool_started"
            and event.get("tool_name") == "product_info.process"
        ]
        product_finished = [
            event for event in events
            if event.get("type") == "tool_finished"
            and event.get("tool_name") == "product_info.process"
        ]
        self.assertEqual(len(product_started), 1)
        self.assertEqual(product_started[0]["text"], "我先整理一下產品資訊…")
        self.assertEqual(len(product_finished), 1)
        self.assertEqual(product_finished[0]["outcome"], "ok")

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

        def fake_synth(slice_payload, candidate_cards=None, on_token=None):
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

        def fake_synth(slice_payload, candidate_cards=None, on_token=None):
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

    def test_card_decision_browse_returns_all(self):
        cards = self._cards()
        self.assertEqual(
            _apply_card_decision(cards, {"mode": "show_all", "indices": [], "card_intent": "browse"}),
            cards,
        )

    def test_card_decision_none_returns_empty(self):
        self.assertEqual(_apply_card_decision(self._cards(), {"mode": "none", "indices": []}), [])

    def test_card_decision_select_filters_indices(self):
        result = _apply_card_decision(self._cards(), {"mode": "select", "indices": [0, 2]})
        self.assertEqual([c["name"] for c in result], ["店A", "店C"])

    def test_card_decision_select_out_of_range_returns_empty(self):
        result = _apply_card_decision(self._cards(), {"mode": "select", "indices": [99]})
        self.assertEqual(result, [])

    def test_card_decision_select_dedupes_indices(self):
        result = _apply_card_decision(self._cards(), {"mode": "select", "indices": [1, 1, 1]})
        self.assertEqual([c["name"] for c in result], ["店B"])

    def test_card_decision_none_decision_returns_empty(self):
        self.assertEqual(_apply_card_decision(self._cards(), None), [])

    def test_card_decision_missing_does_not_inject_candidates(self):
        cards = self._cards() + [{"name": "摨", "category": "park", "distance_label": "400 ?砍偕"}]
        self.assertEqual(_apply_card_decision(cards, None), [])

    def test_show_all_without_browse_intent_does_not_inject_candidates(self):
        cards = self._cards() + [{"name": "extra", "category": "park", "distance_label": "400"}]
        self.assertEqual(_apply_card_decision(cards, {"mode": "show_all", "indices": []}), [])
        self.assertEqual(len(_apply_card_decision(cards, {"mode": "show_all", "card_intent": "browse"})), 4)

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

    def test_internal_place_candidates_have_ephemeral_refs_but_public_cards_do_not(self):
        results = [self._place_result("restaurant", 1)]
        internal = _public_place_cards(results, run_id="run-1", include_internal=True)
        public = _public_place_cards(results)
        self.assertRegex(internal[0]["candidate_ref"], r"^place_candidate_[0-9a-f]{16}$")
        self.assertEqual(internal[0]["distance_m"], 100)
        self.assertNotIn("candidate_ref", public[0])
        self.assertNotIn("distance_m", public[0])


class V3SchedulerWriteTests(unittest.TestCase):
    def setUp(self):
        clear_place_reference_state()

    def tearDown(self):
        clear_place_reference_state()

    def _ctx(self, message="確認"):
        return AgentTurnContext(user_id="owner", room_id="room", message=message)

    def test_write_proposal_creates_confirmation_with_preview(self):
        ctx = self._ctx("幫我找人")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="match", match_intent="start_search", depends_on=[], task_brief="開始找人"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        seen_obs = {}
        def fake_synth(slice_payload, candidate_cards=None, on_token=None):
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
        self.assertEqual(
            insert.call_args.args[0]["interaction_mode"],
            "bubble_buttons_v1",
        )
        obs = seen_obs.get("observations", [])
        self.assertEqual(obs[0]["status"], "ok")
        self.assertTrue(obs[0]["result"]["pending_confirmation"])
        self.assertIn("確認", obs[0]["result"]["preview"])

    def test_agent_match_decision_uses_room_scoped_button_confirmation(self):
        ctx = self._ctx("接受此次配對")
        plan = Plan(tasks=[
            SubTask(id="m1", agent="match", match_intent="accept_proposal", depends_on=[], task_brief="接受目前提案"),
            SubTask(id="s1", agent="synthesizer", depends_on=["m1"], task_brief="呈現確認"),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "match": MagicMock(return_value=(
                     [ToolProposal(
                         tool_name="match.decide_active_proposal",
                         arguments={"decision": "interested"},
                     )],
                     _sub_metrics(),
                 )),
             }), \
             patch("services.ayue_agent.v3.scheduler.prepare_write_confirmation", return_value=(
                 {
                     "action": "match.decide_active_proposal",
                     "arguments": {"decision": "interested"},
                     "data": {"match_id": "match-1", "proposal_revision": 1},
                 },
                 "要接受目前提案嗎？確認後才會送出。",
             )), \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.update_many"), \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.insert_one") as insert, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=(
                 "要接受目前提案嗎？確認後才會送出。", None, _synth_metrics(),
             )):
            build.return_value = MagicMock()
            build.return_value.clock = MagicMock(model_dump=lambda: {})
            build.return_value._mentioned_ids = []
            build.return_value.room_id = "room"
            build.return_value.user_id = "owner"
            run_public_agent_turn_v3(ctx)

        record = insert.call_args.args[0]
        self.assertEqual(record["tool_name"], "match.decide_active_proposal")
        self.assertEqual(record["room_id"], "room")
        self.assertEqual(record["interaction_mode"], "bubble_buttons_v1")

    def test_explicit_arrange_place_continuation_reaches_confirmation_without_write(self):
        message = "第二個好了，幫我安排明天早上五點到六點"
        ctx = self._ctx(message)
        plan = Plan(tasks=[
            SubTask(
                id="c1", agent="calendar", depends_on=[],
                task_brief="從緊鄰候選清單解讀第二個並建立行程",
            ),
            SubTask(
                id="s1", agent="synthesizer", depends_on=["c1"],
                task_brief="呈現 server-owned confirmation preview",
            ),
        ])
        turn = PublicAgentTurnContext(
            user_id="owner", room_id="room", message=message,
            recent_messages=[{
                "role": "assistant",
                "content": "1. 樺達奶茶\n2. 不二 TEA&NO.1\n3. 鹽埕小熊奶茶",
            }],
            clock=TurnClockV1(
                timezone="Asia/Taipei", utc_iso="2026-08-25T12:00:00+00:00",
                local_iso="2026-08-25T20:00:00+08:00", local_date="2026-08-25",
                local_time="20:00", weekday_zh_tw="星期二",
                temporal_references={"明天": "2026-08-26"},
            ),
        )
        replace_presented_candidates(
            "owner", "room",
            _place_cards("樺達奶茶", "不二 TEA&NO.1", "鹽埕小熊奶茶"),
        )
        command = CalendarCommand(
            action="create", title="樺達奶茶", date="明天",
            start_time="05:00", end_time="06:00",
        )
        seen_observations = {}

        def fake_synth(slice_payload, candidate_cards=None, on_token=None):
            seen_observations["items"] = slice_payload.payload.get("observations", [])
            return ("請確認行程", None, _synth_metrics())

        with patch(
            "services.ayue_agent.v3.scheduler.plan_turn",
            return_value=(plan, _planner_metrics()),
        ), patch(
            "services.ayue_agent.v3.scheduler.build_public_agent_turn_context",
            return_value=turn,
        ), patch(
            "services.ayue_agent.v3.calendar_runtime.run_calendar",
            return_value=(CalendarAgentResult(commands=[command]), _sub_metrics()),
        ), patch(
            "services.ayue_agent.v3.scheduler.ConfirmationManager.list_active",
            return_value=[],
        ), patch(
            "services.ayue_agent.v3.scheduler.ConfirmationManager.create_confirmation",
            return_value={"confirmation_id": "calendar-confirmation"},
        ) as create_confirmation, patch(
            "services.ayue_agent.v3.calendar_commands.calendar_access_enabled",
            return_value=True,
        ), patch(
            "services.ayue_agent.v3.calendar_commands.conflicts_for_viewer",
            return_value=[],
        ), patch(
            "services.ayue_agent.v3.synthesizer.synthesize",
            side_effect=fake_synth,
        ), patch(
            "services.ayue_agent.v3.scheduler.execute_write",
        ) as execute_write:
            result = run_public_agent_turn_v3(ctx)

        self.assertTrue(result.handled)
        create_confirmation.assert_called_once()
        payload = create_confirmation.call_args.kwargs["payload"]
        self.assertEqual(payload["plans"][0]["action"], "create")
        self.assertEqual(payload["plans"][0]["form"]["title"], "不二 TEA&NO.1")
        self.assertEqual(payload["plans"][0]["form"]["location"], "不二 TEA&NO.1")
        self.assertEqual(payload["plans"][0]["form"]["date"], "2026-08-26")
        self.assertEqual(payload["plans"][0]["form"]["start_time"], "05:00")
        self.assertEqual(payload["plans"][0]["form"]["end_time"], "06:00")
        observations = seen_observations["items"]
        self.assertTrue(observations[0]["result"]["pending_confirmation"])
        execute_write.assert_not_called()

    def test_invalid_place_ordinal_fails_before_planner_or_write(self):
        replace_presented_candidates(
            "owner", "room",
            _place_cards("樺達奶茶", "不二 TEA&NO.1", "鹽埕小熊奶茶"),
        )
        ctx = self._ctx("第四個好了，幫我安排明天早上五點到六點")
        turn = PublicAgentTurnContext(
            user_id="owner", room_id="room", message=ctx.message,
            clock=TurnClockV1(
                timezone="Asia/Taipei", utc_iso="2026-08-25T12:00:00+00:00",
                local_iso="2026-08-25T20:00:00+08:00", local_date="2026-08-25",
                local_time="20:00", weekday_zh_tw="星期二",
            ),
        )
        with patch(
            "services.ayue_agent.v3.scheduler.build_public_agent_turn_context",
            return_value=turn,
        ), patch(
            "services.ayue_agent.v3.scheduler.plan_turn",
        ) as planner, patch(
            "services.ayue_agent.v3.scheduler.execute_write",
        ) as execute_write:
            result = run_public_agent_turn_v3(ctx)
        self.assertEqual(result.conversation_intent, "place_reference_clarification")
        self.assertIn("第 1 到第 3 個", result.reply)
        planner.assert_not_called()
        execute_write.assert_not_called()

    def test_confirm_path_executes_write_and_relays_reply(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room", message="",
            choice_id="c1", choice_action="confirm",
        )
        seen_obs = {}
        def fake_synth(slice_payload, candidate_cards=None, on_token=None):
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
                "presented_message_id": "message-1", "presented_at": 1,
                "room_id": "room", "surface": "public_ayue",
                "interaction_mode": "bubble_buttons_v1",
            }]
            coll.update_one.return_value = MagicMock(modified_count=1)
            result = run_public_agent_turn_v3(ctx)
        self.assertTrue(result.handled)
        self.assertTrue(result.match_state_changed)
        exec_write.assert_called_once()
        self.assertEqual(exec_write.call_args.kwargs["payload"]["_confirmation_id"], "c1")

    def test_confirm_without_pending_calendar_draft_fails_closed(self):
        ctx = self._ctx("對")
        turn = self._direct_turn_with_calendar_draft()
        plan = Plan(tasks=[SubTask(id="s1", agent="synthesizer", depends_on=[], task_brief="澄清")])
        with patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context", return_value=turn), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.active_guidance_offer", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.execute_write") as execute_write, \
             patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())) as plan_turn, \
             patch("services.ayue_agent.v3.scheduler.synthesizer.synthesize", return_value=("目前沒有明確待處理事項。", None, _synth_metrics())) as synthesize:
            result = run_public_agent_turn_v3(ctx)

        self.assertIn("目前沒有明確", result.reply)
        self.assertFalse(result.calendar_state_changed)
        execute_write.assert_not_called()
        plan_turn.assert_called_once()
        synthesize.assert_called_once()

    def test_bare_ack_without_typed_state_defers_to_planner_without_write(self):
        ctx = self._ctx("好")
        turn = MagicMock(calendar_draft=None, message="好", recent_messages=[])
        plan = Plan(tasks=[
            SubTask(id="s1", agent="synthesizer", depends_on=[], task_brief="回覆"),
        ])
        with patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context", return_value=turn), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.active_guidance_offer", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.execute_write") as execute_write, \
             patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())) as plan_turn, \
             patch("services.ayue_agent.v3.scheduler.synthesizer.synthesize", return_value=("請告訴我想處理的內容", None, _synth_metrics())) as synthesize:
            result = run_public_agent_turn_v3(ctx)

        self.assertEqual(result.reply, "請告訴我想處理的內容")
        execute_write.assert_not_called()
        plan_turn.assert_called_once()
        synthesize.assert_called_once()

    def test_bare_acknowledgement_after_places_retry_offer_reaches_planner(self):
        ctx = self._ctx("好")
        turn = MagicMock(
            calendar_draft=None,
            message="好",
            recent_messages=[{
                "role": "assistant",
                "content": "要不要重新找一次地點？",
            }],
        )
        plan = Plan(tasks=[
            SubTask(id="p1", agent="places", depends_on=[], task_brief="重新搜尋附近地點"),
            SubTask(id="s1", agent="synthesizer", depends_on=["p1"], task_brief="整理地點結果"),
        ])
        places_proposal = _proposal(
            "places.search_nearby",
            {"anchor": "高雄", "categories": ["cafe"]},
        )
        with patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context", return_value=turn), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.active_guidance_offer", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())) as plan_turn, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "places": MagicMock(return_value=(places_proposal, _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", return_value=MagicMock(
                 ok=True, data={"places": []}, error_code=None,
             )) as execute_tool, \
             patch("services.ayue_agent.v3.scheduler.synthesizer.synthesize", return_value=(
                 "這次沒有找到地點", None, _synth_metrics(),
             )), \
             patch("services.ayue_agent.v3.scheduler.execute_write") as execute_write:
            result = run_public_agent_turn_v3(ctx)

        self.assertTrue(result.handled)
        self.assertNotIn("目前沒有待確認的操作", result.reply)
        plan_turn.assert_called_once()
        execute_tool.assert_called_once()
        execute_write.assert_not_called()

    def test_cancel_without_any_pending_operation_fails_closed(self):
        ctx = self._ctx("取消")
        turn = MagicMock(calendar_draft=None)
        with patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context", return_value=turn), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.cancel_all") as cancel_all, \
             patch("services.ayue_agent.v3.scheduler.active_guidance_offer", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.plan_turn") as plan_turn, \
             patch("services.ayue_agent.v3.scheduler.synthesizer.synthesize") as synthesize:
            result = run_public_agent_turn_v3(ctx)

        self.assertIn("沒有待取消的操作", result.reply)
        self.assertIn("沒有執行任何變更", result.reply)
        cancel_all.assert_not_called()
        plan_turn.assert_not_called()
        synthesize.assert_not_called()

    def test_confirm_claim_lost_does_not_synthesize_success(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room", message="",
            choice_id="c1", choice_action="confirm",
        )
        pending = {
            "_id": "c1", "tool_name": "calendar.submit_commands",
            "arguments": {}, "payload": {},
        }
        turn = MagicMock(calendar_draft=None)
        with patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context", return_value=turn), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.record_for_choice", return_value=pending), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.execute_confirmed", return_value=[]), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.choice_projection", return_value={
                 "id": "c1", "state": "pending", "selected": None, "expires_at": 1e18,
             }), \
             patch("services.ayue_agent.v3.scheduler.active_guidance_offer", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.execute_write") as execute_write, \
             patch("services.ayue_agent.v3.scheduler.synthesizer.synthesize") as synthesize:
            result = run_public_agent_turn_v3(ctx)

        self.assertIn("沒有執行新的變更", result.reply)
        self.assertIn("另一個請求處理", result.reply)
        execute_write.assert_not_called()
        synthesize.assert_not_called()

    @staticmethod
    def _direct_turn_with_calendar_draft():
        turn = MagicMock()
        turn.calendar_draft = {
            "action": "create",
            "missing_fields": ["start_time", "end_time"],
        }
        return turn


class V3SchedulerTraceTests(unittest.TestCase):
    def test_local_debug_trace_exposes_planner_failure_and_total_calls(self):
        from services.ayue_agent.v3.debug_trace import get_run

        ctx = AgentTurnContext(user_id="owner", room_id="room", message="壞掉")
        planner_metrics = _planner_metrics()
        planner_metrics.llm_call_count = 2
        planner_metrics.retry_count = 1
        planner_metrics.retry_reason = "missing_tool_call"
        planner_metrics.failure_code = "missing_tool_call"
        with patch.dict("os.environ", {"AYUE_LOCAL_DEBUG_TRACE": "on"}), \
             patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(None, planner_metrics)), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda **_kwargs: {})
            result = run_public_agent_turn_v3(ctx, debug_enabled=True)
            debug_run = get_run(result.agent_run_id, "owner")

        planner_event = next(event for event in debug_run["events"] if event["type"] == "planner_completed")
        self.assertEqual(planner_event["status"], "failed")
        self.assertEqual(planner_event["failure_code"], "missing_tool_call")
        self.assertEqual(planner_event["retry_count"], 1)
        self.assertEqual(planner_event["metrics"]["llm_call_count"], 2)
        self.assertEqual(debug_run["events"][-1]["response"]["llm_call_count"], 2)

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
        self.assertEqual(plan_event["planner_metrics"]["call_count"], 1)
        self.assertTrue(plan_event["planner_metrics"]["model_name"])
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
        planner_event = next(event for event in debug_run["events"] if event["type"] == "planner_completed")
        self.assertEqual(planner_event["metrics"]["llm_call_count"], 1)
        self.assertTrue(planner_event["metrics"]["model_name"])
        self.assertEqual(planner_event["retry_count"], 0)
        synth_event = next(
            event for event in debug_run["events"]
            if event["type"] == "subagent_finished" and event.get("agent") == "synthesizer"
        )
        self.assertEqual(synth_event["status"], "ok")
        self.assertEqual(synth_event["reply_source"], "llm")
        self.assertTrue(synth_event["used_llm"])
        self.assertEqual(synth_event["input_payload"], {"message": "嗨"})
        self.assertEqual(synth_event["results"][0]["reply"], result.reply)
        self.assertEqual(synth_event["llm_call_count"], 1)
        self.assertTrue(synth_event["model_name"])
        final_event = debug_run["events"][-1]
        self.assertEqual(final_event["response"]["llm_call_count"], 2)
        self.assertEqual(len(final_event["response"]["llm_call_metrics"]), 2)

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
        def fake_synth(slice_payload, candidate_cards=None, on_token=None):
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
        ctx = AgentTurnContext(
            user_id="owner", room_id="room", message="",
            choice_id="assessment-choice", choice_action="confirm",
        )
        record = {
            "_id": "assessment-choice",
            "tool_name": "profile.commit_assessment",
            "arguments": {},
            "payload": {"session_id": "s1", "kind": "big_five", "revision": 3},
        }

        def execute_choice(**kwargs):
            ok, reply, error = kwargs["executor"](
                "profile.commit_assessment", {}, "owner", record["payload"],
            )
            return [{"ok": ok, "data": {"reply": reply}, "error_code": error}]

        with patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.record_for_choice", return_value=record), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.execute_confirmed", side_effect=execute_choice), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.choice_projection", return_value={
                 "id": "assessment-choice", "state": "confirmed", "selected": "confirm", "expires_at": 1e18,
             }), \
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
    def test_presentation_block_keeps_selected_card_only(self):
        blocks = _resolve_presentation_blocks(
            [{
                "message_index": 0,
                "markdown": "",
                "candidate_refs": ["place_candidate_a"],
            }],
            [{
                "candidate_ref": "place_candidate_a",
                "name": "店A",
                "category": "restaurant",
                "distance_label": "約 200 公尺",
            }],
            ["附近有一個候選。"],
        )
        card_block = next(block for block in blocks if block.place_card_indices)
        self.assertEqual(card_block.place_card_indices, [0])
        self.assertEqual(card_block.markdown, "")

    def test_presentation_block_without_synth_projection_does_not_invent_label(self):
        blocks = _resolve_presentation_blocks(
            [],
            [{
                "candidate_ref": "place_candidate_a",
                "name": "店A",
                "category": "restaurant",
                "distance_label": "約 200 公尺",
            }],
            ["附近有一個候選。"],
        )
        self.assertEqual(blocks, [])

    def test_disabled_place_cards_are_separate_from_web_sources_and_metrics_populated(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="查一下附近餐廳")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="places", depends_on=[], task_brief="找餐廳"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.public_place_cards_enabled", return_value=False), \
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
                    return_value=(
                        "找到店A",
                        {"mode": "select", "indices": [0], "card_intent": "explicit_set"},
                        _synth_metrics(),
                    )) as synth:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx)
        self.assertEqual(result.sources, [])
        self.assertEqual(result.place_cards, [])
        self.assertEqual(result.presentation_blocks, [])
        internal_cards = synth.call_args.kwargs["candidate_cards"]
        self.assertEqual(len(internal_cards), 1)
        self.assertTrue(internal_cards[0]["candidate_ref"].startswith("place_candidate_"))
        self.assertIn("map_url", internal_cards[0])
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
        def fake_synth(slice_payload, candidate_cards=None, on_token=None):
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
