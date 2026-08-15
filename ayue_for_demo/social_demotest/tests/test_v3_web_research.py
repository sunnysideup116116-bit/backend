import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.ayue_agent.v3 import guarded_execution, planner, scheduler, web_runtime
from services.ayue_agent.v3.contracts import AgentContextSlice, SubTask, SubTaskResult, SubTaskStatus, ToolProposal, VALID_AGENTS
from services.ayue_agent.v3.guarded_execution import GuardedReadExecutor
from services.ayue_agent.v3.sub_agents import places_agent
from services.ayue_agent.v3.sub_agents.base import SubAgentMetrics
from services.ayue_agent.v3.sub_agents.web_agent import (
    WebResearchDecision,
    _decision_tools,
    _project_dependency_observations,
    decide as decide_web,
)
from services.ayue_agent.v3.synthesizer import synthesize
from services.ayue_agent.v3.web_research import (
    MAX_WEB_PROMPT_SEARCH_RESULTS,
    WebEvidenceAssessmentV1,
    anchor_web_search_query,
    anchor_place_search_query,
    build_research_result,
    project_web_observations,
)
from services.ayue_agent.tool_registry import PLACES_TOOLS, WEB_TOOLS


def _search_result(url="https://example.com/forum/post"):
    return {"results": [{
        "title": "Public result",
        "url": url,
        "snippet": "A directly relevant public result.",
        "published_date": "2026-08-09",
    }]}


def _turn(message="What did the public source say?"):
    return SimpleNamespace(
        message=message,
        user_location="Kaohsiung",
        clock=SimpleNamespace(model_dump=lambda: {"timezone": "Asia/Taipei"}),
        _raw_ctx=SimpleNamespace(),
    )


def _slice(message="What did the public source say?"):
    return AgentContextSlice(agent="web", payload={
        "message": message,
        "recent_messages": [],
        "user_location": "Kaohsiung",
        "clock": {"timezone": "Asia/Taipei"},
        "prior_observations": [],
    })


def _trace():
    return {"guard_results": [], "tool_results": [], "event_sequence": []}


def _run_web(task, turn, context_slice, *, seen_keys=None, guard_lock=None,
             on_progress=None, run_id="run", trace=None, debug_enabled=False):
    services = GuardedReadExecutor(
        task_id=task.id,
        agent_name=task.agent,
        turn_ctx=turn,
        seen_keys=seen_keys if seen_keys is not None else set(),
        guard_lock=guard_lock or threading.Lock(),
        on_progress=on_progress,
        run_id=run_id,
        trace=trace or _trace(),
        emit_progress=scheduler._emit_progress,
        append_debug_event=scheduler.append_debug_event,
        debug_enabled=debug_enabled,
    )
    runner_result, metrics = web_runtime.run(context_slice, task=task, services=services)
    result = runner_result.completed_results[0]
    return [result], metrics


class V3WebResearchTests(unittest.TestCase):
    def test_web_dependency_projection_keeps_bounded_activity_facts_only(self):
        projected = _project_dependency_observations([{
            "task_id": "activity-web",
            "status": "ok",
            "tool": "web.search",
            "result": {
                "schema_version": "web_research.v1",
                "status": "answered",
                "coverage": "direct_sufficient",
                "findings": [{
                    "claim": "駁二週六 14:00 有公開活動",
                    "relation": "direct",
                    "source_urls": ["https://example.com/private"],
                }, {
                    "claim": "無關背景",
                    "relation": "adjacent_context",
                }, {
                    "claim": "第三筆不應進入 projection",
                    "relation": "direct",
                }],
                "limitations": ["需再次確認場館安排", "第二個限制"],
            },
        }])
        self.assertEqual(projected[0]["findings"], [
            {"claim": "駁二週六 14:00 有公開活動", "relation": "direct"},
            {"claim": "無關背景", "relation": "adjacent_context"},
        ])
        self.assertNotIn("source_urls", str(projected))
        self.assertLessEqual(len(projected[0]["limitations"]), 2)

    def test_web_capabilities_are_separate_from_places(self):
        self.assertIn("web", VALID_AGENTS)
        self.assertEqual(places_agent._TOOLS, PLACES_TOOLS)
        self.assertTrue(WEB_TOOLS.isdisjoint(places_agent._TOOLS))
        self.assertNotIn("可輔以網路搜尋", places_agent._SYSTEM)
        self.assertIn("獨立 Web task", places_agent._SYSTEM)
        self.assertIn("web", planner._PLANNER_SYSTEM)

    def test_guarded_web_adapter_executes_only_after_guard_and_executor_projection(self):
        services = GuardedReadExecutor(
            task_id="web-1",
            agent_name="web",
            turn_ctx=_turn(),
            seen_keys=set(),
            guard_lock=threading.Lock(),
            on_progress=None,
            run_id="run",
            trace=_trace(),
            emit_progress=scheduler._emit_progress,
            append_debug_event=scheduler.append_debug_event,
        )
        proposal = ToolProposal(
            tool_name="web.search",
            arguments={"query": "public source", "recency": "none", "use_saved_location": False},
        )
        with patch.object(guarded_execution, "execute_tool", return_value=SimpleNamespace(
            ok=True, data={"results": []}, error_code=None,
        )) as execute_tool:
            outcome = services.execute(
                proposal,
                allowed_tools=frozenset({"web.search", "web.extract"}),
                step_count=0,
                max_reads=3,
                prior_observations=[],
                call_index=0,
            )
        self.assertTrue(outcome.attempted)
        self.assertEqual(outcome.result.status, SubTaskStatus.OK)
        execute_tool.assert_called_once()
        self.assertEqual(execute_tool.call_args.args[0].name, "web.search")
        self.assertEqual(execute_tool.call_args.args[0].arguments["query"], "public source")

    def test_guarded_web_adapter_keeps_extract_url_binding_before_execution(self):
        services = GuardedReadExecutor(
            task_id="web-1",
            agent_name="web",
            turn_ctx=_turn(),
            seen_keys=set(),
            guard_lock=threading.Lock(),
            on_progress=None,
            run_id="run",
            trace=_trace(),
            emit_progress=scheduler._emit_progress,
            append_debug_event=scheduler.append_debug_event,
        )
        proposal = ToolProposal(
            tool_name="web.extract",
            arguments={"urls": ["https://not-observed.example/article"], "query": "details"},
        )
        with patch.object(guarded_execution, "execute_tool") as execute_tool:
            outcome = services.execute(
                proposal,
                allowed_tools=frozenset({"web.search", "web.extract"}),
                step_count=0,
                max_reads=3,
                prior_observations=[],
                call_index=0,
            )
        self.assertFalse(outcome.attempted)
        self.assertEqual(outcome.result.error_code, "web_extract_url_not_bound")
        execute_tool.assert_not_called()

    def test_scheduler_web_runner_is_domain_runtime_with_shared_services_contract(self):
        self.assertIs(scheduler._SUB_AGENT_RUNNERS["web"].runner, web_runtime.run)

        received = {}

        def runner(context_slice, *, task, services):
            received["task"] = task
            received["services"] = services
            return ([], SubAgentMetrics())

        task = SubTask(id="web-1", agent="web", depends_on=[], task_brief="research")
        services = object()
        scheduler._invoke_registered_runner(runner, _slice(), task, services)
        self.assertIs(received["task"], task)
        self.assertIs(received["services"], services)

    def test_invalid_optional_recency_falls_back_to_none(self):
        decision = WebResearchDecision(
            action="search", queries=["Pier-2 events 2026-08-10"], recency="2026",
        )
        self.assertEqual(decision.recency, "none")

    def test_first_round_schema_excludes_conclusion_fields(self):
        tools = _decision_tools(initial_search=True, can_finish=False)
        self.assertEqual(
            [tool["function"]["name"] for tool in tools],
            ["web_search_decision", "web_extract_decision"],
        )
        for tool in tools:
            properties = tool["function"]["parameters"]["properties"]
            self.assertNotIn("assessment", properties)
            self.assertNotIn("findings", properties)

    def test_ordinary_search_schema_does_not_expose_place_subject_refs(self):
        search = next(
            tool for tool in _decision_tools(initial_search=True, can_finish=False)
            if tool["function"]["name"] == "web_search_decision"
        )
        self.assertNotIn(
            "subject_refs",
            search["function"]["parameters"]["properties"],
        )
        place_search = next(
            tool for tool in _decision_tools(
                initial_search=True, can_finish=False, has_place_candidates=True,
            )
            if tool["function"]["name"] == "web_search_decision"
        )
        self.assertIn(
            "subject_refs",
            place_search["function"]["parameters"]["properties"],
        )

    def test_final_round_schema_requires_finish(self):
        tools = _decision_tools(
            can_search=False, can_extract=False, can_finish=True, initial_search=False,
        )
        self.assertEqual([tool["function"]["name"] for tool in tools], ["web_finish_decision"])
        parameters = tools[0]["function"]["parameters"]
        self.assertIn("has_direct_evidence", parameters["required"])
        self.assertNotIn("coverage", parameters["properties"])
        self.assertNotIn("status", parameters["properties"])

    def test_finish_only_uses_phase_state_even_after_round_three(self):
        source_url = "https://example.com/forum/post"
        provider_result = SimpleNamespace(
            input_tokens=1, output_tokens=1, duration_ms=1, content="",
            tool_calls=[{"name": "web_finish_decision", "arguments": {
                "evidence_conflicts_target": False,
                "has_direct_evidence": True,
                "direct_evidence_complete": True,
                "findings": [{
                    "finding": "The observed source directly supports the request.",
                    "direct": True,
                    "source_urls": [source_url],
                }],
            }}],
        )
        with patch(
            "services.ayue_agent.v3.sub_agents.web_agent.generate_chat_completion_with_tools",
            return_value=provider_result,
        ):
            decision, metrics = decide_web(
                _slice(), task_brief="Find exact public evidence", round_index=7,
                observations=[{"tool": "web.search", "result": _search_result(source_url)}],
                tool_calls_used=3, search_calls_used=2, extract_calls_used=1,
                finish_only=True,
            )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, "finish")
        self.assertEqual(
            [tool["function"]["name"] for tool in metrics.tools_raw],
            ["web_finish_decision"],
        )
        self.assertEqual(metrics.input_payload["phase"], "finish")
        self.assertEqual(metrics.input_payload["available_actions"], ["finish"])
        self.assertNotIn("第一輪只能", metrics.prompt_raw)
        self.assertNotIn("第二輪看到", metrics.prompt_raw)
        self.assertNotIn("第三輪只能", metrics.prompt_raw)
        self.assertNotIn("先填 assessment", metrics.prompt_raw)
        self.assertEqual(metrics.error, "")

    def test_search_cap_does_not_drop_later_extract_observation(self):
        def search_rows(start, count):
            return [{
                "title": f"Search result {index}",
                "url": f"https://example.com/search-{index}",
                "snippet": f"Search snippet {index}",
            } for index in range(start, start + count)]

        observations = [
            {"tool": "web.search", "result": {"results": search_rows(1, 5)}},
            {"tool": "web.search", "result": {"results": search_rows(6, 5)}},
            {"tool": "web.extract", "result": {"pages": [{
                "url": "https://example.com/search-1",
                "content": "Later extracted evidence must remain visible.",
                "truncated": False,
            }]}},
        ]

        projected = project_web_observations(observations)
        search_count = sum(
            len(item["result"]["results"])
            for item in projected if item["tool"] == "web.search"
        )
        extract_items = [item for item in projected if item["tool"] == "web.extract"]

        self.assertEqual(search_count, MAX_WEB_PROMPT_SEARCH_RESULTS)
        self.assertEqual(len(extract_items), 1)
        self.assertIn(
            "Later extracted evidence must remain visible.",
            extract_items[0]["result"]["pages"][0]["content"],
        )

    def test_full_runtime_finish_receives_late_extract_content(self):
        source_url = "https://example.com/target"
        provider_results = [
            SimpleNamespace(
                input_tokens=1, output_tokens=1, duration_ms=1, content="",
                tool_calls=[{"name": "web_search_decision", "arguments": {
                    "queries": ["initial discovery"],
                }}],
            ),
            SimpleNamespace(
                input_tokens=1, output_tokens=1, duration_ms=1, content="",
                tool_calls=[{"name": "web_search_decision", "arguments": {
                    "queries": ["refined context"],
                }}],
            ),
            SimpleNamespace(
                input_tokens=1, output_tokens=1, duration_ms=1, content="",
                tool_calls=[{"name": "web_extract_decision", "arguments": {
                    "urls": [source_url],
                    "extract_query": "direct details",
                }}],
            ),
            SimpleNamespace(
                input_tokens=1, output_tokens=1, duration_ms=1, content="",
                tool_calls=[{"name": "web_finish_decision", "arguments": {
                    "evidence_conflicts_target": False,
                    "has_direct_evidence": True,
                    "direct_evidence_complete": True,
                    "findings": [{
                        "finding": "The extracted page directly answers the request.",
                        "direct": True,
                        "source_urls": [source_url],
                    }],
                }}],
            ),
        ]

        def fake_execute(tool_call, raw_ctx, *, clock):
            if tool_call.name == "web.search":
                return SimpleNamespace(ok=True, data={"results": [{
                    "title": "Target source",
                    "url": source_url,
                    "snippet": "Search discovery result.",
                }]})
            return SimpleNamespace(ok=True, data={"pages": [{
                "url": source_url,
                "content": "Later extracted evidence must remain visible to the finalizer.",
                "truncated": False,
            }]})

        with patch.object(web_runtime, "web_enabled", return_value=True), \
             patch.object(
                 web_runtime.web_agent,
                 "generate_chat_completion_with_tools",
                 side_effect=provider_results,
             ) as provider, \
             patch.object(guarded_execution, "execute_tool", side_effect=fake_execute) as execute:
            results, _metrics = _run_web(
                SubTask(id="web1", agent="web", task_brief="Find nuanced public evidence"),
                _turn(), _slice(), seen_keys=set(), guard_lock=threading.Lock(),
                on_progress=None, run_id="run", trace=_trace(), debug_enabled=False,
            )

        self.assertEqual(execute.call_count, 3)
        self.assertEqual(provider.call_count, 4)
        self.assertEqual(results[0].observation["status"], "answered")
        self.assertIn(
            "Later extracted evidence must remain visible to the finalizer.",
            provider.call_args_list[-1].args[0],
        )
        self.assertEqual(
            provider.call_args_list[-1].args[1][0]["function"]["name"],
            "web_finish_decision",
        )

    def test_second_round_allows_only_one_refined_query(self):
        tools = _decision_tools(
            can_search=True, can_extract=True, can_finish=True, initial_search=False,
        )
        search = next(tool for tool in tools if tool["function"]["name"] == "web_search_decision")
        queries = search["function"]["parameters"]["properties"]["queries"]
        self.assertEqual(queries["maxItems"], 1)

    def test_second_round_parser_bounds_extra_refined_queries(self):
        provider_result = SimpleNamespace(
            input_tokens=1, output_tokens=1, duration_ms=1, content="",
            tool_calls=[{"name": "web_search_decision", "arguments": {
                "queries": ["refined one", "refined two"], "recency": "none",
            }}],
        )
        with patch(
            "services.ayue_agent.v3.sub_agents.web_agent.generate_chat_completion_with_tools",
            return_value=provider_result,
        ):
            decision, metrics = decide_web(
                _slice(), task_brief="Find exact dated events", round_index=2,
                observations=[{"tool": "web.search", "result": _search_result()}],
                tool_calls_used=1, search_calls_used=1, extract_calls_used=0,
            )
        self.assertEqual(decision.queries, ["refined one"])
        self.assertEqual(metrics.error, "")

    def test_web_agent_receives_bounded_place_candidates_and_subject_refs(self):
        ref = "place_candidate_0123456789abcdef"
        provider_result = SimpleNamespace(
            input_tokens=1, output_tokens=1, duration_ms=1, content="",
            tool_calls=[{"name": "web_search_decision", "arguments": {
                "queries": ["A Cafe hours"], "subject_refs": [ref],
            }}],
        )
        with patch(
            "services.ayue_agent.v3.sub_agents.web_agent.generate_chat_completion_with_tools",
            return_value=provider_result,
        ):
            decision, metrics = decide_web(
                _slice(), task_brief="確認今晚是否營業", round_index=1,
                observations=[], tool_calls_used=0, search_calls_used=0,
                extract_calls_used=0,
                place_candidates=[{
                    "candidate_ref": ref, "name": "A Cafe", "category": "cafe",
                    "address_summary": "楠梓區", "distance_m": 800,
                    "map_url": "https://internal.invalid", "place_id": "secret",
                }],
            )
        self.assertIsNotNone(decision)
        self.assertEqual(decision.subject_refs, [ref])
        self.assertEqual(metrics.input_payload["place_candidates"][0]["name"], "A Cafe")
        self.assertNotIn("map_url", metrics.input_payload["place_candidates"][0])
        self.assertNotIn("place_id", metrics.input_payload["place_candidates"][0])

    def test_web_decision_retries_once_when_provider_omits_function_call(self):
        ref = "place_candidate_0123456789abcdef"
        responses = [
            SimpleNamespace(
                input_tokens=1, output_tokens=1, duration_ms=2,
                content="", tool_calls=[],
            ),
            SimpleNamespace(
                input_tokens=2, output_tokens=1, duration_ms=3,
                content="", tool_calls=[{
                    "name": "web_search_decision",
                    "arguments": {"queries": ["A Cafe hours"], "subject_refs": [ref]},
                }],
            ),
        ]
        with patch(
            "services.ayue_agent.v3.sub_agents.web_agent.generate_chat_completion_with_tools",
            side_effect=responses,
        ) as provider:
            decision, metrics = decide_web(
                _slice(), task_brief="確認今晚是否營業", round_index=1,
                observations=[], tool_calls_used=0, search_calls_used=0,
                extract_calls_used=0,
                place_candidates=[{
                    "candidate_ref": ref, "name": "A Cafe", "category": "cafe",
                    "address_summary": "鹽埕區", "distance_m": 300,
                }],
            )
        self.assertIsNotNone(decision)
        self.assertEqual(decision.subject_refs, [ref])
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(metrics.input_tokens, 3)
        self.assertEqual(metrics.duration_ms, 5)
        self.assertEqual(metrics.rejected_calls, ["web_decision_missing_function_call"])
        self.assertIn("唯一一次修正機會", provider.call_args.args[0])

    def test_web_decision_retries_once_on_provider_5xx(self):
        class ProviderError(Exception):
            status_code = 500

        provider_result = SimpleNamespace(
            input_tokens=2, output_tokens=1, duration_ms=3, content="",
            tool_calls=[{
                "name": "web_search_decision",
                "arguments": {"queries": ["exact public question"]},
            }],
        )
        with patch(
            "services.ayue_agent.v3.sub_agents.web_agent.generate_chat_completion_with_tools",
            side_effect=[ProviderError("upstream failure"), provider_result],
        ) as provider:
            decision, metrics = decide_web(
                _slice(), task_brief="Find exact public evidence", round_index=1,
                observations=[], tool_calls_used=0, search_calls_used=0,
                extract_calls_used=0,
            )
        self.assertIsNotNone(decision)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(metrics.error, "")
        self.assertEqual(metrics.rejected_calls, ["web_decision_provider_5xx"])

    def test_web_decision_retries_invalid_place_subject_binding(self):
        ref = "place_candidate_0123456789abcdef"
        responses = [
            SimpleNamespace(
                input_tokens=1, output_tokens=1, duration_ms=1, content="",
                tool_calls=[{
                    "name": "web_search_decision",
                    "arguments": {"queries": ["A Cafe hours"]},
                }],
            ),
            SimpleNamespace(
                input_tokens=1, output_tokens=1, duration_ms=1, content="",
                tool_calls=[{
                    "name": "web_search_decision",
                    "arguments": {"queries": ["A Cafe hours"], "subject_refs": [ref]},
                }],
            ),
        ]
        with patch(
            "services.ayue_agent.v3.sub_agents.web_agent.generate_chat_completion_with_tools",
            side_effect=responses,
        ):
            decision, metrics = decide_web(
                _slice(), task_brief="確認今晚是否營業", round_index=1,
                observations=[], tool_calls_used=0, search_calls_used=0,
                extract_calls_used=0,
                place_candidates=[{
                    "candidate_ref": ref, "name": "A Cafe", "category": "cafe",
                    "address_summary": "鹽埕區", "distance_m": 300,
                }],
            )
        self.assertIsNotNone(decision)
        self.assertEqual(metrics.rejected_calls, ["web_place_subject_binding_invalid"])

    def test_finish_booleans_derive_insufficient_evidence(self):
        provider_result = SimpleNamespace(
            input_tokens=1, output_tokens=1, duration_ms=1, content="",
            tool_calls=[{"name": "web_finish_decision", "arguments": {
                "evidence_conflicts_target": False,
                "has_direct_evidence": False,
                "findings": [{"finding": "Only generic venue background was found."}],
                "limitations": ["No dated schedule was found."],
            }}],
        )
        with patch(
            "services.ayue_agent.v3.sub_agents.web_agent.generate_chat_completion_with_tools",
            return_value=provider_result,
        ):
            decision, metrics = decide_web(
                _slice(), task_brief="Find exact dated events", round_index=3,
                observations=[{"tool": "web.search", "result": _search_result()}],
                tool_calls_used=1, search_calls_used=1, extract_calls_used=0,
            )
        self.assertEqual(metrics.error, "")
        self.assertEqual(decision.status, "insufficient_evidence")
        self.assertEqual(decision.assessment.coverage, "adjacent_only")

    def test_finish_respects_per_finding_direct_flag(self):
        direct_url = "https://example.com/direct"
        adjacent_url = "https://example.com/background"
        provider_result = SimpleNamespace(
            input_tokens=1, output_tokens=1, duration_ms=1, content="",
            tool_calls=[{"name": "web_finish_decision", "arguments": {
                "has_direct_evidence": True,
                "direct_evidence_complete": False,
                "findings": [
                    {
                        "finding": "The shop lists Sunday hours until 23:00.",
                        "direct": True,
                        "source_urls": [direct_url],
                    },
                    {
                        "finding": "A general district guide mentions cafes.",
                        "direct": False,
                        "source_urls": [adjacent_url],
                    },
                ],
                "limitations": ["Only one candidate was confirmed."],
            }}],
        )
        observations = [{"tool": "web.search", "result": {"results": [
            {"title": "Direct", "url": direct_url, "snippet": "hours"},
            {"title": "Background", "url": adjacent_url, "snippet": "guide"},
        ]}}]
        with patch(
            "services.ayue_agent.v3.sub_agents.web_agent.generate_chat_completion_with_tools",
            return_value=provider_result,
        ):
            decision, _metrics = decide_web(
                _slice(), task_brief="Confirm Sunday opening hours", round_index=3,
                observations=observations, tool_calls_used=1,
                search_calls_used=1, extract_calls_used=0,
            )
        result = build_research_result(
            research_question="Which is open?",
            answer_target="Confirm Sunday opening hours",
            decision=decision,
            observations=observations,
            execution_status="completed",
            stop_reason="evidence_sufficient",
        )
        self.assertEqual([item.relation for item in result.findings], [
            "direct", "adjacent_context",
        ])

    def test_finish_normalizes_provider_drift_without_losing_evidence(self):
        source_url = "https://www.playsport.cc/gamesData/result?allianceid=1"
        provider_result = SimpleNamespace(
            input_tokens=1, output_tokens=1, duration_ms=1, content="",
            tool_calls=[{"name": "web_finish_decision", "arguments": {
                "has_direct_evidence": "true",
                "direct_evidence_complete": False,
                "evidence_conflicts_target": False,
                "findings": ["Guardians 8:2 White Sox.", "Twins 8:6 Brewers."],
                "limitations": ["one", "two", "three", "four must be truncated"],
                "supporting_source_urls": [source_url],
                "supporting_source_types": ["sportsbook result page"],
                "missing_evidence": "x" * 400,
                "provider_extra_prose": "must not reject the whole decision",
            }}],
        )
        observations = [{"tool": "web.search", "result": _search_result(source_url)}]
        with patch(
            "services.ayue_agent.v3.sub_agents.web_agent.generate_chat_completion_with_tools",
            return_value=provider_result,
        ):
            decision, metrics = decide_web(
                _slice(), task_brief="Find today's MLB scores", round_index=3,
                observations=observations, tool_calls_used=3,
                search_calls_used=2, extract_calls_used=1,
            )
        result = build_research_result(
            research_question="MLB today?", answer_target="Find today's MLB scores",
            decision=decision, observations=observations,
            execution_status="completed", stop_reason="evidence_sufficient",
        )
        self.assertEqual(metrics.error, "")
        self.assertEqual(len(decision.limitations), 3)
        self.assertEqual(len(decision.assessment.missing_evidence), 300)
        self.assertEqual(result.status, "partial")
        self.assertEqual(len(result.findings), 2)
        self.assertEqual(result.sources[0].source_type, "other")

    def test_search_query_is_anchored_to_planner_target(self):
        target = "Find 2026-08-10 to 2026-08-16 events at Kaohsiung Pier-2 Art Center."
        query = anchor_web_search_query(target, "weekend events")
        self.assertIn("Kaohsiung Pier-2 Art Center", query)
        self.assertIn("2026-08-10", query)
        self.assertIn("weekend events", query)
        self.assertLessEqual(len(query), 300)

    def test_place_query_keeps_candidate_and_unresolved_criterion(self):
        query = anchor_place_search_query(
            candidate_name="A Cafe",
            address_summary="楠梓區",
            answer_target="確認今晚十點後是否營業",
            suggested_query="hours",
        )
        self.assertIn("A Cafe", query)
        self.assertIn("今晚十點後是否營業", query)
        self.assertLessEqual(len(query), 300)

    def test_place_finding_requires_same_subject_source(self):
        ref_a = "place_candidate_0123456789abcdef"
        ref_b = "place_candidate_fedcba9876543210"
        observations = [{
            "tool": "web.search",
            "subject_ref": ref_a,
            "result": _search_result(),
        }]
        decision = WebResearchDecision(
            action="finish",
            assessment=WebEvidenceAssessmentV1(
                target_alignment="aligned", coverage="direct_sufficient",
            ),
            status="answered",
            findings=[{
                "claim": "B is open",
                "relation": "direct",
                "subject_ref": ref_b,
                "source_urls": ["https://example.com/forum/post"],
            }],
        )
        result = build_research_result(
            research_question="q", answer_target="a", decision=decision,
            observations=observations, execution_status="completed",
            stop_reason="evidence_sufficient", allowed_subject_refs={ref_a, ref_b},
        )
        self.assertEqual(result.findings, [])

    def test_scheduler_executes_anchored_query(self):
        target = "Find 2026-08-10 to 2026-08-16 events at Kaohsiung Pier-2 Art Center."
        decisions = [
            WebResearchDecision(action="search", queries=["weekend events"], recency="2026"),
            self._finish(),
        ]
        executed_queries = []

        def fake_execute(tc, raw_ctx, *, clock):
            executed_queries.append(tc.arguments["query"])
            return SimpleNamespace(ok=True, data=_search_result())

        with patch.object(web_runtime, "web_enabled", return_value=True), \
             patch.object(web_runtime.web_agent, "decide", side_effect=[
                 (decisions[0], SubAgentMetrics(input_tokens=1)),
                 (decisions[1], SubAgentMetrics(input_tokens=1)),
             ]), \
             patch.object(guarded_execution, "execute_tool", side_effect=fake_execute):
            results, _metrics = _run_web(
                SubTask(id="web1", agent="web", task_brief=target),
                _turn(target), _slice(target), seen_keys=set(),
                guard_lock=threading.Lock(), on_progress=None,
                run_id="run", trace=_trace(), debug_enabled=False,
            )
        self.assertEqual(results[0].status, SubTaskStatus.OK)
        self.assertIn("Kaohsiung Pier-2 Art Center", executed_queries[0])
        self.assertIn("2026-08-10", executed_queries[0])

    def test_scheduler_passes_place_candidates_and_preserves_subject_binding(self):
        target = "Find tonight's public hours at a nearby cafe."
        slc = _slice(target)
        slc.payload["prior_observations"] = [{
            "task_id": "places1",
            "status": "ok",
            "tool": "places.search_nearby",
            "result": {"places": [{
                "provider": "openstreetmap",
                "name": "A Cafe",
                "category": "cafe",
                "address_summary": "Central District",
                "distance_m": 700,
                "map_url": "https://www.openstreetmap.org/?mlat=22.62&mlon=120.31#map=18/22.62/120.31",
            }]},
        }]
        captured_candidates = []
        executed_queries = []

        def fake_decide(*args, **kwargs):
            candidates = kwargs.get("place_candidates") or []
            captured_candidates.append(candidates)
            ref = candidates[0]["candidate_ref"]
            if len(captured_candidates) == 1:
                return WebResearchDecision(
                    action="search", queries=["opening hours"], subject_refs=[ref],
                ), SubAgentMetrics(input_tokens=1)
            return WebResearchDecision(
                action="finish",
                assessment=WebEvidenceAssessmentV1(
                    target_alignment="aligned", coverage="direct_sufficient",
                ),
                status="answered",
                findings=[{
                    "claim": "A Cafe is open tonight.",
                    "relation": "direct",
                    "subject_ref": ref,
                    "source_urls": ["https://example.com/forum/post"],
                }],
            ), SubAgentMetrics(input_tokens=1)

        def fake_execute(tc, raw_ctx, *, clock):
            executed_queries.append(tc.arguments["query"])
            return SimpleNamespace(ok=True, data=_search_result())

        with patch.object(web_runtime, "web_enabled", return_value=True), \
             patch.object(web_runtime.web_agent, "decide", side_effect=fake_decide), \
             patch.object(guarded_execution, "execute_tool", side_effect=fake_execute):
            results, _metrics = _run_web(
                SubTask(id="web1", agent="web", task_brief=target),
                _turn(target), slc, seen_keys=set(), guard_lock=threading.Lock(),
                on_progress=None, run_id="run", trace=_trace(), debug_enabled=False,
            )

        self.assertEqual(len(captured_candidates), 2)
        self.assertEqual(captured_candidates[0][0]["name"], "A Cafe")
        self.assertNotIn("map_url", captured_candidates[0][0])
        self.assertIn("A Cafe", executed_queries[0])
        observation = results[0].observation
        self.assertEqual(observation["status"], "answered")
        self.assertEqual(observation["findings"][0]["subject_ref"], captured_candidates[0][0]["candidate_ref"])

    def test_optional_place_bootstrap_searches_candidates_before_web_decision(self):
        target = "Find a casual public update about a nearby cafe."
        slc = _slice(target)
        slc.payload["prior_observations"] = [{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"places": [{
                "provider": "openstreetmap", "name": "A Cafe", "category": "cafe",
                "address_summary": "Central District", "distance_m": 700,
                "map_url": "https://www.openstreetmap.org/?mlat=22.62&mlon=120.31#map=18/22.62/120.31",
            }]},
        }]
        executed_queries = []

        def fake_execute(tc, raw_ctx, *, clock):
            executed_queries.append(tc.arguments["query"])
            return SimpleNamespace(ok=True, data=_search_result())

        def fake_decide(*args, **kwargs):
            decision = self._finish()
            decision.findings[0].subject_ref = kwargs["place_candidates"][0]["candidate_ref"]
            return decision, SubAgentMetrics(input_tokens=1)

        with patch.object(web_runtime.config, "AYUE_V3_WEB_PLACE_BOOTSTRAP_FAST_PATH", True), \
             patch.object(web_runtime, "web_enabled", return_value=True), \
             patch.object(web_runtime.web_agent, "decide", side_effect=fake_decide) as decide, \
             patch.object(guarded_execution, "execute_tool", side_effect=fake_execute):
            results, _metrics = _run_web(
                SubTask(id="web1", agent="web", task_brief=target),
                _turn(target), slc, seen_keys=set(), guard_lock=threading.Lock(),
                on_progress=None, run_id="run", trace=_trace(), debug_enabled=False,
            )

        self.assertEqual(len(executed_queries), 1)
        self.assertIn("A Cafe", executed_queries[0])
        self.assertEqual(decide.call_args.kwargs["search_calls_used"], 1)
        self.assertEqual(decide.call_args.kwargs["tool_calls_used"], 1)
        self.assertEqual(results[0].observation["status"], "answered")

    def _finish(self, *, coverage="direct_sufficient", status="answered", relation="direct"):
        return WebResearchDecision(
            action="finish",
            assessment=WebEvidenceAssessmentV1(
                target_alignment="aligned", coverage=coverage, missing_evidence="",
            ),
            status=status,
            findings=[{
                "claim": "The requested proposition is directly supported.",
                "relation": relation,
                "source_urls": ["https://example.com/forum/post"],
                "source_types": ["forum"],
            }],
        )

    def test_search_refined_search_extract_finish_uses_three_tools_then_finish(self):
        decisions = [
            (WebResearchDecision(action="search", queries=["initial discovery"]), SubAgentMetrics(input_tokens=1)),
            (WebResearchDecision(action="search", queries=["refined context"]), SubAgentMetrics(input_tokens=1)),
            (WebResearchDecision(
                action="extract", urls=["https://example.com/forum/post"],
                extract_query="relevant details",
            ), SubAgentMetrics(input_tokens=1)),
            (self._finish(), SubAgentMetrics(input_tokens=1)),
        ]
        calls = []

        def fake_execute(tc, raw_ctx, *, clock):
            calls.append(tc.name)
            if tc.name == "web.search":
                return SimpleNamespace(ok=True, data=_search_result())
            return SimpleNamespace(ok=True, data={"pages": [{
                "url": "https://example.com/forum/post",
                "content": "Direct public evidence.", "truncated": False,
            }]})

        with patch.object(web_runtime, "web_enabled", return_value=True), \
             patch.object(web_runtime.web_agent, "decide", side_effect=decisions) as decide, \
             patch.object(guarded_execution, "execute_tool", side_effect=fake_execute):
            results, _metrics = _run_web(
                SubTask(id="web1", agent="web", task_brief="Find nuanced public evidence"),
                _turn(), _slice(), seen_keys=set(), guard_lock=threading.Lock(),
                on_progress=None, run_id="run", trace=_trace(), debug_enabled=False,
            )

        self.assertEqual(calls, ["web.search", "web.search", "web.extract"])
        self.assertEqual(decide.call_count, 4)
        self.assertEqual(decide.call_args_list[-1].kwargs["tool_calls_used"], 3)
        self.assertEqual(results[0].observation["status"], "answered")

    def test_simple_lookup_can_finish_after_search_without_extract(self):
        decisions = [
            (WebResearchDecision(action="search", queries=["explicit lookup"]), SubAgentMetrics(input_tokens=1)),
            (self._finish(), SubAgentMetrics(input_tokens=1)),
        ]
        with patch.object(web_runtime, "web_enabled", return_value=True), \
             patch.object(web_runtime.web_agent, "decide", side_effect=decisions) as decide, \
             patch.object(guarded_execution, "execute_tool", return_value=SimpleNamespace(
                 ok=True, data=_search_result(),
             )) as execute:
            results, _metrics = _run_web(
                SubTask(id="web1", agent="web", task_brief="Find one explicit fact"),
                _turn(), _slice(), seen_keys=set(), guard_lock=threading.Lock(),
                on_progress=None, run_id="run", trace=_trace(), debug_enabled=False,
            )

        self.assertEqual(execute.call_count, 1)
        self.assertEqual(decide.call_count, 2)
        self.assertEqual(results[0].observation["status"], "answered")

    def test_initial_parallel_search_still_respects_total_tool_budget(self):
        decisions = [
            (WebResearchDecision(action="search", queries=["first", "second"]), SubAgentMetrics(input_tokens=1)),
            (WebResearchDecision(action="search", queries=["refined"]), SubAgentMetrics(input_tokens=1)),
            (self._finish(), SubAgentMetrics(input_tokens=1)),
        ]
        with patch.object(web_runtime, "web_enabled", return_value=True), \
             patch.object(web_runtime.web_agent, "decide", side_effect=decisions) as decide, \
             patch.object(guarded_execution, "execute_tool", return_value=SimpleNamespace(
                 ok=True, data=_search_result(),
             )) as execute:
            results, _metrics = _run_web(
                SubTask(id="web1", agent="web", task_brief="Compare public sources"),
                _turn(), _slice(), seen_keys=set(), guard_lock=threading.Lock(),
                on_progress=None, run_id="run", trace=_trace(), debug_enabled=False,
            )

        self.assertEqual(execute.call_count, 3)
        self.assertEqual(decide.call_args_list[-1].kwargs["tool_calls_used"], 3)
        self.assertEqual(results[0].observation["status"], "answered")

    def test_nuanced_policy_exposes_extract_and_prefers_page_context(self):
        provider_result = SimpleNamespace(
            input_tokens=1, output_tokens=1, duration_ms=1, content="",
            tool_calls=[{"name": "web_extract_decision", "arguments": {
                "urls": ["https://example.com/forum/post"],
                "extract_query": "comparison details",
            }}],
        )
        with patch(
            "services.ayue_agent.v3.sub_agents.web_agent.generate_chat_completion_with_tools",
            return_value=provider_result,
        ):
            decision, metrics = decide_web(
                _slice("Compare and recommend the best option with details"),
                task_brief="Compare and recommend the best option with details",
                round_index=2,
                observations=[{"tool": "web.search", "result": _search_result()}],
                tool_calls_used=1, search_calls_used=1, extract_calls_used=0,
            )
        self.assertEqual(decision.action, "extract")
        self.assertIn("source discovery", metrics.prompt_raw)
        self.assertIn("relevance and authority", metrics.prompt_raw)

    def test_web_agent_keeps_search_observation_before_extract(self):
        decisions = [
            WebResearchDecision(action="search", queries=["exact public question"]),
            WebResearchDecision(action="extract", urls=["https://example.com/forum/post"]),
            self._finish(),
        ]
        calls = []

        def fake_execute(tc, raw_ctx, *, clock):
            calls.append(tc.name)
            if tc.name == "web.search":
                return SimpleNamespace(ok=True, data=_search_result())
            return SimpleNamespace(ok=True, data={"pages": [{
                "url": "https://example.com/forum/post",
                "content": "Direct public evidence.", "truncated": False,
            }]})

        with patch.object(web_runtime, "web_enabled", return_value=True), \
             patch.object(web_runtime.web_agent, "decide", side_effect=[
                 (decisions[0], SubAgentMetrics(input_tokens=1)),
                 (decisions[1], SubAgentMetrics(input_tokens=1)),
                 (decisions[2], SubAgentMetrics(input_tokens=1)),
             ]), \
             patch.object(guarded_execution, "execute_tool", side_effect=fake_execute):
            results, metrics = _run_web(
                SubTask(id="web1", agent="web", task_brief="Find exact public evidence"),
                _turn(), _slice(), seen_keys=set(), guard_lock=threading.Lock(),
                on_progress=None, run_id="run", trace=_trace(), debug_enabled=False,
            )
        self.assertEqual(calls, ["web.search", "web.extract"])
        self.assertEqual(results[0].observation["status"], "answered")
        self.assertEqual(metrics.input_tokens, 3)

    def test_adjacent_only_is_insufficient_evidence(self):
        decisions = [
            WebResearchDecision(action="search", queries=["background recap"]),
            WebResearchDecision(
                action="finish",
                assessment=WebEvidenceAssessmentV1(
                    target_alignment="aligned", coverage="adjacent_only",
                    missing_evidence="No direct evidence was found.",
                ),
                status="insufficient_evidence",
                findings=[{
                    "claim": "A related recap exists.", "relation": "adjacent_context",
                    "source_urls": ["https://example.com/forum/post"],
                    "source_types": ["news"],
                }],
            ),
        ]
        with patch.object(web_runtime, "web_enabled", return_value=True), \
             patch.object(web_runtime.web_agent, "decide", side_effect=[
                 (decisions[0], SubAgentMetrics(input_tokens=1)),
                 (decisions[1], SubAgentMetrics(input_tokens=1)),
             ]), \
             patch.object(guarded_execution, "execute_tool", return_value=SimpleNamespace(
                 ok=True, data=_search_result(),
             )):
            results, _metrics = _run_web(
                SubTask(id="web1", agent="web", task_brief="Find exact reactions"),
                _turn(), _slice(), seen_keys=set(), guard_lock=threading.Lock(),
                on_progress=None, run_id="run", trace=_trace(), debug_enabled=False,
            )
        self.assertEqual(results[0].observation["status"], "insufficient_evidence")
        self.assertEqual(results[0].observation["coverage"], "adjacent_only")

    def test_finish_before_observation_fails_closed(self):
        with patch.object(web_runtime, "web_enabled", return_value=True), \
             patch.object(web_runtime.web_agent, "decide", return_value=(
                 self._finish(), SubAgentMetrics(input_tokens=1),
             )), \
             patch.object(guarded_execution, "execute_tool") as execute:
            results, _metrics = _run_web(
                SubTask(id="web1", agent="web", task_brief="Find evidence"),
                _turn(), _slice(), seen_keys=set(), guard_lock=threading.Lock(),
                on_progress=None, run_id="run", trace=_trace(), debug_enabled=False,
            )
        self.assertEqual(results[0].observation["execution_status"], "unavailable")
        execute.assert_not_called()

    def test_parser_failure_after_successful_search_preserves_sources(self):
        decisions = [WebResearchDecision(action="search", queries=["direct query"]), None, None]
        with patch.object(web_runtime, "web_enabled", return_value=True), \
             patch.object(web_runtime.web_agent, "decide", side_effect=[
                 (decisions[0], SubAgentMetrics(input_tokens=1)),
                 (None, SubAgentMetrics(input_tokens=1, error="web_decision_schema_invalid")),
                 (None, SubAgentMetrics(input_tokens=1, error="web_decision_schema_invalid")),
                 (None, SubAgentMetrics(input_tokens=1, error="web_decision_schema_invalid")),
                 (None, SubAgentMetrics(input_tokens=1, error="web_decision_schema_invalid")),
             ]), \
             patch.object(guarded_execution, "execute_tool", return_value=SimpleNamespace(
                 ok=True, data=_search_result(),
             )):
            results, _metrics = _run_web(
                SubTask(id="web1", agent="web", task_brief="Find evidence"),
                _turn(), _slice(), seen_keys=set(), guard_lock=threading.Lock(),
                on_progress=None, run_id="run", trace=_trace(), debug_enabled=False,
            )
        observation = results[0].observation
        self.assertEqual(observation["execution_status"], "degraded")
        self.assertEqual(observation["stop_reason"], "model_failure")
        self.assertEqual(len(observation["sources"]), 1)

    def test_scheduler_uses_finish_only_recovery_after_late_decision_failure(self):
        decisions = [
            WebResearchDecision(action="search", queries=["direct query"]),
            None,
            self._finish(),
        ]
        with patch.object(web_runtime, "web_enabled", return_value=True), \
             patch.object(web_runtime.web_agent, "decide", side_effect=[
                 (decisions[0], SubAgentMetrics(input_tokens=1)),
                 (None, SubAgentMetrics(input_tokens=1, error="web_decision_provider_5xx")),
                 (decisions[2], SubAgentMetrics(input_tokens=1)),
             ]) as decide, \
             patch.object(guarded_execution, "execute_tool", return_value=SimpleNamespace(
                 ok=True, data=_search_result(),
             )) as execute:
            results, _metrics = _run_web(
                SubTask(id="web1", agent="web", task_brief="Find evidence"),
                _turn(), _slice(), seen_keys=set(), guard_lock=threading.Lock(),
                on_progress=None, run_id="run", trace=_trace(), debug_enabled=False,
            )
        self.assertEqual(results[0].observation["status"], "answered")
        self.assertEqual(decide.call_count, 3)
        self.assertEqual(execute.call_count, 1)

    def test_failed_research_decision_cannot_consume_budget_or_remove_finish_phase(self):
        decisions = [
            (WebResearchDecision(action="search", queries=["initial discovery"]), SubAgentMetrics(input_tokens=1)),
            (WebResearchDecision(action="search", queries=["refined context"]), SubAgentMetrics(input_tokens=1)),
            (WebResearchDecision(
                action="extract", urls=["https://example.com/forum/post"],
                extract_query="relevant details",
            ), SubAgentMetrics(input_tokens=1)),
            (None, SubAgentMetrics(input_tokens=1, error="web_decision_provider_5xx")),
            (self._finish(), SubAgentMetrics(input_tokens=1)),
        ]

        def fake_execute(tc, raw_ctx, *, clock):
            if tc.name == "web.extract":
                return SimpleNamespace(ok=True, data={"pages": [{
                    "url": "https://example.com/forum/post",
                    "content": "Direct public evidence.",
                    "truncated": False,
                }]})
            return SimpleNamespace(ok=True, data=_search_result())

        with patch.object(web_runtime, "web_enabled", return_value=True), \
             patch.object(web_runtime.web_agent, "decide", side_effect=decisions) as decide, \
             patch.object(guarded_execution, "execute_tool", side_effect=fake_execute) as execute:
            results, _metrics = _run_web(
                SubTask(id="web1", agent="web", task_brief="Find evidence"),
                _turn(), _slice(), seen_keys=set(), guard_lock=threading.Lock(),
                on_progress=None, run_id="run", trace=_trace(), debug_enabled=False,
            )

        self.assertEqual(results[0].observation["status"], "answered")
        self.assertEqual(execute.call_count, 3)
        self.assertEqual(decide.call_count, 5)
        self.assertEqual(decide.call_args_list[3].kwargs["tool_calls_used"], 3)
        self.assertEqual(decide.call_args_list[4].kwargs["tool_calls_used"], 3)
        self.assertEqual(decide.call_args_list[4].kwargs["search_calls_used"], 2)
        self.assertEqual(decide.call_args_list[4].kwargs["extract_calls_used"], 1)
        self.assertTrue(decide.call_args_list[4].kwargs["finish_only"])

    def test_web_unavailable_is_distinct_from_no_evidence(self):
        with patch.object(web_runtime, "web_enabled", return_value=False), \
             patch.object(web_runtime.web_agent, "decide") as decide:
            results, _metrics = _run_web(
                SubTask(id="web1", agent="web", task_brief="Find evidence"),
                _turn(), _slice(), seen_keys=set(), guard_lock=threading.Lock(),
                on_progress=None, run_id="run", trace=_trace(), debug_enabled=False,
            )
        self.assertEqual(results[0].observation["execution_status"], "unavailable")
        self.assertEqual(results[0].observation["stop_reason"], "tool_unavailable")
        decide.assert_not_called()

    def test_projection_is_bounded_and_keeps_only_safe_urls(self):
        observations = [{"tool": "web.search", "result": {"results": [
            {"title": "safe", "url": "https://example.com/a", "snippet": "x" * 2000},
            {"title": "private", "url": "http://127.0.0.1/a", "snippet": "private"},
        ]}}]
        rows = project_web_observations(observations)[0]["result"]["results"]
        self.assertEqual(len(rows), 1)
        self.assertLessEqual(len(rows[0]["snippet"]), 600)
        self.assertEqual(rows[0]["source_ref"], "web_source_01")

    def test_extract_projection_keeps_8000_chars_per_page(self):
        observations = [{"tool": "web.extract", "result": {"pages": [{
            "url": "https://example.com/article",
            "content": "x" * 8_001,
            "truncated": True,
        }]}}]
        pages = project_web_observations(observations)[0]["result"]["pages"]
        self.assertEqual(len(pages[0]["content"]), 8_000)
        self.assertTrue(pages[0]["truncated"])

    def test_source_ref_resolves_only_against_current_observation_catalog(self):
        observations = [{"tool": "web.search", "result": _search_result()}]
        decision = WebResearchDecision(
            action="finish",
            assessment=WebEvidenceAssessmentV1(coverage="direct_sufficient"),
            status="answered",
            findings=[{
                "claim": "直接相關公告",
                "relation": "direct",
                "source_refs": ["web_source_01"],
            }],
        )
        result = build_research_result(
            research_question="活動？", answer_target="活動公告", decision=decision,
            observations=observations, execution_status="completed",
            stop_reason="evidence_sufficient",
        )
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.findings[0].source_urls, ["https://example.com/forum/post"])

    def test_synthesizer_surfaces_safe_adjacent_findings_without_llm(self):
        slc = AgentContextSlice(agent="synthesizer", payload={
            "message": "Find exact reactions", "recent_messages": [],
            "recent_context": "", "user_location": "", "clock": {},
            "observations": [{
                "task_id": "web1", "status": "ok", "tool": None,
                "result": {
                    "schema_version": "web_research.v1",
                    "research_question": "Find exact reactions",
                    "answer_target": "forum reactions",
                    "status": "insufficient_evidence",
                    "execution_status": "completed",
                    "coverage": "adjacent_only",
                    "findings": [{
                        "claim": "A related public recap exists.",
                        "relation": "adjacent_context",
                        "source_urls": ["https://example.com/forum/post"],
                    }],
                    "sources": [{
                        "url": "https://example.com/forum/post",
                        "title": "Related recap", "source_type": "news",
                    }],
                    "limitations": ["No direct forum evidence was found."],
                    "stop_reason": "no_direct_evidence",
                },
            }],
        })
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
        ) as provider:
            reply, _cards, metrics = synthesize(slc)
        provider.assert_not_called()
        self.assertIn("A related public recap exists", reply)
        self.assertEqual(metrics.fallback_reason, "web_research_insufficient")

    def test_casual_source_only_result_is_not_blocked_by_strict_sentence(self):
        slc = AgentContextSlice(agent="synthesizer", payload={
            "message": "最近有什麼活動？", "recent_messages": [],
            "recent_context": "", "user_location": "", "clock": {},
            "observations": [{
                "task_id": "web1", "status": "ok", "tool": None,
                "result": {
                    "schema_version": "web_research.v1",
                    "research_question": "最近有什麼活動？",
                    "answer_target": "近期活動探索",
                    "evidence_policy": "casual_discovery",
                    "status": "insufficient_evidence",
                    "execution_status": "degraded",
                    "coverage": "none", "findings": [],
                    "sources": [{"url": "https://example.com/event", "title": "活動公告", "source_type": "social"}],
                    "limitations": ["最後整理未完成"],
                    "stop_reason": "model_failure",
                },
            }],
        })
        with patch("services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools") as provider:
            reply, _cards, _metrics = synthesize(slc)
        provider.assert_not_called()
        self.assertIn("活動公告", reply)
        self.assertNotIn("目前查到的公開資訊還不足以確認你問的內容", reply)


if __name__ == "__main__":
    unittest.main()
