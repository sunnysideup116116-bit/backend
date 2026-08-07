# social_demotest/tests/test_v3_scheduler.py
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from services.ayue_agent.contracts import AgentTurnContext, AgentResult
from services.ayue_agent.v3.contracts import Plan, SubTask, SubTaskResult, SubTaskStatus, ToolProposal
from services.ayue_agent.v3.planner import PlannerMetrics
from services.ayue_agent.v3.synthesizer import SynthesizerMetrics
from services.ayue_agent.v3.sub_agents.base import SubAgentMetrics
from services.ayue_agent.v3.scheduler import (
    _apply_card_decision, _prior_observations_for, _public_place_cards,
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

    def test_steak_example_full_flow(self):
        ctx = self._ctx("我下週五晚上想吃牛排，幫我找餐廳並看看我那天有沒有空")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="查詢使用者下週五晚上的行程空檔"),
            SubTask(id="t2", agent="places", depends_on=[], task_brief="搜尋附近的牛排餐廳"),
            SubTask(id="t3", agent="places", depends_on=["t2"], task_brief="依 t2 結果篩選推薦餐廳"),
            SubTask(id="t4", agent="synthesizer", depends_on=["t1", "t2", "t3"], task_brief="彙整"),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
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
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertIsInstance(result, AgentResult)
        self.assertTrue(result.handled)
        self.assertEqual(result.agent_mode, "v3")

    def test_failed_sub_agent_skipped_and_synthesizer_handles_gap(self):
        ctx = self._ctx("你好嗎")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="查詢本人行程"),
            SubTask(id="t2", agent="places", depends_on=[], task_brief="找地點"),
            SubTask(id="t3", agent="synthesizer", depends_on=["t1", "t2"], task_brief="彙整"),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "calendar": MagicMock(return_value=([ToolProposal(tool_name="calendar.list_my_events", arguments={})], _sub_metrics())),
                 "places": MagicMock(return_value=([], _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", return_value=MagicMock(ok=True, data={}, error_code=None)), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("部分資訊ok，但部分沒找到", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.handled)

    def test_planner_returns_none_yields_fail_closed(self):
        ctx = self._ctx("嗨")
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(None, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.handled)
        self.assertEqual(result.agent_mode, "v3")
        self.assertIsNotNone(result.fallback_reason)

    def test_failed_dependency_skips_dependent_task(self):
        ctx = self._ctx("幫我找餐廳並篩選")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="places", depends_on=[], task_brief="搜尋附近餐廳"),
            SubTask(id="t2", agent="places", depends_on=["t1"], task_brief="依 t1 結果篩選"),
            SubTask(id="t3", agent="synthesizer", depends_on=["t1", "t2"], task_brief="彙整"),
        ])
        places_runner = MagicMock(return_value=([], _sub_metrics()))
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {"places": places_runner}), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", return_value=MagicMock(ok=True, data={}, error_code=None)), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("沒找到適合的餐廳", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx, mode="on")
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
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
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
            result = run_public_agent_turn_v3(ctx, mode="on")
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
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {"calendar": cal_runner}), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", return_value=MagicMock(ok=True, data={"events": []}, error_code=None)), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("行程ok", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            run_public_agent_turn_v3(ctx, mode="on", on_progress=capture)
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
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            run_public_agent_turn_v3(ctx, mode="on", on_progress=capture)
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
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "calendar": lambda slc, task_brief: slow_runner(proposals["calendar"], _sub_metrics()),
                 "places": lambda slc, task_brief: slow_runner(proposals["places"], _sub_metrics()),
                 "relationship": lambda slc, task_brief: slow_runner(proposals["relationship"], _sub_metrics()),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", return_value=MagicMock(ok=True, data={}, error_code=None)), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("都查好了", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            run_public_agent_turn_v3(ctx, mode="on")
        self.assertGreaterEqual(max_active, 2, "same-layer tasks should overlap in time")

    def test_parallelism_respects_max_parallel_flag(self):
        ctx = self._ctx("查很多")
        plan = Plan(tasks=[
            SubTask(id=f"t{i}", agent="calendar", depends_on=[], task_brief=f"查{i}") for i in range(6)
        ] + [SubTask(id="syn", agent="synthesizer", depends_on=[f"t{i}" for i in range(6)], task_brief="彙整")])
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
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {"calendar": slow_runner}), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", return_value=MagicMock(ok=True, data={}, error_code=None)), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("ok", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            run_public_agent_turn_v3(ctx, mode="on")
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
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "calendar": MagicMock(side_effect=fake_runner),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool",
                   return_value=MagicMock(ok=True, data=find_result, error_code=None)) as mock_exec, \
             patch("services.ayue_agent.v3.scheduler.prepare_write_confirmation",
                   return_value=({"action": "calendar.update_my_event", "arguments": {}, "data": {}},
                                 "要把「吃牛排」改成8/15嗎？回覆「確認」")) as prepare, \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.insert_one"), \
             patch("services.ayue_agent.v3.synthesizer.synthesize",
                   return_value=("好，等你確認", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value._mentioned_ids = []
            mock_build.return_value.user_id = "owner"
            result = run_public_agent_turn_v3(ctx, mode="on")
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
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "places": MagicMock(return_value=(multi, _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", side_effect=fake_exec), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("牛排和冰都查好了", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            run_public_agent_turn_v3(ctx, mode="on")
        self.assertEqual(len(executed), 2, "both tool calls must execute")

    def test_parallel_tasks_with_identical_calls_do_not_cross_dedupe(self):
        """Identical tool+args across two parallel tasks must both run (per-task dedup)."""
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
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {"places": runner}), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", side_effect=fake_exec), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("兩邊都查好了", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            run_public_agent_turn_v3(ctx, mode="on")
        self.assertEqual(len(executed), 2, "identical calls in separate parallel tasks must both run")

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
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "calendar": MagicMock(return_value=(multi, _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", side_effect=fake_exec), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("行程查好了", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            run_public_agent_turn_v3(ctx, mode="on")
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
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
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
            result = run_public_agent_turn_v3(ctx, mode="on")
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
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
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
            result = run_public_agent_turn_v3(ctx, mode="on")
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
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "match": MagicMock(return_value=(
                     [ToolProposal(tool_name="match.start_search", arguments={})], _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.prepare_write_confirmation",
                   return_value=({"action": "match.start_search", "arguments": {}, "data": {}}, "要開始找人嗎？回覆「確認」")) as prepare, \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.insert_one") as insert, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", side_effect=fake_synth):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value._mentioned_ids = []
            result = run_public_agent_turn_v3(ctx, mode="on")
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
        with patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS") as coll, \
             patch("services.ayue_agent.v3.scheduler.execute_write",
                   return_value=(True, "好，我開始幫你找，通常約需要 1–3 分鐘。", None)) as exec_write, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", side_effect=fake_synth):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            coll.find.return_value = [{
                "_id": "c1", "user_id": "owner", "agent_name": "match",
                "tool_name": "match.start_search", "arguments": {},
                "payload": {}, "status": "pending",
                "created_at": 0, "expires_at": 1e18,
            }]
            coll.update_one.return_value = MagicMock(modified_count=1)
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.handled)
        exec_write.assert_called_once()
        self.assertEqual(exec_write.call_args.kwargs["payload"]["_confirmation_id"], "c1")

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
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
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
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.insert_one") as insert, \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.update_one") as update_one, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", side_effect=fake_synth):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value._mentioned_ids = []
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.handled)
        insert.assert_called_once()
        inserted = insert.call_args[0][0]
        self.assertEqual(inserted["tool_name"], "calendar.cancel_my_event")
        # 第二筆 create 併入同一 confirmation 的 batch（新格式 {tool, arguments, data}）
        update_one.assert_called_once()
        push_call = update_one.call_args
        self.assertEqual(push_call.args[0], {"_id": inserted["_id"]})
        pushed = push_call.args[1]["$push"]["batch"]
        self.assertEqual(pushed["tool"], "calendar.create_my_event")
        self.assertEqual(pushed["arguments"]["title"], "B")
        obs = seen_obs.get("observations", [])
        confirmed = [o for o in obs if o.get("tool") == "calendar.create_my_event"]
        self.assertEqual(len(confirmed), 1)
        self.assertTrue(confirmed[0]["result"].get("pending_confirmation"))

    def test_two_create_proposals_merge_into_one_batch_confirmation(self):
        # 同一 sub-task 提出兩筆 calendar.create_my_event → 一次 confirmation + batch 陣列；
        # 一次「確認」即新增兩筆（需求：一次確認變更多個行程）。
        ctx = self._ctx("幫我新增8/12吃牛排和8/9看醫生")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="新增兩筆行程"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
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
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.handled)
        # 只建立一筆 confirmation；第二筆 create 是 push 到同一 confirmation 的 batch 欄位
        insert.assert_called_once()
        inserted = insert.call_args[0][0]
        self.assertEqual(inserted["tool_name"], "calendar.create_my_event")
        self.assertEqual(inserted["batch"], [])
        self.assertEqual(inserted["arguments"]["title"], "吃牛排")
        # 第二筆 create 透過 $push 加入 batch（新格式 {tool, arguments, data}）
        update_one.assert_called_once()
        push_call = update_one.call_args
        self.assertEqual(push_call.args[0], {"_id": inserted["_id"]})
        pushed = push_call.args[1]["$push"]["batch"]
        self.assertEqual(pushed["tool"], "calendar.create_my_event")
        self.assertEqual(pushed["arguments"]["title"], "看醫生")


class V3SchedulerTraceTests(unittest.TestCase):
    def test_trace_persisted_with_allowlisted_fields(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="嗨")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="查行程"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
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
            run_public_agent_turn_v3(ctx, mode="on")
        self.assertEqual(persist.call_count, 1)
        payload = persist.call_args.args[2]
        self.assertIn("plan", payload)
        self.assertIn("tool_results", payload)
        self.assertIn("event_sequence", payload)
        self.assertNotIn("message", payload)
        self.assertNotIn("prompt", payload)


class V3SchedulerOpportunityTests(unittest.TestCase):
    def test_social_opening_creates_guidance_confirmation(self):
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
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.assess_match_opportunity") as assess, \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.insert_one") as insert, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", side_effect=fake_synth):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value.user_profile = {}
            mock_build.return_value.message = "一個人去有點孤單"
            assess.return_value = MagicMock(state="ready", reason_codes=(), fingerprint="fp1")
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.handled)
        insert.assert_called_once()
        obs = seen_obs.get("observations", [])
        self.assertTrue(any(
            (isinstance(o.get("result"), dict) and o["result"].get("pending_confirmation"))
            or (isinstance(o.get("result"), list) and any(
                isinstance(r, dict) and r.get("pending_confirmation") for r in o["result"]
            ))
            for o in obs
        ))

    def test_social_opening_not_ready_asks_basis(self):
        from services.ayue_agent.v3.contracts import OpportunitySignal
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="一個人去有點孤單")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="synthesizer", depends_on=[], task_brief="回覆"),
        ], opportunity=OpportunitySignal(signal="social_opening", evidence_span="一個人去有點孤單", confidence=0.9))
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.assess_match_opportunity") as assess:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value.user_profile = {}
            mock_build.return_value.message = "一個人去有點孤單"
            assess.return_value = MagicMock(state="not_ready", reason_codes=("profile_basis_insufficient",),
                                            missing_basis=("preferences",))
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.handled)
        self.assertEqual(result.match_readiness_state, "not_ready")
        self.assertIn("多了解你的方向", result.reply)


class V3SchedulerAssessmentTests(unittest.TestCase):
    def test_active_assessment_advances_without_planner(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="我喜歡戶外活動")
        with patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.active_assessment_session",
                   return_value={"session_id": "s1", "kind": "big_five", "expires_at": 1e18, "revision": 1}), \
             patch("services.ayue_agent.v3.scheduler.advance_assessment_session",
                   return_value={"status": "active", "session_state": "active", "kind": "big_five",
                                 "revision": 2, "reply": "好的，那假日你通常怎麼安排？"}), \
             patch("services.ayue_agent.v3.scheduler.plan_turn") as plan:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.handled)
        self.assertEqual(result.assessment_state, "active")
        self.assertEqual(result.assessment_kind, "big_five")
        plan.assert_not_called()

    def test_awaiting_commit_confirm_commits(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="確認")
        with patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.awaiting_assessment_commit",
                   return_value={"session_id": "s1", "kind": "big_five", "revision": 3, "expires_at": 1e18}), \
             patch("services.ayue_agent.v3.scheduler.assessment_commit_choice", return_value="confirm"), \
             patch("services.ayue_agent.v3.scheduler.commit_assessment_session",
                   return_value={"status": "committed", "session_state": "completed",
                                 "kind": "big_five", "revision": 4, "reply": "已套用新的基本性格資料。"}), \
             patch("services.ayue_agent.v3.scheduler.plan_turn") as plan:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx, mode="on")
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
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
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
            result = run_public_agent_turn_v3(ctx, mode="on")
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
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "places": MagicMock(return_value=(multi, _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", side_effect=fake_exec), \
             patch("services.ayue_agent.v3.synthesizer.synthesize",
                   return_value=("約 40 公里", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            run_public_agent_turn_v3(ctx, mode="on")
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
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
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
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.handled)
        mock_exec.assert_not_called()
        obs = seen_obs.get("observations", [])
        self.assertEqual(obs[0]["status"], "failed")
        self.assertEqual(obs[0]["error_code"], "web_extract_url_not_bound")


if __name__ == "__main__":
    unittest.main()