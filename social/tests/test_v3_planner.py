# tests/test_v3_planner.py
import json
import os
import unittest
from copy import deepcopy
from unittest.mock import patch

from services.ayue_agent.contracts import PublicAgentTurnContext, TurnClockV1
from services.ayue_agent.v3.contracts import Plan, SubTask
from services.ayue_agent.v3.planner import (
    _PLANNER_PROMPT_VERSION, _PLANNER_SYSTEM, _decompose_tool_schema, _planner_prompt,
    _planner_validation_retry_hint, _normalize_provider_plan_arguments, plan_turn,
)
from services.ai_service import ToolCallResult


def _clock():
    return TurnClockV1(
        timezone="Asia/Taipei", utc_iso="2026-08-04T12:00:00+00:00",
        local_iso="2026-08-04T20:00:00+08:00", local_date="2026-08-04",
        local_time="20:00", weekday_zh_tw="星期二",
    )


def _fc_result(content="", tool_calls=None, *, inject_write_intent=True):
    calls = deepcopy(tool_calls or [])
    if inject_write_intent:
        for call in calls:
            if call.get("name") != "decompose_tasks":
                continue
            arguments = call.get("arguments")
            if isinstance(arguments, dict):
                arguments.setdefault("write_intent", "none")
    return ToolCallResult(content=content, tool_calls=calls)


def _steak_dag_arguments():
    return {
        "tasks": [
            {"id": "t1", "agent": "calendar", "depends_on": [], "task_brief": "查詢使用者下週五晚上的行程空檔"},
            {"id": "t2", "agent": "places", "depends_on": [], "task_brief": "搜尋附近的牛排餐廳"},
            {"id": "t3", "agent": "places", "depends_on": ["t2"], "task_brief": "依 t2 結果篩選推薦餐廳"},
            {"id": "t4", "agent": "synthesizer", "depends_on": ["t1", "t2", "t3"], "task_brief": "彙整行程空檔與餐廳推薦，產生最終回覆給使用者。"}
        ]
    }


def _known_contact_activity_dinner_arguments(*, hard_gate=False):
    outcome = "calendar.no_scheduled_events" if hard_gate else "task.finished"
    return {
        "mode": "tasks",
        "tasks": [
            {
                "id": "c1", "agent": "calendar", "depends_on": [],
                "task_brief": "唯讀查詢這週六的本人行程",
                "outcome_contract": "calendar.availability.v1",
            },
            {
                "id": "r1", "agent": "relationship", "depends_on": [],
                "task_brief": "列出 accepted contacts，供最終回覆比較適合人選",
                "run_if": {"source_task_id": "c1", "required_outcome": outcome},
            },
            {
                "id": "w1", "agent": "web", "depends_on": [],
                "task_brief": "找高雄中山大學附近這週六可參加的新活動",
                "evidence_policy": "casual_discovery",
                "run_if": {"source_task_id": "c1", "required_outcome": outcome},
            },
            {
                "id": "p1", "agent": "places", "depends_on": ["w1"],
                "task_brief": "依 w1 的 typed activity venue 找附近晚餐",
            },
            {
                "id": "s1", "agent": "synthesizer",
                "depends_on": ["c1", "r1", "p1"],
                "task_brief": "整合空檔、人選、活動與晚餐，只提供建議不新增行程",
            },
        ],
    }


class V3PlannerTests(unittest.TestCase):
    def _turn(self, message):
        return PublicAgentTurnContext(
            user_id="owner", room_id="room", message=message,
            clock=_clock(),
        )

    def test_steak_example_produces_calendar_places_synthesizer_dag(self):
        turn = self._turn("我下週五晚上想吃牛排，幫我找餐廳並看看我那天有沒有空")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": _steak_dag_arguments()},
            ]),
        ):
            plan, _metrics = plan_turn(turn)
        self.assertIsNotNone(plan)
        self.assertIsInstance(plan, Plan)
        agents = [t.agent for t in plan.tasks]
        self.assertIn("calendar", agents)
        self.assertIn("places", agents)
        self.assertIn("synthesizer", agents)
        syn = [t for t in plan.tasks if t.agent == "synthesizer"]
        self.assertEqual(len(syn), 1)

    def test_unjustified_synthesizer_only_retries_as_direct_chat(self):
        turn = self._turn("你好嗎")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            side_effect=[
                _fc_result(tool_calls=[
                    {"name": "decompose_tasks", "arguments": {
                        "tasks": [{"id": "t1", "agent": "synthesizer", "depends_on": [], "task_brief": "回覆使用者的問候"}]
                    }},
                ]),
                _fc_result(tool_calls=[
                    {"name": "decompose_tasks", "arguments": {
                        "mode": "direct_chat", "tasks": [], "direct_reply": "嗨～今天怎麼樣？",
                    }},
                ]),
            ],
        ) as provider:
            plan, metrics = plan_turn(turn)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.mode, "direct_chat")
        self.assertEqual(plan.tasks, [])
        self.assertEqual(metrics.llm_call_count, 2)
        self.assertEqual(metrics.retry_reason, "invalid_arguments")
        self.assertTrue(provider.call_args.kwargs["prefer_fast_model"])

    def test_known_contact_activity_dinner_plan_keeps_all_four_domains(self):
        turn = self._turn(
            "這週六我想約目前認識的人出去，幫我挑一個適合的，找高雄中山大學附近最近的新活動，再安排附近晚餐；只給建議，不要新增行程。"
        )
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "decompose_tasks",
                "arguments": _known_contact_activity_dinner_arguments(),
            }]),
        ):
            plan, metrics = plan_turn(turn)

        self.assertIsNotNone(plan)
        self.assertEqual(
            [task.agent for task in plan.tasks],
            ["calendar", "relationship", "web", "places", "synthesizer"],
        )
        self.assertEqual(plan.tasks[1].run_if.required_outcome, "task.finished")
        self.assertEqual(plan.tasks[2].run_if.required_outcome, "task.finished")
        self.assertEqual(plan.tasks[3].depends_on, ["w1"])
        self.assertEqual(metrics.direct_chat_fallback_reason, "")

    def test_provider_scoped_evidence_policy_is_repaired_without_mutating_raw_arguments(self):
        turn = self._turn("找附近適合約會的地方")
        arguments = {"tasks": [
            {"id": "p1", "agent": "places", "depends_on": [], "task_brief": "找地方",
             "evidence_policy": "casual_discovery"},
            {"id": "s1", "agent": "synthesizer", "depends_on": ["p1"], "task_brief": "整理"},
        ]}
        original = json.loads(json.dumps(arguments))
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{"name": "decompose_tasks", "arguments": arguments}]),
        ):
            plan, metrics = plan_turn(turn)
        self.assertEqual([task.agent for task in plan.tasks], ["places", "synthesizer"])
        self.assertEqual(metrics.retry_count, 0)
        self.assertEqual(metrics.failure_code, "")
        self.assertEqual(metrics.attempts[0]["status"], "repaired")
        self.assertEqual(metrics.attempts[0]["repair_codes"], ["non_web_evidence_policy_removed"])
        self.assertEqual(arguments, original)

    def test_provider_scoped_calendar_outcome_contract_is_repaired(self):
        turn = self._turn("找附近的地方")
        arguments = {"tasks": [
            {"id": "p1", "agent": "places", "depends_on": [], "task_brief": "找地方",
             "outcome_contract": "calendar.availability.v1"},
            {"id": "s1", "agent": "synthesizer", "depends_on": ["p1"], "task_brief": "整理"},
        ]}
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{"name": "decompose_tasks", "arguments": arguments}]),
        ):
            plan, metrics = plan_turn(turn)
        self.assertIsNotNone(plan)
        self.assertEqual(metrics.attempts[0]["status"], "repaired")
        self.assertEqual(metrics.attempts[0]["repair_codes"], ["non_calendar_outcome_contract_removed"])

    def test_repair_code_cap_does_not_stop_scanning_later_tasks(self):
        arguments = {"tasks": [
            {"id": "p1", "agent": "places", "depends_on": [], "task_brief": "x",
             "evidence_policy": "casual_discovery",
             "outcome_contract": "calendar.availability.v1"},
            {"id": "p2", "agent": "places", "depends_on": [], "task_brief": "y",
             "evidence_policy": "strict_verification",
             "outcome_contract": "calendar.availability.v1"},
        ]}
        normalized, codes = _normalize_provider_plan_arguments(arguments)
        self.assertEqual(codes, [
            "non_web_evidence_policy_removed",
            "non_calendar_outcome_contract_removed",
        ])
        self.assertEqual(normalized["tasks"][0].keys(), {"id", "agent", "depends_on", "task_brief"})
        self.assertEqual(normalized["tasks"][1].keys(), {"id", "agent", "depends_on", "task_brief"})

    def test_valid_web_and_calendar_scoped_fields_are_preserved(self):
        arguments = {"tasks": [
            {"id": "w1", "agent": "web", "depends_on": [], "task_brief": "查證",
             "evidence_policy": "strict_verification"},
            {"id": "c1", "agent": "calendar", "depends_on": [], "task_brief": "查行程",
             "outcome_contract": "calendar.availability.v1"},
        ]}
        normalized, codes = _normalize_provider_plan_arguments(arguments)
        self.assertEqual(codes, [])
        self.assertEqual(normalized, arguments)
        self.assertIsNot(normalized, arguments)

    def test_invalid_unknown_and_graph_drift_are_not_repaired(self):
        cases = [
            {"agent": "places", "evidence_policy": "not-valid"},
            {"agent": "future_agent", "evidence_policy": "casual_discovery"},
            {"agent": "places", "run_if": {"source_task_id": "missing", "required_outcome": "task.finished"}},
        ]
        for task_extra in cases:
            with self.subTest(task_extra=task_extra):
                task = {"id": "p1", "depends_on": [], "task_brief": "x", **task_extra}
                normalized, codes = _normalize_provider_plan_arguments({"tasks": [task]})
                self.assertEqual(normalized, {"tasks": [task]})
                self.assertEqual(codes, [])

    def test_empty_optional_placeholders_are_repaired_without_retry(self):
        turn = self._turn("幫我約小涵")
        arguments = {"tasks": [
            {
                "id": "c1", "agent": "calendar", "depends_on": [],
                "task_brief": "availability", "outcome_contract": "", "run_if": {},
            },
            {
                "id": "r1", "agent": "relationship", "depends_on": [],
                "task_brief": "create card", "evidence_policy": "",
                "outcome_contract": "", "run_if": {},
            },
            {
                "id": "s1", "agent": "synthesizer", "depends_on": ["c1", "r1"],
                "task_brief": "present",
            },
        ]}
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{"name": "decompose_tasks", "arguments": arguments}]),
        ) as provider:
            plan, metrics = plan_turn(turn)
        self.assertIsNotNone(plan)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(metrics.retry_count, 0)
        self.assertEqual(metrics.failure_code, "")
        self.assertEqual(metrics.attempts[0]["status"], "repaired")
        self.assertEqual(metrics.attempts[0]["repair_codes"], ["empty_optional_task_fields_removed"])
        self.assertEqual(arguments["tasks"][0]["outcome_contract"], "")
        self.assertEqual(arguments["tasks"][0]["run_if"], {})
        self.assertEqual(arguments["tasks"][1]["evidence_policy"], "")

    def test_misplaced_date_invitation_intent_is_recovered_to_top_level(self):
        turn = self._turn("幫我約小涵")
        arguments = {
            "write_intent": "none",
            "tasks": [
                {
                    "id": "r1", "agent": "relationship", "depends_on": [],
                    "task_brief": "建立空白約會邀請卡",
                    "outcome_contract": "relationship.date_invitation.v1",
                },
                {
                    "id": "s1", "agent": "synthesizer", "depends_on": ["r1"],
                    "task_brief": "呈現 confirmation preview",
                },
            ],
        }
        original = deepcopy(arguments)
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{"name": "decompose_tasks", "arguments": arguments}]),
        ) as provider:
            plan, metrics = plan_turn(turn)
        self.assertIsNotNone(plan)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(plan.write_intent, "relationship.date_invitation.v1")
        self.assertEqual([task.agent for task in plan.tasks], ["relationship", "synthesizer"])
        self.assertIsNone(plan.tasks[0].outcome_contract)
        self.assertEqual(metrics.retry_count, 0)
        self.assertEqual(metrics.failure_code, "")
        self.assertEqual(metrics.attempts[0]["status"], "repaired")
        self.assertEqual(
            metrics.attempts[0]["repair_codes"],
            ["misplaced_date_invitation_intent_recovered"],
        )
        self.assertEqual(arguments, original)

    def test_canonical_subtask_still_rejects_empty_optional_values(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            SubTask(
                id="p1", agent="places", depends_on=[], task_brief="x",
                evidence_policy="",
            )
        with self.assertRaises(ValidationError):
            SubTask(
                id="c1", agent="calendar", depends_on=[], task_brief="x",
                outcome_contract="",
            )
        with self.assertRaises(ValidationError):
            SubTask(
                id="p2", agent="places", depends_on=[], task_brief="x",
                run_if="",
            )

    def test_missing_write_intent_retries_once_and_fails_closed(self):
        turn = self._turn("普通聊天")
        missing = _fc_result(
            tool_calls=[{
                "name": "decompose_tasks",
                "arguments": {"mode": "direct_chat", "tasks": [], "direct_reply": "嗨"},
            }],
            inject_write_intent=False,
        )
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            side_effect=[missing, missing],
        ) as provider:
            plan, metrics = plan_turn(turn)
        self.assertIsNone(plan)
        self.assertEqual(metrics.retry_count, 1)
        self.assertEqual(metrics.failure_code, "invalid_arguments")
        self.assertIn("write_intent is required", provider.call_args_list[1].args[0])

    def test_model_level_scoped_error_gets_hint_without_generic_dag_hint(self):
        from pydantic import ValidationError
        from services.ayue_agent.v3.planner import _DecomposeTasksArguments
        with self.assertRaises(ValidationError) as caught:
            _DecomposeTasksArguments.model_validate({"write_intent": "none", "tasks": [{
                "id": "p1", "agent": "places", "depends_on": [], "task_brief": "x",
                "evidence_policy": "casual_discovery",
            }]})
        hint = _planner_validation_retry_hint(caught.exception)
        self.assertIn("evidence_policy is only for Web tasks", hint)
        self.assertNotIn("A domain request uses mode=tasks", hint)

    def test_run_if_model_level_error_gets_scoped_hint_without_raw_message(self):
        from pydantic import ValidationError
        from services.ayue_agent.v3.contracts import Plan
        with self.assertRaises(ValidationError) as caught:
            Plan.model_validate({"tasks": [
                {"id": "p1", "agent": "places", "depends_on": [], "task_brief": "x",
                 "run_if": {"source_task_id": "missing", "required_outcome": "task.finished"}},
                {"id": "s1", "agent": "synthesizer", "depends_on": ["p1"], "task_brief": "s"},
            ]})
        hint = _planner_validation_retry_hint(caught.exception)
        self.assertIn("run_if is only a control edge", hint)
        self.assertNotIn("missing", hint)

    def test_explicit_stop_when_busy_uses_calendar_free_gate_for_both_parallel_reads(self):
        turn = self._turn(
            "這週六先看我有沒有事，有事就算了；沒事從目前認識的人挑一位，再找新活動和附近晚餐。"
        )
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "decompose_tasks",
                "arguments": _known_contact_activity_dinner_arguments(hard_gate=True),
            }]),
        ):
            plan, _metrics = plan_turn(turn)

        self.assertEqual(
            [plan.tasks[index].run_if.required_outcome for index in (1, 2)],
            ["calendar.no_scheduled_events", "calendar.no_scheduled_events"],
        )

    def test_simple_chat_can_produce_strict_direct_reply_plan(self):
        turn = self._turn("哈囉")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": {
                    "mode": "direct_chat", "tasks": [], "direct_reply": "嗨～怎麼啦？",
                }},
            ]),
        ):
            plan, metrics = plan_turn(turn)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.mode, "direct_chat")
        self.assertEqual(plan.tasks, [])
        self.assertEqual(plan.direct_reply, "嗨～怎麼啦？")
        self.assertEqual(metrics.decision_mode, "direct_chat")

    def test_incompatible_direct_payload_preserves_valid_domain_dag(self):
        turn = self._turn("幫我查明天行程")
        tasks = [{
            "id": "t1", "agent": "calendar", "depends_on": [], "task_brief": "查行程",
        }, {
            "id": "s1", "agent": "synthesizer", "depends_on": ["t1"], "task_brief": "彙整",
        }]
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": {
                    "mode": "direct_chat", "tasks": tasks, "direct_reply": "我幫你看",
                }},
            ]),
        ):
            plan, metrics = plan_turn(turn)
        self.assertEqual(plan.mode, "tasks")
        self.assertEqual([task.agent for task in plan.tasks], ["calendar", "synthesizer"])
        self.assertEqual(metrics.direct_chat_fallback_reason, "incompatible_direct_chat_payload")

    def test_malformed_direct_payload_retries_instead_of_synthesizer_fallback(self):
        turn = self._turn("笑死")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            side_effect=[
                _fc_result(tool_calls=[
                    {"name": "decompose_tasks", "arguments": {
                        "mode": "direct_chat", "tasks": [], "direct_reply": 42,
                    }},
                ]),
                _fc_result(tool_calls=[
                    {"name": "decompose_tasks", "arguments": {
                        "mode": "direct_chat", "tasks": [], "direct_reply": "真的很好笑 😂",
                    }},
                ]),
            ],
        ) as provider:
            plan, metrics = plan_turn(turn)
        self.assertEqual(plan.mode, "direct_chat")
        self.assertEqual(plan.tasks, [])
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(metrics.retry_reason, "invalid_arguments")
        self.assertEqual(metrics.direct_chat_fallback_reason, "")

    def test_legacy_product_info_mode_is_repaired_into_normal_dag(self):
        turn = self._turn("你跟這個 App 是做什麼的？")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "decompose_tasks",
                "arguments": {
                    "mode": "product_info",
                    "product_info_topics": [
                        "這個交友 App 的用途與定位",
                        "阿月（媒人）在 App 裡的角色",
                    ],
                },
            }]),
        ):
            plan, metrics = plan_turn(turn)

        self.assertIsNotNone(plan)
        self.assertEqual(plan.mode, "tasks")
        self.assertEqual([task.agent for task in plan.tasks], ["product_info", "synthesizer"])
        self.assertEqual(metrics.decision_mode, "tasks")
        self.assertEqual(metrics.product_info_fallback_reason, "legacy_product_info_mode")
        self.assertEqual(metrics.error, "")

    def test_planner_tool_schema_inlines_subtask_refs(self):
        schema = _decompose_tool_schema()["function"]["parameters"]
        serialized = json.dumps(schema, ensure_ascii=False)
        self.assertNotIn("$defs", serialized)
        self.assertNotIn("$ref", serialized)
        self.assertEqual(
            set(schema["properties"]),
            {
                "mode", "write_intent", "presentation_mode", "tasks", "direct_reply",
                "direct_messages", "opportunity",
            },
        )
        self.assertIn("write_intent", schema["required"])
        task_schema = schema["properties"]["tasks"]["items"]
        self.assertEqual(
            set(task_schema["properties"]),
            {
                "id", "agent", "depends_on", "task_brief", "evidence_policy",
                "outcome_contract", "run_if",
            },
        )
        self.assertEqual(
            set(task_schema["required"]),
            {"id", "agent", "task_brief"},
        )
        for field_name in ("evidence_policy", "outcome_contract", "run_if"):
            with self.subTest(field_name=field_name):
                self.assertNotIn("default", task_schema["properties"][field_name])
                self.assertNotIn("anyOf", task_schema["properties"][field_name])
                self.assertNotIn('"type": "null"', json.dumps(task_schema["properties"][field_name]))

    def test_planner_system_policy_has_routing_catalog_and_task_contract(self):
        for agent in ("calendar", "places", "web", "match", "relationship", "profile", "product_info", "synthesizer"):
            self.assertIn(f"{agent}=", _PLANNER_SYSTEM)
        self.assertIn("id", _PLANNER_SYSTEM)
        self.assertIn("agent", _PLANNER_SYSTEM)
        self.assertIn("depends_on", _PLANNER_SYSTEM)
        self.assertIn("task_brief", _PLANNER_SYSTEM)
        self.assertIn("不要使用 type", _PLANNER_SYSTEM)
        self.assertIn("mode=direct_chat", _PLANNER_SYSTEM)
        self.assertIn("direct_reply", _PLANNER_SYSTEM)
        self.assertIn("c1=calendar(outcome_contract=calendar.availability.v1)", _PLANNER_SYSTEM)
        self.assertIn("r1=relationship(run_if c1:task.finished)", _PLANNER_SYSTEM)
        self.assertIn("w1=web(run_if c1:task.finished)", _PLANNER_SYSTEM)
        self.assertIn("p1=places(depends_on=[w1])", _PLANNER_SYSTEM)
        self.assertIn("不需要 App、domain、private、external truth", _PLANNER_SYSTEM)
        self.assertIn("product_info -> synthesizer", _PLANNER_SYSTEM)
        self.assertNotIn("product_info_topics", _PLANNER_SYSTEM)

    def test_planner_dependency_policy_is_typed_and_not_sequential_by_default(self):
        self.assertIn("typed observation、candidate ref", _PLANNER_SYSTEM)
        self.assertIn("獨立查詢", _PLANNER_SYSTEM)

    def test_planner_keeps_structured_place_facts_in_places(self):
        self.assertIn("結構化 hours、price、rating、walking", _PLANNER_SYSTEM)
        self.assertIn("使用 places -> synthesizer", _PLANNER_SYSTEM)
        self.assertIn("不自動加 web", _PLANNER_SYSTEM)

    def test_planner_keeps_unstructured_public_place_claims_on_web(self):
        for claim in (
            "優惠", "特殊菜單", "活動", "臨時歇業", "社群公告",
        ):
            self.assertIn(claim, _PLANNER_SYSTEM)
        self.assertIn("使用 places -> web -> synthesizer", _PLANNER_SYSTEM)
        self.assertIn("server-issued candidate refs", _PLANNER_SYSTEM)
        self.assertIn("不使用關鍵字或 regex router", _PLANNER_SYSTEM)

    def test_planner_policy_distinguishes_contact_aggregate_from_match_singleton(self):
        self.assertIn("match=單筆 active proposal/search lifecycle", _PLANNER_SYSTEM)
        self.assertIn("relationship=accepted contacts aggregate", _PLANNER_SYSTEM)
        self.assertIn("relationship.date_invitation.v1", _PLANNER_SYSTEM)
        self.assertIn("Match 絕不作前置檢查", _PLANNER_SYSTEM)
        self.assertEqual(_PLANNER_SYSTEM.count("relationship.date_invitation.v1"), 1)
        self.assertNotIn("我想約小涵", _PLANNER_SYSTEM)
        self.assertNotIn("你可以幫我約嗎", _PLANNER_SYSTEM)

    def test_explicit_blank_invite_uses_relationship_then_synthesizer(self):
        turn = self._turn("幫我約小葵")
        arguments = {
            "write_intent": "relationship.date_invitation.v1",
            "tasks": [
                {
                    "id": "r1",
                    "agent": "relationship",
                    "depends_on": [],
                    "task_brief": "為小葵建立空白約會邀請卡；不要詢問或填寫日期、時間、地點、活動。",
                },
                {
                    "id": "s1",
                    "agent": "synthesizer",
                    "depends_on": ["r1"],
                    "task_brief": "忠實呈現邀請卡建立與等待對方填寫的狀態。",
                },
            ],
        }
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "decompose_tasks",
                "arguments": arguments,
            }]),
        ) as provider:
            plan, metrics = plan_turn(turn)

        self.assertIsNotNone(plan)
        self.assertEqual(metrics.decision_mode, "tasks")
        self.assertEqual(metrics.prompt_version, _PLANNER_PROMPT_VERSION)
        self.assertEqual(
            [task.agent for task in plan.tasks],
            ["relationship", "synthesizer"],
        )
        self.assertEqual(plan.write_intent, "relationship.date_invitation.v1")
        self.assertIn("relationship.start_date_coordination", plan.tasks[0].task_brief)
        self.assertNotIn("calendar", [task.agent for task in plan.tasks])
        self.assertNotIn("places", [task.agent for task in plan.tasks])
        self.assertNotIn("web", [task.agent for task in plan.tasks])
        self.assertEqual(provider.call_count, 1)

    def test_date_invitation_intent_rejects_match_precheck_and_retries(self):
        turn = self._turn("幫我約小哲出來")
        wrong_match_plan = _fc_result(tool_calls=[{
            "name": "decompose_tasks",
            "arguments": {
                "write_intent": "relationship.date_invitation.v1",
                "tasks": [
                    {
                        "id": "m1", "agent": "match", "depends_on": [],
                        "task_brief": "先確認對方是不是 accepted contact",
                    },
                    {
                        "id": "s1", "agent": "synthesizer", "depends_on": ["m1"],
                        "task_brief": "回覆是否能建立邀請卡",
                    },
                ],
            },
        }])
        relationship_plan = _fc_result(tool_calls=[{
            "name": "decompose_tasks",
            "arguments": {
                "write_intent": "relationship.date_invitation.v1",
                "tasks": [
                    {
                        "id": "r1", "agent": "relationship", "depends_on": [],
                        "task_brief": "建立空白邀請卡",
                    },
                    {
                        "id": "s1", "agent": "synthesizer", "depends_on": ["r1"],
                        "task_brief": "呈現確認",
                    },
                ],
            },
        }])
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            side_effect=[wrong_match_plan, relationship_plan],
        ) as provider:
            plan, metrics = plan_turn(turn)
        self.assertEqual([task.agent for task in plan.tasks], ["relationship", "synthesizer"])
        self.assertEqual(metrics.retry_count, 1)
        self.assertEqual(metrics.retry_reason, "invalid_arguments")
        self.assertEqual(metrics.failure_code, "")
        retry_prompt = provider.call_args_list[1].args[0]
        self.assertIn("Match is never a precheck", retry_prompt)
        self.assertIn("relationship.start_date_coordination", plan.tasks[0].task_brief)

    def test_date_invitation_match_precheck_twice_fails_closed(self):
        turn = self._turn("你幫我建立邀請卡")
        invalid = _fc_result(tool_calls=[{
            "name": "decompose_tasks",
            "arguments": {
                "write_intent": "relationship.date_invitation.v1",
                "tasks": [
                    {"id": "m1", "agent": "match", "depends_on": [], "task_brief": "先查狀態"},
                    {"id": "s1", "agent": "synthesizer", "depends_on": ["m1"], "task_brief": "回覆"},
                ],
            },
        }])
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            side_effect=[invalid, invalid],
        ):
            plan, metrics = plan_turn(turn)
        self.assertIsNone(plan)
        self.assertEqual(metrics.retry_count, 1)
        self.assertEqual(metrics.failure_code, "invalid_arguments")

    def test_blank_invite_prompt_version_is_explicit_and_budgeted(self):
        self.assertEqual(_PLANNER_PROMPT_VERSION, "compact_v3_write_intent_v1")
        self.assertIn("write_intent 必填", _PLANNER_SYSTEM)
        self.assertIn("relationship.date_invitation.v1", _PLANNER_SYSTEM)
        self.assertNotIn("HIGH-PRIORITY DATE INVITATION ROUTING", _PLANNER_SYSTEM)

    def test_agent_schema_describes_relationship_aggregate_and_match_singleton(self):
        schema = _decompose_tool_schema()["function"]["parameters"]
        task_schema = schema["properties"]["tasks"]["items"]
        agent_description = task_schema["properties"]["agent"]["description"]
        self.assertIn("aggregate of accepted/established contacts", agent_description)
        self.assertIn("list, count, compare, or choose among them", agent_description)
        self.assertIn("singleton active proposal/search lifecycle", agent_description)

    def test_specific_chat_advice_can_route_to_private_surface_product_info(self):
        turn = self._turn("你看得到我跟小安剛才聊什麼嗎？我下一句要怎麼回？")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "decompose_tasks",
                "arguments": {
                    "tasks": [
                        {"id": "p", "agent": "product_info", "depends_on": [], "task_brief": turn.message},
                        {"id": "s", "agent": "synthesizer", "depends_on": ["p"], "task_brief": "Compose."},
                    ],
                },
            }]),
        ):
            plan, _metrics = plan_turn(turn)

        self.assertEqual([task.agent for task in plan.tasks], ["product_info", "synthesizer"])

    def test_matching_principles_is_a_typed_product_info_topic(self):
        turn = self._turn("你們到底是怎麼配對的？原理是什麼？")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "decompose_tasks",
                "arguments": {
                    "tasks": [
                        {"id": "p", "agent": "product_info", "depends_on": [], "task_brief": turn.message},
                        {"id": "s", "agent": "synthesizer", "depends_on": ["p"], "task_brief": "Compose."},
                    ],
                },
            }]),
        ):
            plan, metrics = plan_turn(turn)

        self.assertEqual(plan.mode, "tasks")
        self.assertEqual(plan.tasks[0].agent, "product_info")
        self.assertEqual(metrics.decision_mode, "tasks")

    def test_planner_system_policy_has_bounded_social_opening_contract(self):
        self.assertIn('opportunity.signal="social_opening"', _PLANNER_SYSTEM)
        self.assertIn("evidence_span", _PLANNER_SYSTEM)
        self.assertIn("0.8", _PLANNER_SYSTEM)
        self.assertIn('signal="none"', _PLANNER_SYSTEM)

    def test_planner_system_policy_routes_calendar_draft_continuations(self):
        self.assertIn("calendar_draft", _PLANNER_SYSTEM)
        self.assertIn("missing_fields", _PLANNER_SYSTEM)
        self.assertIn("candidates", _PLANNER_SYSTEM)
        self.assertIn("只交 calendar 做唯讀驗證", _PLANNER_SYSTEM)

    def test_planner_exposes_recent_mutation_only_as_bounded_verification_context(self):
        turn = self._turn("剛才那筆有成功嗎？").model_copy(update={
            "calendar_recent_mutation": {
                "action": "cancel", "outcome": "success", "labels": ["看牙醫"],
            },
        })
        prompt = _planner_prompt(turn)
        self.assertIn("calendar_recent_mutation", prompt)
        self.assertIn("calendar_recent_mutation", _PLANNER_SYSTEM)
        self.assertIn("唯讀驗證", _PLANNER_SYSTEM)

    def test_compact_planner_prompt_and_context_projection_budget(self):
        schema_chars = len(json.dumps(_decompose_tool_schema(), ensure_ascii=False, separators=(",", ":")))
        self.assertLessEqual(len(_PLANNER_SYSTEM), 6000)
        self.assertLessEqual(schema_chars, 3500)
        self.assertLessEqual(len(_PLANNER_SYSTEM) + schema_chars, 9500)
        message = "找附近的咖啡廳"
        turn = self._turn(message).model_copy(update={
            "recent_messages": [
                {"role": "user", "content": "舊訊息一"},
                {"role": "assistant", "content": "舊回覆二"},
                {"role": "user", "content": "舊訊息三"},
                {"role": "assistant", "content": "舊回覆四"},
                {"role": "user", "content": "舊訊息五"},
                {"role": "user", "content": message},
            ],
            "active_proposal": {
                "status": "pending", "counterparty": "小安",
                "user_can_decide": True, "proposal_revision": 99,
            },
            "calendar_draft": {"missing_fields": ["date"], "candidates": []},
        })
        prompt = _planner_prompt(turn)
        payload = json.loads(prompt)
        self.assertEqual(prompt.count(message), 1)
        self.assertLessEqual(len(payload["recent_messages"]), 4)
        self.assertLessEqual(
            sum(len(item["content"]) for item in payload["recent_messages"]), 2000,
        )
        self.assertNotIn("proposal_revision", payload["active_proposal"])
        self.assertNotIn("utc_iso", payload["clock"])
        self.assertNotIn("local_iso", payload["clock"])
        self.assertNotIn("version", payload["clock"])
        self.assertNotIn("\": ", prompt)

    def test_planner_uses_system_role_and_minimal_routing_context(self):
        turn = self._turn("最近有什麼行程？")
        turn = turn.model_copy(update={
            "recent_context": "不要送進 Planner",
            "user_location": "不要送進 Planner",
            "relevant_memories": ["不要送進 Planner"],
        })
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": {
                    "tasks": [{"id": "t1", "agent": "synthesizer", "depends_on": [], "task_brief": "回覆"}],
                }},
            ]),
        ) as call:
            plan_turn(turn)
        self.assertTrue(call.call_args.kwargs["system_prompt"])
        user_prompt = call.call_args.args[0]
        self.assertIn("最近有什麼行程", user_prompt)
        self.assertNotIn("不要送進 Planner", user_prompt)
        self.assertNotIn("pending_confirmations", user_prompt)
        self.assertNotIn("find_my_event", call.call_args.kwargs["system_prompt"])

    def test_basic_assessment_intent_has_profile_task(self):
        turn = self._turn("那我來做基本性格")
        expected = {
            "tasks": [
                {"id": "p1", "agent": "profile", "depends_on": [],
                 "task_brief": "開始基本性格探索，呼叫 profile.start_assessment(kind=basic)"},
                {"id": "s1", "agent": "synthesizer", "depends_on": ["p1"],
                 "task_brief": "根據探索 confirmation 結果回覆使用者"},
            ]
        }
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": expected},
            ]),
        ):
            plan, _metrics = plan_turn(turn)
        self.assertIsNotNone(plan)
        self.assertEqual([task.agent for task in plan.tasks], ["profile", "synthesizer"])

    def test_inaccurate_existing_personality_profile_can_produce_basic_assessment_task(self):
        turn = self._turn("我覺得我資料上的個性不是我欸")
        expected = {
            "tasks": [
                {"id": "p1", "agent": "profile", "depends_on": [],
                 "task_brief": "既有個性資料不符合本人，重新開始基本性格探索並呼叫 profile.start_assessment(kind=basic)"},
                {"id": "s1", "agent": "synthesizer", "depends_on": ["p1"],
                 "task_brief": "呈現重新測驗的確認預覽"},
            ]
        }
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": expected},
            ]),
        ):
            plan, _metrics = plan_turn(turn)

        self.assertEqual([task.agent for task in plan.tasks], ["profile", "synthesizer"])

    def test_no_tool_call_returns_none(self):
        turn = self._turn("x")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(content="我想先聊聊"),
        ) as provider:
            plan, metrics = plan_turn(turn)
        self.assertIsNone(plan)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(metrics.llm_call_count, 2)
        self.assertEqual(metrics.retry_reason, "missing_tool_call")
        self.assertEqual(metrics.failure_code, "missing_tool_call")

    def test_wrong_tool_name_returns_none(self):
        turn = self._turn("x")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "nope.bad", "arguments": {}},
            ]),
        ) as provider:
            plan, metrics = plan_turn(turn)
        self.assertIsNone(plan)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(metrics.failure_code, "wrong_function_name")

    def test_invalid_arguments_returns_none(self):
        turn = self._turn("x")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": {
                    "tasks": [{"id": "t1", "agent": "not_an_agent", "depends_on": [], "task_brief": "x"}],
                }},
            ]),
        ) as provider:
            plan, metrics = plan_turn(turn)
        self.assertIsNone(plan)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(metrics.failure_code, "invalid_arguments")

    def test_agent_scoped_field_error_gets_specific_retry_guidance(self):
        turn = self._turn("我目前有配到誰")
        invalid = _fc_result(tool_calls=[{
            "name": "decompose_tasks",
            "arguments": {
                "mode": "tasks",
                "tasks": [
                    {
                        "id": "r1", "agent": "relationship", "depends_on": [],
                        "task_brief": "列出 accepted contacts",
                        "evidence_policy": "match.current_proposal.v1",
                        "outcome_contract": "match.current_proposal.v1",
                    },
                    {
                        "id": "s1", "agent": "synthesizer", "depends_on": ["r1"],
                        "task_brief": "彙整",
                    },
                ],
            },
        }])
        valid = _fc_result(tool_calls=[{
            "name": "decompose_tasks",
            "arguments": {
                "mode": "tasks",
                "tasks": [
                    {
                        "id": "r1", "agent": "relationship", "depends_on": [],
                        "task_brief": "列出 accepted contacts",
                    },
                    {
                        "id": "s1", "agent": "synthesizer", "depends_on": ["r1"],
                        "task_brief": "彙整",
                    },
                ],
            },
        }])
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            side_effect=[invalid, valid],
        ) as provider:
            plan, metrics = plan_turn(turn)

        retry_prompt = provider.call_args_list[1].args[0]
        self.assertEqual([task.agent for task in plan.tasks], ["relationship", "synthesizer"])
        self.assertIn("evidence_policy is only for Web tasks", retry_prompt)
        self.assertIn("outcome_contract is only for a Calendar availability task", retry_prompt)
        self.assertIn("observation schema names never belong here", retry_prompt)
        self.assertEqual(metrics.retry_reason, "invalid_arguments")
        self.assertEqual(metrics.failure_code, "")

    def test_missing_tool_call_retries_once_then_recovers(self):
        turn = self._turn("找附近的咖啡廳")
        valid = _fc_result(tool_calls=[{
            "name": "decompose_tasks",
            "arguments": {"tasks": [
                {
                    "id": "p1", "agent": "places", "depends_on": [],
                    "task_brief": "找附近咖啡廳",
                },
                {
                    "id": "s1", "agent": "synthesizer", "depends_on": ["p1"],
                    "task_brief": "彙整",
                },
            ]},
        }])
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            side_effect=[_fc_result(content="我先想一下"), valid],
        ) as provider:
            plan, metrics = plan_turn(turn)
        self.assertIsNotNone(plan)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(metrics.llm_call_count, 2)
        self.assertEqual(metrics.retry_count, 1)
        self.assertEqual(metrics.retry_reason, "missing_tool_call")
        self.assertEqual(metrics.failure_code, "")
        self.assertEqual([item["status"] for item in metrics.attempts], ["protocol_error", "ok"])
        self.assertIn("protocol retry", provider.call_args_list[1].args[0])

    def test_wrong_function_name_retries_once_then_recovers(self):
        turn = self._turn("查詢明天行程")
        valid = _fc_result(tool_calls=[{
            "name": "decompose_tasks",
            "arguments": {"tasks": [
                {
                    "id": "c1", "agent": "calendar", "depends_on": [],
                    "task_brief": "查詢明天行程",
                },
                {
                    "id": "s1", "agent": "synthesizer", "depends_on": ["c1"],
                    "task_brief": "彙整",
                },
            ]},
        }])
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            side_effect=[
                _fc_result(tool_calls=[{"name": "wrong_tool", "arguments": {}}]),
                valid,
            ],
        ) as provider:
            plan, metrics = plan_turn(turn)
        self.assertIsNotNone(plan)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(metrics.retry_reason, "wrong_function_name")
        self.assertEqual(metrics.failure_code, "")

    def test_planner_does_not_accept_type_as_agent_alias(self):
        turn = self._turn("幫我回覆這句話")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": {
                    "tasks": [{
                        "type": "synthesizer", "depends_on": [], "task_brief": "回覆",
                    }],
                }},
            ]),
        ):
            plan, _metrics = plan_turn(turn)
        self.assertIsNone(plan)

    def test_timeout_returns_none(self):
        turn = self._turn("x")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            side_effect=TimeoutError("timeout"),
        ) as provider:
            plan, metrics = plan_turn(turn)
        self.assertIsNone(plan)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(metrics.llm_call_count, 1)
        self.assertEqual(metrics.failure_code, "provider_error")


class V3PlannerOpportunityTests(unittest.TestCase):
    def test_plan_parses_opportunity_signal(self):
        from services.ayue_agent.v3.contracts import OpportunitySignal
        from services.ayue_agent.v3.planner import _DecomposeTasksArguments
        args = _DecomposeTasksArguments.model_validate({
            "write_intent": "none",
            "tasks": [{"id": "t1", "agent": "synthesizer", "depends_on": [], "task_brief": "回覆"}],
            "opportunity": {"signal": "social_opening", "evidence_span": "一個人去有點孤單", "confidence": 0.9},
        })
        self.assertEqual(args.opportunity.signal, "social_opening")
        self.assertEqual(args.opportunity.evidence_span, "一個人去有點孤單")

    def test_plan_turn_carries_opportunity(self):
        from services.ayue_agent.v3.contracts import OpportunitySignal
        turn = PublicAgentTurnContext(
            user_id="owner", room_id="room", message="一個人去有點孤單",
            clock=_clock(),
        )
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": {
                    "tasks": [{"id": "t1", "agent": "synthesizer", "depends_on": [], "task_brief": "回覆"}],
                    "opportunity": {"signal": "social_opening", "evidence_span": "一個人去有點孤單", "confidence": 0.9},
                }},
            ]),
        ):
            plan, _metrics = plan_turn(turn)
        self.assertIsNotNone(plan)
        self.assertIsInstance(plan.opportunity, OpportunitySignal)
        self.assertEqual(plan.opportunity.signal, "social_opening")
        self.assertEqual(plan.opportunity.evidence_span, "一個人去有點孤單")


@unittest.skipUnless(
    os.getenv("AYUE_LIVE_PLANNER_SMOKE", "").strip().lower() in {"1", "true", "on"},
    "set AYUE_LIVE_PLANNER_SMOKE=1 to run provider-backed Planner smoke tests",
)
class V3PlannerLiveSmokeTests(unittest.TestCase):
    """Optional smoke coverage for the configured real function-calling provider."""

    def _turn(self, message):
        return PublicAgentTurnContext(
            user_id="live-planner-smoke", room_id="live-planner-smoke", message=message,
            clock=_clock(),
        )

    def _assert_agents(self, message, expected):
        plan, metrics = plan_turn(self._turn(message))
        self.assertIsNotNone(plan, metrics.error)
        self.assertEqual(plan.mode, "tasks")
        self.assertEqual({task.agent for task in plan.tasks}, set(expected) | {"synthesizer"})
        self.assertEqual(sum(task.agent == "synthesizer" for task in plan.tasks), 1)

    def test_live_simple_chat(self):
        plan, metrics = plan_turn(self._turn("我今天有點累"))
        self.assertIsNotNone(plan, metrics.error)
        self.assertEqual(plan.mode, "direct_chat")
        self.assertEqual(plan.tasks, [])
        self.assertTrue(plan.direct_reply)

    def test_live_places_request(self):
        self._assert_agents("幫我找台北車站附近的咖啡廳", {"places"})

    def test_live_explicit_match_request(self):
        self._assert_agents("請開始幫我找一位適合一起吃飯的人", {"match"})

    def test_live_known_contact_activity_dinner_request(self):
        plan, metrics = plan_turn(self._turn(
            "這週六我想約目前認識的人出去，幫我挑一個適合的，找高雄中山大學附近最近有什麼新的活動，再幫我安排附近吃晚餐；先給我完整建議，不要幫我真的新增行程。"
        ))
        self.assertIsNotNone(plan, metrics.error)
        self.assertEqual(plan.mode, "tasks")
        self.assertEqual(
            {task.agent for task in plan.tasks},
            {"calendar", "relationship", "web", "places", "synthesizer"},
        )
        calendar = next(task for task in plan.tasks if task.agent == "calendar")
        relationship = next(task for task in plan.tasks if task.agent == "relationship")
        web = next(task for task in plan.tasks if task.agent == "web")
        places = next(task for task in plan.tasks if task.agent == "places")
        self.assertEqual(calendar.outcome_contract, "calendar.availability.v1")
        self.assertEqual(relationship.run_if.source_task_id, calendar.id)
        self.assertEqual(relationship.run_if.required_outcome, "task.finished")
        self.assertEqual(web.run_if.source_task_id, calendar.id)
        self.assertEqual(web.run_if.required_outcome, "task.finished")
        self.assertEqual(places.depends_on, [web.id])


if __name__ == "__main__":
    unittest.main()
