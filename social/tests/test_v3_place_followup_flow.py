import threading
import unittest
from unittest.mock import patch

import config
from services.ayue_agent.contracts import AgentTurnContext, PublicAgentTurnContext, ToolResult
from services.ayue_agent.v3.contracts import AgentContextSlice, SubTask, ToolProposal
from services.ayue_agent.v3.place_references import (
    clear_runtime_state,
    get_candidate_set,
    public_resolution,
    recent_selected_projection,
    replace_presented_candidates,
    resolve_message_reference,
)
from services.ayue_agent.v3.scheduler import _run_sub_task, run_public_agent_turn_v3
from services.ayue_agent.v3.place_followups import clear_runtime_state as clear_followup_state
from services.ayue_agent.v3.sub_agents.base import SubAgentMetrics
from services.ayue_agent.v3.sub_agents.web_agent import WebResearchDecision
from services.ayue_agent.v3.web_research import (
    WebEvidenceAssessmentV1,
    WebResearchFindingDraft,
)
from services.ai_service import ToolCallResult


def _fc(arguments):
    return ToolCallResult(
        content="",
        tool_calls=[{"name": "decompose_tasks", "arguments": arguments}],
    )


def _public_turn(message):
    return PublicAgentTurnContext(
        user_id="integration-owner",
        room_id="integration-room",
        message=message,
    )


class V3PlaceFollowupFlowTests(unittest.TestCase):
    def setUp(self):
        clear_runtime_state()
        clear_followup_state()

    def tearDown(self):
        clear_runtime_state()
        clear_followup_state()

    def test_discover_then_details_then_reviews_stays_on_selected_place(self):
        places = [
            {
                "name": name,
                "category": "restaurant",
                "address_summary": "803臺灣高雄市鹽埕區大禮街",
                "provider": "google",
                "place_id": f"ChIJfried{index}",
                "map_url": f"https://www.google.com/maps/place/fried-{index}",
            }
            for index, name in enumerate((
                "Tu酥館台式炸雞鹽埕店",
                "原大禮街香雞排",
                "駁二大禮市場雞排",
                "鹽埕炸物攤",
                "阿亮香雞排 鹽埕店",
            ), start=1)
        ]
        planner_results = [
            _fc({
                "mode": "tasks",
                "write_intent": "none",
                "tasks": [
                    {
                        "id": "p1", "agent": "places", "place_mode": "discover",
                        "depends_on": [], "task_brief": "推薦五間鹽埕區雞排店",
                    },
                    {
                        "id": "s1", "agent": "synthesizer", "depends_on": ["p1"],
                        "task_brief": "整理推薦結果",
                    },
                ],
            }),
            _fc({
                "mode": "tasks",
                "write_intent": "none",
                "tasks": [
                    {
                        "id": "p1", "agent": "places", "place_mode": "details",
                        "depends_on": [], "task_brief": "查原大禮街香雞排（高雄市鹽埕區）的地址與結構化資料",
                    },
                    {
                        "id": "s1", "agent": "synthesizer", "depends_on": ["p1"],
                        "task_brief": "直接回答選定店家的資料",
                    },
                ],
            }),
            _fc({
                "mode": "tasks",
                "write_intent": "none",
                "tasks": [
                    {
                        "id": "p1", "agent": "places", "place_mode": "reviews",
                        "depends_on": [], "task_brief": "查原大禮街香雞排（高雄市鹽埕區）的好不好吃與口味評論",
                    },
                    {
                        "id": "w1", "agent": "web", "depends_on": ["p1"],
                        "evidence_policy": "casual_discovery",
                        "task_brief": "研究原大禮街香雞排的公開口味食記",
                    },
                    {
                        "id": "s1", "agent": "synthesizer", "depends_on": ["w1"],
                        "task_brief": "先回答口味傾向，再說來源限制",
                    },
                ],
            }),
            _fc({
                "mode": "tasks",
                "write_intent": "none",
                "tasks": [
                    {
                        "id": "p1", "agent": "places", "place_mode": "details",
                        "depends_on": [], "task_brief": "查原大禮街香雞排（高雄市鹽埕區）的營業資訊",
                    },
                    {
                        "id": "s1", "agent": "synthesizer", "depends_on": ["p1"],
                        "task_brief": "整理已選店家公開資訊",
                    },
                ],
            }),
            _fc({
                "mode": "tasks",
                "write_intent": "none",
                "tasks": [
                    {
                        "id": "p1", "agent": "places", "place_mode": "details",
                        "depends_on": [], "task_brief": "再次查原大禮街香雞排（高雄市鹽埕區）的詳細資訊",
                    },
                    {
                        "id": "w1", "agent": "web", "web_mode": "place_hours_fallback",
                        "depends_on": ["p1"], "task_brief": "再次補查已選店家營業資訊",
                    },
                    {
                        "id": "s1", "agent": "synthesizer", "depends_on": ["p1", "w1"],
                        "task_brief": "整理已選店家公開資訊",
                    },
                ],
            }),
            _fc({
                "mode": "tasks",
                "write_intent": "none",
                "tasks": [
                    {
                        "id": "w1", "agent": "web", "web_mode": "public_lookup",
                        "depends_on": [], "task_brief": "查第一個活動的公開資訊",
                    },
                    {
                        "id": "s1", "agent": "synthesizer", "depends_on": ["w1"],
                        "task_brief": "整理活動資訊",
                    },
                ],
            }),
        ]
        place_agent_results = [
            ToolCallResult(content="", tool_calls=[{
                "name": "places.search_nearby",
                "arguments": {
                    "anchor": "高雄市鹽埕區",
                    "categories": ["restaurant"],
                    "cuisine": "雞排",
                    "limit": 5,
                },
            }]),
            ToolCallResult(content="", tool_calls=[{
                "name": "places.resolve_place",
                "arguments": {"query": "原大禮街香雞排"},
            }]),
            ToolCallResult(content="", tool_calls=[{
                "name": "places.resolve_place",
                "arguments": {"query": "原大禮街香雞排"},
            }]),
            ToolCallResult(content="", tool_calls=[{
                "name": "places.resolve_place",
                "arguments": {"query": "原大禮街香雞排"},
            }]),
            ToolCallResult(content="", tool_calls=[{
                "name": "places.resolve_place",
                "arguments": {"query": "原大禮街香雞排"},
            }]),
        ]
        synth_results = [
            # Cards-off ordinary prose is accepted and the hidden order is
            # derived from the first unique public mention of each trusted name.
            ToolCallResult(content=(
                "可以依序看 Tu酥館台式炸雞鹽埕店、原大禮街香雞排、"
                "駁二大禮市場雞排、鹽埕炸物攤和阿亮香雞排 鹽埕店。"
            ), tool_calls=[]),
            ToolCallResult(content="原大禮街香雞排位於高雄市鹽埕區大禮街。", tool_calls=[]),
            ToolCallResult(content="查到的一篇食記提到外皮酥脆，但也覺得偏油；目前只有一篇來源。", tool_calls=[]),
            ToolCallResult(content="已延續原大禮街香雞排並補查公開資訊。", tool_calls=[]),
            ToolCallResult(content="再次延續原大禮街香雞排並補查公開資訊。", tool_calls=[]),
            ToolCallResult(content="第一個活動的公開資訊已整理。", tool_calls=[]),
        ]
        executed_tools = []

        def planner_provider(*_args, **_kwargs):
            return planner_results.pop(0)

        def places_provider(*_args, **_kwargs):
            return place_agent_results.pop(0)

        def fake_execute(call, _raw_ctx, *, clock):
            executed_tools.append(call.name)
            if call.name == "places.search_nearby":
                return ToolResult(ok=True, data={"places": places}, private_data={})
            if call.name == "places.resolve_place":
                return ToolResult(ok=True, data={
                    "found": True,
                    "place": dict(places[1]),
                }, private_data={})
            if call.name == "web.search":
                return ToolResult(ok=True, data={"results": [{
                    "title": "原大禮街香雞排食記",
                    "url": "https://example.com/original-dalijie-review",
                    "snippet": "外皮酥脆，但有人覺得偏油。",
                }]}, private_data={})
            raise AssertionError(f"unexpected tool {call.name}")

        def fake_web_decide(context_slice, **kwargs):
            candidates = kwargs.get("place_candidates") or []
            self.assertLessEqual(len(candidates), 1)
            subject_ref = candidates[0]["candidate_ref"] if candidates else None
            if kwargs.get("observations"):
                finding_kwargs = {"subject_ref": subject_ref} if subject_ref else {}
                return WebResearchDecision(
                    action="finish",
                    assessment=WebEvidenceAssessmentV1(
                        target_alignment="aligned",
                        coverage="direct_partial",
                    ),
                    status="partial",
                    findings=[WebResearchFindingDraft(
                        claim="一篇食記提到外皮酥脆，但也覺得偏油。",
                        relation="direct",
                        source_refs=["web_source_01"],
                        source_urls=["https://example.com/original-dalijie-review"],
                        source_types=["article"],
                        **finding_kwargs,
                    )],
                    limitations=["目前只有一篇可直接對應的食記。"],
                ), SubAgentMetrics()
            search_kwargs = {"subject_refs": [subject_ref]} if subject_ref else {}
            return WebResearchDecision(
                action="search",
                queries=["原大禮街香雞排 外皮 口味 食記"],
                **search_kwargs,
            ), SubAgentMetrics()

        def synth_provider(*_args, **_kwargs):
            return synth_results.pop(0)

        def context_builder(ctx, *, clock=None):
            return _public_turn(ctx.message)

        contexts = [
            AgentTurnContext(
                user_id="integration-owner",
                room_id="integration-room",
                message="推薦五間雞排店",
            ),
            AgentTurnContext(
                user_id="integration-owner",
                room_id="integration-room",
                message="第二間怎樣？",
            ),
            AgentTurnContext(
                user_id="integration-owner",
                room_id="integration-room",
                message="它好不好吃啊？",
            ),
            AgentTurnContext(
                user_id="integration-owner",
                room_id="integration-room",
                message="幫我查營業資訊",
            ),
            AgentTurnContext(
                user_id="integration-owner",
                room_id="integration-room",
                message="去查阿",
            ),
            AgentTurnContext(
                user_id="integration-owner",
                room_id="integration-room",
                message="第一個活動推薦嘛",
            ),
        ]

        with patch.object(config, "AYUE_PUBLIC_PLACE_CARDS_ENABLED", False), \
             patch.object(config, "AYUE_V3_WEB_PLACE_BOOTSTRAP_FAST_PATH", False), \
             patch("services.ayue_agent.v3.scheduler.build_public_agent_turn_context", side_effect=context_builder), \
             patch("services.ayue_agent.v3.scheduler.ConfirmationManager.list_active", return_value=[]), \
             patch("services.ayue_agent.v3.planner.generate_chat_completion_with_tools", side_effect=planner_provider), \
             patch("services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools", side_effect=places_provider), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", side_effect=fake_execute), \
             patch("services.ayue_agent.v3.web_runtime.web_enabled", return_value=True), \
             patch("services.ayue_agent.v3.web_runtime.web_agent.decide", side_effect=fake_web_decide), \
             patch("services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools", side_effect=synth_provider):
            first = run_public_agent_turn_v3(contexts[0])
            first_snapshot = get_candidate_set("integration-owner", "integration-room")
            second = run_public_agent_turn_v3(contexts[1])
            second_snapshot = get_candidate_set("integration-owner", "integration-room")
            selected_after_details = recent_selected_projection(
                "integration-owner", "integration-room",
            )
            third = run_public_agent_turn_v3(contexts[2])
            third_snapshot = get_candidate_set("integration-owner", "integration-room")
            fourth = run_public_agent_turn_v3(contexts[3])
            fourth_snapshot = get_candidate_set("integration-owner", "integration-room")
            fifth = run_public_agent_turn_v3(contexts[4])
            fifth_snapshot = get_candidate_set("integration-owner", "integration-room")
            sixth = run_public_agent_turn_v3(contexts[5])
            sixth_snapshot = get_candidate_set("integration-owner", "integration-room")

        self.assertIn("原大禮街香雞排", first.reply)
        self.assertNotIn("2. 原大禮街香雞排", first.reply)
        self.assertTrue(all(snapshot is None for snapshot in (
            first_snapshot, second_snapshot, third_snapshot,
            fourth_snapshot, fifth_snapshot, sixth_snapshot,
        )))
        self.assertIn("原大禮街香雞排", second.reply)
        self.assertIsNone(selected_after_details)
        self.assertNotIn("推薦地點", second.reply)
        self.assertIn("外皮酥脆", third.reply)
        self.assertIn("偏油", third.reply)
        self.assertNotIn("十九甲雞排", third.reply)
        self.assertNotIn("推薦地點", third.reply)
        self.assertIn("原大禮街香雞排", fourth.reply)
        self.assertIn("原大禮街香雞排", fifth.reply)
        self.assertIn("第一個活動", sixth.reply)
        self.assertEqual(
            executed_tools.count("places.search_nearby"),
            1,
        )
        self.assertEqual(executed_tools.count("places.resolve_place"), 4)
        self.assertEqual(executed_tools.count("web.search"), 4)
        self.assertEqual(
            third.sources,
            [{
                "title": "原大禮街香雞排食記",
                "url": "https://example.com/original-dalijie-review",
            }],
        )

    def test_selection_persistence_failure_stops_before_place_or_web_tools(self):
        replace_presented_candidates(
            "integration-owner",
            "integration-room",
            [
                {
                    "name": "第一間",
                    "category": "restaurant",
                    "address_summary": "高雄市鹽埕區",
                    "provider": "google",
                    "place_id": "ChIJfirst",
                    "map_url": "https://www.google.com/maps/place/first",
                },
                {
                    "name": "豪食G炸雞",
                    "category": "restaurant",
                    "address_summary": "高雄市鹽埕區富野路140號",
                    "provider": "google",
                    "place_id": "ChIJfried",
                    "map_url": "https://www.google.com/maps/place/fried",
                },
            ],
            origin_run_id="fried-run",
        )
        planner_result = _fc({
            "mode": "tasks",
            "write_intent": "none",
            "tasks": [
                {
                    "id": "p1", "agent": "places", "place_mode": "details",
                    "depends_on": [], "task_brief": "查豪食G炸雞（高雄市鹽埕區富野路140號）",
                },
                {
                    "id": "s1", "agent": "synthesizer", "depends_on": ["p1"],
                    "task_brief": "整理店家資訊",
                },
            ],
        })
        ctx = AgentTurnContext(
            user_id="integration-owner",
            room_id="integration-room",
            message="第二間的詳細資訊給我",
        )
        with patch(
            "services.ayue_agent.v3.scheduler.build_public_agent_turn_context",
            side_effect=lambda raw, clock=None: _public_turn(raw.message),
        ), patch(
            "services.ayue_agent.v3.scheduler.ConfirmationManager.list_active",
            return_value=[],
        ), patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=planner_result,
        ), patch(
            "services.ayue_agent.v3.scheduler.commit_resolved_selection",
            return_value={"status": "storage_unavailable"},
        ) as commit_selection, patch(
            "services.ayue_agent.v3.scheduler._persist_trace",
        ) as persist_trace, patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=ToolCallResult(content="", tool_calls=[{
                "name": "places.resolve_place",
                "arguments": {"query": "豪食G炸雞"},
            }]),
        ) as places_provider, patch(
            "services.ayue_agent.v3.scheduler.execute_tool",
            return_value=ToolResult(ok=True, data={
                "found": True,
                "place": {
                    "name": "豪食G炸雞",
                    "address_summary": "高雄市鹽埕區富野路140號",
                    "provider": "google",
                    "place_id": "ChIJfried",
                },
            }),
        ) as execute, patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=ToolCallResult(content="豪食G炸雞的資料已重新核對。", tool_calls=[]),
        ):
            result = run_public_agent_turn_v3(ctx)
        places_provider.assert_called_once()
        execute.assert_called_once()
        commit_selection.assert_not_called()
        self.assertIsNone(result.fallback_reason)
        self.assertIn("豪食G炸雞", result.reply)

    def test_details_rejects_a_different_provider_branch(self):
        cards = [
            {
                "name": name,
                "category": "restaurant",
                "address_summary": "高雄市鹽埕區",
                "provider": "google",
                "place_id": f"ChIJbranch{index}",
                "map_url": f"https://www.google.com/maps/place/branch-{index}",
            }
            for index, name in enumerate(("店 A", "原大禮街香雞排"), start=1)
        ]
        replace_presented_candidates(
            "integration-owner", "integration-room", cards, origin_run_id="identity-run",
        )
        resolution = resolve_message_reference(
            "integration-owner", "integration-room", "第二間怎樣？",
        )
        turn = _public_turn("第二間怎樣？").model_copy(update={
            "place_reference_resolution": public_resolution(resolution),
        })
        turn._raw_ctx = AgentTurnContext(
            user_id="integration-owner", room_id="integration-room", message="第二間怎樣？",
        )
        turn._mentioned_ids = []
        task = SubTask(
            id="p1", agent="places", place_mode="details", depends_on=[],
            task_brief="查原大禮街香雞排（高雄市鹽埕區）的詳細資料",
        )
        wrong_branch = {
            **cards[0],
            "name": "原大禮街香雞排－新北樹林店",
            "place_id": "ChIJdifferentbranch",
            "map_url": "https://www.google.com/maps/place/different-branch",
        }
        trace = {"guard_results": [], "tool_results": [], "event_sequence": []}
        with patch(
            "services.ayue_agent.v3.scheduler.run_places",
            return_value=([ToolProposal(
                tool_name="places.resolve_place",
                arguments={"query": "原大禮街香雞排－新北樹林店"},
            )], SubAgentMetrics()),
        ), patch(
            "services.ayue_agent.v3.scheduler.execute_tool",
            return_value=ToolResult(ok=True, data={"found": True, "place": wrong_branch}, private_data={}),
        ) as execute_tool:
            results, _metrics = _run_sub_task(
                task,
                turn,
                [],
                seen_keys=set(),
                step_counts={},
                read_budget_state={"count": 0},
                planner_write_intent="none",
                guard_lock=threading.Lock(),
                on_progress=None,
                run_id="identity-run-followup",
                trace=trace,
            )

        self.assertEqual(results[0].error_code, "sub_agent_invalid_proposal")
        execute_tool.assert_not_called()


if __name__ == "__main__":
    unittest.main()
