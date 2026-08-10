# social_demotest/tests/test_v3_synthesizer.py
import unittest
from unittest.mock import patch

from services.ayue_agent.v3.contracts import AgentContextSlice
from services.ayue_agent.v3.synthesizer import (
    _compose_public_reply_tool_schema,
    _parse_composed_reply,
    synthesize,
)
from services.ai_service import ToolCallResult


def _fc_result(content="", tool_calls=None):
    return ToolCallResult(content=content, tool_calls=tool_calls or [])


class V3SynthesizerTests(unittest.TestCase):
    def test_active_composer_schema_keeps_card_description_parser_only(self):
        schema = _compose_public_reply_tool_schema()["function"]["parameters"]
        block_item = schema["properties"]["blocks"]["items"]
        self.assertNotIn("card_description", block_item["properties"])

    def _slice(self, observations, presentation_mode="default"):
        return AgentContextSlice(agent="synthesizer", payload={
            "message": "你幫我看看行程和附近餐廳",
            "recent_messages": [],
            "recent_context": "",
            "user_location": "台北市",
            "clock": {"timezone": "Asia/Taipei", "local_date": "2026-08-04", "local_time": "20:00"},
            "observations": observations,
            "presentation_mode": presentation_mode,
        })

    def _candidate_cards(self):
        return [
            {"name": "義式料理餐廳", "category": "restaurant", "distance_label": "726 公尺",
             "map_url": "https://maps.example.com/a", "place_id": "abc"},
            {"name": "小酒館", "category": "bar", "distance_label": "200 公尺",
             "map_url": "https://maps.example.com/b", "place_id": "def"},
        ]

    def _web_result(self, *, status="answered", execution_status="completed",
                    findings=None, limitations=None, source_url="https://example.com/verified"):
        return {
            "schema_version": "web_research.v1",
            "research_question": "What did the verified public source say?",
            "answer_target": "verified public source answer",
            "status": status,
            "execution_status": execution_status,
            "coverage": "direct_sufficient" if status == "answered" else "direct_partial",
            "findings": findings if findings is not None else [{
                "claim": "The verified source says the event starts at 19:00.",
                "relation": "direct",
                "source_urls": [source_url],
            }],
            "sources": [{
                "url": source_url,
                "title": "Verified source",
                "source_type": "official",
            }],
            "limitations": limitations or [],
            "stop_reason": "evidence_sufficient" if status == "answered" else "partial_coverage",
        }

    def test_produces_reply_from_observations(self):
        slc = self._slice([
            {"task_id": "t1", "status": "ok", "tool": "calendar.list_my_events",
             "result": {"events": [{"title": "家庭聚餐", "date": "2026-08-09", "start_time": "20:00"}]}},
            {"task_id": "t2", "status": "ok", "tool": "places.search_nearby",
             "result": {"places": [{"name": "義式料理餐廳", "distance_m": 726}]}},
        ])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(
                content="這週末有家庭聚餐，我也找到附近一家義式料理餐廳，要不要一起去？",
                tool_calls=[{"name": "decide_place_cards", "arguments": {"mode": "show_all"}}],
            ),
        ):
            reply, card_decision, _metrics = synthesize(slc)
        self.assertIn("聚餐", reply)
        self.assertIn("餐廳", reply)
        self.assertEqual(card_decision, {"mode": "show_all", "indices": []})

    def test_itinerary_mode_renders_typed_ordered_markdown_and_refs(self):
        slc = self._slice([
            {
                "task_id": "w1", "status": "ok", "tool": "web.research",
                "result": {"primary_activity": {
                    "title": "鹽埕夜間市集", "date": "2026-08-09",
                    "start_time": "18:00", "end_time": "19:30",
                    "venue": "駁二藝術特區", "summary": "公開活動資訊",
                }},
            },
        ], presentation_mode="itinerary")
        cards = [
            {"candidate_ref": "place_1", "name": "午餐店", "category": "restaurant"},
            {"candidate_ref": "place_2", "name": "咖啡廳", "category": "cafe"},
        ]
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "compose_public_reply",
                "arguments": {
                    "messages": ["typed itinerary"],
                    "presentation_class": "grounded_recommendation",
                    "itinerary": {
                        "title": "鹽埕一日安排",
                        "date_label": "日期：2026-08-09",
                        "stops": [
                            {"kind": "activity", "start_time": "18:00", "end_time": "19:30", "title": "鹽埕夜間市集", "activity_ref": "primary_activity"},
                            {"kind": "meal", "start_time": "20:00", "end_time": "21:00", "title": "午餐店", "candidate_ref": "place_1"},
                            {"kind": "cafe", "start_time": "21:15", "end_time": "22:00", "title": "咖啡廳", "candidate_ref": "place_2"},
                        ],
                    },
                },
            }]),
        ):
            reply, card_decision, metrics = synthesize(slc, candidate_cards=cards)
        self.assertIn("鹽埕一日安排", reply)
        self.assertIn("18:00", reply)
        self.assertIn("20:00", reply)
        self.assertEqual(card_decision["selected_candidate_refs"], ["place_1", "place_2"])
        self.assertEqual(metrics.presentation_class, "grounded_recommendation")

    def test_itinerary_provider_failure_returns_string_fallback(self):
        slc = self._slice([], presentation_mode="itinerary")
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            side_effect=RuntimeError("provider unavailable"),
        ):
            reply, card_decision, metrics = synthesize(slc)
        self.assertIsInstance(reply, str)
        self.assertIn("一日行程建議", reply)
        self.assertEqual(card_decision["mode"], "none")
        self.assertEqual(metrics.fallback_reason, "itinerary_fallback")

    def test_activity_fallback_keeps_three_non_overlapping_stops(self):
        slc = self._slice([{
            "task_id": "w1", "status": "ok", "tool": "web.research",
            "result": {"primary_activity": {
                "title": "活動", "date": "2026-08-09",
                "start_time": "18:00", "end_time": "19:30", "venue": "駁二",
            }},
        }], presentation_mode="itinerary")
        cards = [{"candidate_ref": "place_1", "name": "晚餐店", "category": "restaurant"}]
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            side_effect=RuntimeError("provider unavailable"),
        ):
            reply, card_decision, _metrics = synthesize(slc, candidate_cards=cards)
        self.assertIn("活動周邊一日安排", reply)
        self.assertIn("晚餐店", reply)
        self.assertEqual(card_decision["selected_candidate_refs"], ["place_1"])

    def test_verified_observation_uses_grounded_system_mode(self):
        slc = self._slice([{
            "task_id": "t1", "status": "ok", "tool": "calendar.list_my_events",
            "result": {"events": [{"title": "家庭聚餐", "date": "2026-08-09"}]},
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content="有家庭聚餐。"),
        ) as call:
            synthesize(slc)
        self.assertIn("grounded_result", call.call_args.kwargs["system_prompt"])
        self.assertNotIn("clarification_policy", call.call_args.args[0])

    def test_invalid_calendar_command_cannot_supply_a_missing_field_to_synthesizer(self):
        slc = self._slice([{
            "task_id": "t1", "status": "ok", "tool": "calendar.submit_commands",
            "result": {"calendar_command_result": {
                "status": "needs_clarification",
                "clarification": {
                    "code": "invalid_command", "missing_fields": [],
                    "message": "這次行程指令格式無法驗證，請重新描述需求。",
                },
            }},
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content="請重新描述需求。"),
        ) as call:
            synthesize(slc)
        self.assertIn("schema validation 沒有建立 authoritative missing field", call.call_args.kwargs["system_prompt"])

    def test_recent_calendar_mutation_verification_is_server_owned_reply(self):
        slc = self._slice([{
            "task_id": "t1", "status": "ok", "tool": "calendar.verify_recent_mutation",
            "result": {"calendar_mutation_verification": {
                "status": "verified_success", "action": "cancel",
                "label": "8/12 15:00–16:00 看牙醫", "outcome": "success",
            }},
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
        ) as provider:
            reply, card_decision, _metrics = synthesize(slc)
        provider.assert_not_called()
        self.assertIn("已取消", reply)
        self.assertIn("看牙醫", reply)
        self.assertIsNone(card_decision)

    def test_empty_observation_uses_general_conversation_mode(self):
        slc = self._slice([])
        slc.payload["message"] = "我今天有點累"
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content="辛苦了，今天發生什麼事？"),
        ) as call:
            synthesize(slc)
        system_prompt = call.call_args.kwargs["system_prompt"]
        self.assertIn("general_conversation", system_prompt)
        self.assertIn("不得宣稱查過", system_prompt)
        self.assertIn("1–2 句", system_prompt)
        self.assertIn("不要套公式", system_prompt)
        self.assertIn("Web／Places 多來源整理", system_prompt)
        self.assertIn("安全 Markdown 子集", system_prompt)

    def test_general_reply_uses_three_sentence_160_char_envelope(self):
        slc = self._slice([])
        slc.payload["message"] = "今天有點累"
        content = "先別急著把今天的累解釋成自己不夠努力。可以先休息一下，再挑一件最小的事做。你想先說說是哪一段最消耗你嗎？"
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content=content),
        ):
            reply, _card_decision, _metrics = synthesize(slc)
        self.assertIn("先別急著把今天的累解釋成自己不夠努力", reply)
        self.assertIn("最消耗你", reply)
        self.assertLessEqual(len(reply), 160)

    def test_grounded_reply_keeps_longer_detail_envelope(self):
        slc = self._slice([{
            "task_id": "t1", "status": "ok", "tool": "calendar.list_my_events",
            "result": {"events": [{"title": "家庭聚餐", "date": "2026-08-09", "start_time": "20:00"}]},
        }])
        content = "這是第一段已驗證的行程說明，提供日期、開始時間與活動內容，讓你先知道安排。這是第二段補充，交代使用者需要知道的細節，避免把重要資訊藏起來。這是第三段補充，說明目前資料的範圍與限制，方便你判斷下一步。這是第四段補充，只用於完整呈現 grounded result 的必要內容。這是第五段補充，沒有額外加入客套或未驗證的推測。"
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content=content),
        ):
            reply, _card_decision, _metrics = synthesize(slc)
        self.assertIn("grounded result", reply)
        self.assertGreater(len(reply), 160)

    def test_handles_partial_failure(self):
        slc = self._slice([
            {"task_id": "t1", "status": "ok", "tool": "calendar.list_my_events",
             "result": {"events": [{"title": "家庭聚餐", "date": "2026-08-09"}]}},
            {"task_id": "t2", "status": "failed", "tool": None,
             "error_code": "llm_timeout"},
            {"task_id": "t3", "status": "skipped", "tool": None,
             "skip_reason": "dependency_failed"},
        ])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(
                content="這週末有家庭聚餐，不過部分資訊我沒能取得，行程部分要再幫你確認。",
            ),
        ):
            reply, card_decision, _metrics = synthesize(slc)
        self.assertIn("聚餐", reply)
        self.assertIn("行程", reply)
        self.assertIsNone(card_decision)

    def test_fallback_on_provider_error(self):
        slc = self._slice([])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            side_effect=Exception("provider down"),
        ):
            reply, card_decision, _metrics = synthesize(slc)
        self.assertIsInstance(reply, str)
        self.assertTrue(len(reply) > 0)
        self.assertIsNone(card_decision)

    def test_select_decision_parsed(self):
        slc = self._slice([])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(
                content="這家不錯",
                tool_calls=[{"name": "decide_place_cards", "arguments": {"mode": "select", "indices": [0]}}],
            ),
        ):
            reply, card_decision, _metrics = synthesize(slc, candidate_cards=self._candidate_cards())
        self.assertEqual(card_decision, {"mode": "select", "indices": [0]})

    def test_none_decision_parsed(self):
        slc = self._slice([])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(
                content="這次先不顯示",
                tool_calls=[{"name": "decide_place_cards", "arguments": {"mode": "none"}}],
            ),
        ):
            reply, card_decision, _metrics = synthesize(slc, candidate_cards=self._candidate_cards())
        self.assertEqual(card_decision, {"mode": "none", "indices": []})

    def test_composed_reply_binds_recommendation_refs_to_selected_cards(self):
        result = _fc_result(tool_calls=[{
            "name": "compose_public_reply",
            "arguments": {
                "messages": ["先選 A，因為目前公開資訊直接支持它。"],
                "presentation_class": "grounded_recommendation",
                "card_mode": "select",
                "card_intent": "curated",
                "selected_candidate_refs": ["place_candidate_0123456789abcdef"],
                "recommended_candidate_refs": ["place_candidate_0123456789abcdef"],
                "discussed_candidate_refs": ["place_candidate_0123456789abcdef"],
            },
        }])
        parsed = _parse_composed_reply(result, [{
            "candidate_ref": "place_candidate_0123456789abcdef",
            "name": "A", "category": "cafe", "distance_label": "100 公尺",
        }])
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[1]["indices"], [0])

    def test_composed_reply_keeps_explanation_in_markdown_and_drops_legacy_card_description(self):
        result = _fc_result(tool_calls=[{
            "name": "compose_public_reply",
            "arguments": {
                "messages": ["先看這家。"],
                "presentation_class": "grounded_recommendation",
                "card_mode": "select",
                "card_intent": "curated",
                "selected_candidate_refs": ["place_candidate_0123456789abcdef"],
                "recommended_candidate_refs": ["place_candidate_0123456789abcdef"],
                "discussed_candidate_refs": ["place_candidate_0123456789abcdef"],
                "blocks": [{
                    "message_index": 0,
                    "markdown": "",
                    "card_description": "公開頁面列出的營業時間符合你指定的條件。",
                    "candidate_refs": ["place_candidate_0123456789abcdef"],
                }],
            },
        }])
        parsed = _parse_composed_reply(result, [{
            "candidate_ref": "place_candidate_0123456789abcdef",
            "name": "A", "category": "cafe", "distance_label": "100 公尺",
        }])
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[3][0]["markdown"], "")
        self.assertNotIn("card_description", parsed[3][0])

    def test_composed_reply_rejects_card_description_with_url(self):
        result = _fc_result(tool_calls=[{
            "name": "compose_public_reply",
            "arguments": {
                "messages": ["先看這家。"],
                "presentation_class": "grounded_recommendation",
                "card_mode": "select",
                "card_intent": "curated",
                "selected_candidate_refs": ["place_candidate_0123456789abcdef"],
                "recommended_candidate_refs": ["place_candidate_0123456789abcdef"],
                "discussed_candidate_refs": ["place_candidate_0123456789abcdef"],
                "blocks": [{
                    "message_index": 0,
                    "card_description": "請看 https://example.com 的營業資訊。",
                    "candidate_refs": ["place_candidate_0123456789abcdef"],
                }],
            },
        }])
        self.assertIsNone(_parse_composed_reply(result, [{
            "candidate_ref": "place_candidate_0123456789abcdef",
            "name": "A", "category": "cafe",
        }]))

    def test_composed_reply_rejects_recommendation_hidden_from_cards(self):
        result = _fc_result(tool_calls=[{
            "name": "compose_public_reply",
            "arguments": {
                "messages": ["推薦 A。"],
                "presentation_class": "grounded_recommendation",
                "card_mode": "select",
                "card_intent": "curated",
                "selected_candidate_refs": ["place_candidate_0123456789abcdef"],
                "recommended_candidate_refs": ["place_candidate_fedcba9876543210"],
                "discussed_candidate_refs": ["place_candidate_0123456789abcdef", "place_candidate_fedcba9876543210"],
            },
        }])
        self.assertIsNone(_parse_composed_reply(result, [
            {"candidate_ref": "place_candidate_0123456789abcdef", "name": "A"},
            {"candidate_ref": "place_candidate_fedcba9876543210", "name": "B"},
        ]))

    def test_no_candidates_no_tool_exposed(self):
        """Without candidates the tool must not be exposed; no tool call is expected."""
        slc = self._slice([])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content="沒有候選"),
        ) as mock_call:
            reply, card_decision, _metrics = synthesize(slc, candidate_cards=None)
        self.assertIsNone(card_decision)
        called_tools = mock_call.call_args[0][1] if mock_call.call_args else []
        self.assertEqual(called_tools, [])

    def test_provider_failure_uses_observation_fallback_not_canned_line(self):
        """When the LLM fails, the fallback must answer from observations,
        never the generic '我在。你可以跟我聊聊…' canned line."""
        slc = self._slice([
            {"task_id": "t1", "status": "ok", "tool": "places.search_nearby",
             "result": {"places": [{"name": "鹽埕炸雞專賣店", "distance_m": 300}]}},
        ])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            side_effect=Exception("provider down"),
        ):
            reply, card_decision, _metrics = synthesize(slc, candidate_cards=self._candidate_cards())
        self.assertIn("炸雞", reply)
        self.assertIn("### 附近地點", reply)
        self.assertIn("**鹽埕炸雞專賣店**", reply)
        self.assertNotIn("我在。你可以跟我聊聊", reply)
        self.assertIsNone(card_decision)

    def test_answered_web_provider_failure_keeps_direct_findings_in_markdown(self):
        slc = self._slice([{
            "task_id": "web1", "status": "ok", "tool": None,
            "result": {
                "schema_version": "web_research.v1",
                "research_question": "全聯最近有優惠嗎",
                "answer_target": "全聯近期公開優惠",
                "status": "answered", "execution_status": "completed",
                "coverage": "direct_sufficient",
                "findings": [{
                    "claim": "全聯公開活動頁列出指定商品集點活動",
                    "relation": "direct",
                    "source_urls": ["https://example.com/campaign"],
                }],
                "sources": [{
                    "url": "https://example.com/campaign", "title": "活動頁",
                    "source_type": "official",
                }],
                "limitations": [], "stop_reason": "evidence_sufficient",
            },
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            side_effect=Exception("provider down"),
        ):
            reply, _card_decision, metrics = synthesize(slc)
        self.assertIn("### 查詢結果", reply)
        self.assertIn("全聯公開活動頁", reply)
        self.assertNotIn("不足以整理成可靠答案", reply)
        self.assertEqual(metrics.presentation_class, "grounded_recommendation")

    def test_answered_web_only_result_reaches_synthesizer(self):
        slc = self._slice([{
            "task_id": "web1", "status": "ok", "tool": None,
            "result": self._web_result(),
        }])
        slc.payload["recent_messages"] = [{"role": "user", "content": "unrelated private context"}]
        slc.payload["recent_context"] = "unrelated private memory"
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content="A natural answer from the verified source."),
        ) as provider:
            reply, card_decision, metrics = synthesize(slc)
        provider.assert_called_once()
        self.assertIsNone(card_decision)
        self.assertEqual(metrics.reply_source, "llm")
        self.assertIn("A natural answer", reply)
        self.assertNotIn("查詢結果", reply)
        self.assertNotIn("unrelated private", metrics.prompt_raw)

    def test_partial_web_only_with_direct_finding_reaches_synthesizer_and_keeps_limitations(self):
        limitation = "The source does not confirm availability after 20:00."
        slc = self._slice([{
            "task_id": "web1", "status": "ok", "tool": None,
            "result": self._web_result(
                status="partial",
                findings=[{
                    "claim": "The verified source confirms the listed start time.",
                    "relation": "direct",
                    "source_urls": ["https://example.com/verified"],
                }],
                limitations=[limitation],
            ),
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content=f"The start time is confirmed. Limitation: {limitation}"),
        ) as provider:
            reply, _card_decision, metrics = synthesize(slc)
        provider.assert_called_once()
        self.assertEqual(metrics.reply_source, "llm")
        self.assertIn(limitation, reply)

    def test_web_only_model_cannot_invent_source_url_or_ref(self):
        result = self._web_result()
        result["primary_activity"] = {
            "source_refs": ["web_source_01"],
            "source_urls": [result["sources"][0]["url"]],
        }
        slc = self._slice([{
            "task_id": "web1", "status": "ok", "tool": None, "result": result,
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(
                content="The answer is supported by https://evil.example/not-observed and web_source_99.",
            ),
        ) as provider:
            reply, _card_decision, metrics = synthesize(slc)
        provider.assert_called_once()
        self.assertEqual(metrics.reply_source, "observation_fallback")
        self.assertEqual(metrics.fallback_reason, "unsupported_claim")
        self.assertNotIn("evil.example", reply)
        self.assertNotIn("web_source_99", reply)

    def test_web_only_observed_source_url_remains_grounded(self):
        result = self._web_result()
        observed_url = result["sources"][0]["url"]
        slc = self._slice([{
            "task_id": "web1", "status": "ok", "tool": None, "result": result,
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content=f"The source is {observed_url}."),
        ):
            reply, _card_decision, metrics = synthesize(slc)
        self.assertEqual(metrics.reply_source, "llm")
        self.assertIn(observed_url, reply)

    def test_plain_places_use_deterministic_markdown_and_requested_limit(self):
        candidates = [
            {
                "candidate_ref": f"place_candidate_{index:016x}",
                "name": name,
                "category": "cafe",
                "distance_m": distance,
                "distance_label": f"約 {distance} 公尺",
            }
            for index, (name, distance) in enumerate(
                [("遠一點咖啡", 900), ("最近咖啡", 120), ("中距咖啡", 500)]
            )
        ]
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {
                "anchor_label": "鹽埕區",
                "requested_limit": 2,
                "ordering": "distance",
                "places": [{"name": item["name"]} for item in candidates],
            },
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
        ) as provider:
            reply, card_decision, metrics = synthesize(slc, candidate_cards=candidates)
        provider.assert_not_called()
        self.assertIn("### 附近地點", reply)
        self.assertIn("1. **最近咖啡**", reply)
        self.assertIn("2. **中距咖啡**", reply)
        self.assertNotIn("遠一點咖啡", reply)
        self.assertEqual(card_decision["indices"], [1, 2])
        self.assertEqual(metrics.fallback_reason, "places_deterministic_presentation")
        card_blocks = [block for block in metrics.presentation_blocks if block["candidate_refs"]]
        self.assertEqual([block["candidate_refs"] for block in card_blocks], [
            ["place_candidate_0000000000000001"],
            ["place_candidate_0000000000000002"],
        ])
        self.assertTrue(all(block["markdown"] for block in card_blocks))
        self.assertTrue(all("card_description" not in block for block in card_blocks))

    def test_plain_places_empty_result_is_deterministic_without_llm(self):
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {
                "anchor_label": "鹽埕區",
                "requested_limit": 3,
                "ordering": "distance",
                "places": [],
            },
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
        ) as provider:
            reply, card_decision, metrics = synthesize(slc, candidate_cards=[])
        provider.assert_not_called()
        self.assertIn("### 沒找到符合條件的地點", reply)
        self.assertEqual(card_decision["mode"], "none")
        self.assertEqual(metrics.fallback_reason, "places_deterministic_presentation")

    def test_unavailable_place_web_lookup_is_honest_and_skips_second_llm(self):
        candidates = [{
            "candidate_ref": f"place_candidate_{index:016x}",
            "name": f"咖啡店 {index}",
            "category": "cafe",
            "distance_label": f"約 {index + 1}00 公尺",
        } for index in range(4)]
        slc = self._slice([{
            "task_id": "web1", "status": "ok", "tool": None,
            "result": {
                "schema_version": "web_research.v1",
                "research_question": "找今晚十點後還開的咖啡廳",
                "answer_target": "確認候選今晚十點後是否營業",
                "status": "insufficient_evidence",
                "execution_status": "unavailable",
                "coverage": "none",
                "findings": [], "sources": [],
                "limitations": ["目前沒有找到能直接支持使用者原始問題的公開證據。"],
                "stop_reason": "model_failure",
            },
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
        ) as provider:
            reply, card_decision, metrics = synthesize(slc, candidate_cards=candidates)
        provider.assert_not_called()
        self.assertIn("網路查證沒有完成", reply)
        self.assertIn("不是已確認推薦", reply)
        self.assertNotIn("公開證據", reply)
        self.assertNotIn("。。", reply)
        self.assertEqual(card_decision["indices"], [0, 1, 2])
        self.assertEqual(metrics.fallback_reason, "web_research_insufficient")

    def test_insufficient_place_web_lookup_labels_cards_as_unconfirmed(self):
        candidates = [{
            "candidate_ref": f"place_candidate_{index:016x}",
            "name": f"咖啡店 {index}", "category": "cafe", "distance_label": "",
        } for index in range(3)]
        slc = self._slice([{
            "task_id": "web1", "status": "ok", "tool": None,
            "result": {
                "schema_version": "web_research.v1",
                "research_question": "找今晚十點後還開的咖啡廳",
                "answer_target": "確認候選今晚十點後是否營業",
                "status": "insufficient_evidence",
                "execution_status": "completed",
                "coverage": "none",
                "findings": [], "sources": [],
                "limitations": ["沒有可確認的營業時間。"],
                "stop_reason": "no_direct_evidence",
            },
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
        ) as provider:
            reply, card_decision, _metrics = synthesize(slc, candidate_cards=candidates)
        provider.assert_not_called()
        self.assertIn("公開資訊不足", reply)
        self.assertIn("不是已確認推薦", reply)
        self.assertEqual(card_decision["indices"], [0, 1, 2])

    def test_provider_failure_selects_only_web_verified_place_refs(self):
        first_ref = "place_candidate_0123456789abcdef"
        second_ref = "place_candidate_fedcba9876543210"
        candidates = [
            {"candidate_ref": first_ref, "name": "已確認店", "category": "cafe", "distance_label": "約 100 公尺"},
            {"candidate_ref": second_ref, "name": "未確認店", "category": "cafe", "distance_label": "約 200 公尺"},
        ]
        slc = self._slice([{
            "task_id": "web1", "status": "ok", "tool": None,
            "result": {
                "schema_version": "web_research.v1",
                "research_question": "找今晚十點後還開的咖啡廳",
                "answer_target": "確認候選今晚十點後是否營業",
                "status": "partial", "execution_status": "completed",
                "coverage": "direct_partial",
                "findings": [{
                    "claim": "已確認店公開頁面列出週日晚間營業至 23:00",
                    "relation": "direct", "subject_ref": first_ref,
                    "source_urls": ["https://example.com/hours"],
                }],
                "sources": [{"url": "https://example.com/hours", "title": "Hours"}],
                "limitations": ["其他候選仍無法確認。"],
                "stop_reason": "partial_coverage",
            },
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            side_effect=Exception("provider down"),
        ):
            reply, card_decision, _metrics = synthesize(slc, candidate_cards=candidates)
        self.assertIn("已確認店", reply)
        self.assertIn("其他候選仍無法確認", reply)
        self.assertEqual(card_decision["indices"], [0])

    def test_internal_meta_reply_rejected_uses_observation_fallback(self):
        """A reply that trips the internal-meta filter must fall back to
        observation-based text, not a canned line."""
        slc = self._slice([
            {"task_id": "t1", "status": "ok", "tool": "calendar.find_my_event",
             "result": {"found": True, "event": {"title": "看電影", "date": "2026-08-25", "start_time": "17:00"}}},
        ])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content="我沒有工具可以回答你"),  # trips _INTERNAL_META_REPLY_RE
        ):
            reply, card_decision, _metrics = synthesize(slc)
        self.assertIn("電影", reply)
        self.assertNotIn("我在。你可以跟我聊聊", reply)

    def test_observation_fallback_no_results_returns_honest_message(self):
        slc = self._slice([])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            side_effect=Exception("provider down"),
        ):
            reply, _card_decision, metrics = synthesize(slc)
        self.assertEqual(reply, "我剛剛沒接好，但你不用整段重講。把最想先說的那一點丟給我，我從那裡接。")
        self.assertEqual(metrics.reply_source, "general_fallback")
        self.assertEqual(metrics.fallback_reason, "provider_error")
        self.assertEqual(metrics.error_code, "synthesizer_provider_error")

    def test_capability_query_is_not_classified_by_synthesizer_regex(self):
        slc = self._slice([])
        slc.payload["message"] = "你可以幹嘛"
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content="一般回覆"),
        ) as mock_call:
            reply, card_decision, metrics = synthesize(slc)
        self.assertEqual(reply, "一般回覆")
        self.assertIsNone(card_decision)
        self.assertEqual(metrics.reply_source, "llm")
        mock_call.assert_called_once()

    def test_soft_match_opportunity_fallback_does_not_claim_search_started(self):
        slc = self._slice([{
            "task_id": "guidance", "status": "ok", "tool": None,
            "result": {"match_opportunity_offer": {"expires_in_seconds": 900}},
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            side_effect=Exception("provider down"),
        ):
            reply, _card_decision, _metrics = synthesize(slc)
        self.assertIn("想試試看", reply)
        self.assertNotIn("已開始", reply)

    def test_surface_questions_are_typed_product_info_planner_inputs(self):
        slc = self._slice([])
        slc.payload["message"] = "那你可\u200b以幹嘛？"
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content="一般回覆"),
        ) as mock_call:
            reply, _card_decision, metrics = synthesize(slc)
        self.assertEqual(reply, "一般回覆")
        self.assertEqual(metrics.reply_source, "llm")
        mock_call.assert_called_once()

    def test_product_info_facts_are_composed_for_the_current_question(self):
        slc = self._slice([{
            "task_id": "product_info", "status": "ok", "tool": None,
            "result": {"product_info": {
                "manifest_version": "v4",
                "topics": ["same_identity", "surface_scope"],
                "facts": {
                    "identity": {"same_ayue": True},
                    "surface_scope": {
                        "public": "owner_self_and_new_relationships",
                        "private": "current_accepted_relationship",
                    },
                },
            }},
        }])
        slc.payload["message"] = "另一個阿月是幹啥的？"
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content="也是我，只是在雙人聊天室裡會專心處理你和那個人的互動。"),
        ) as provider:
            reply, _card_decision, metrics = synthesize(slc)

        self.assertIn("也是我", reply)
        self.assertEqual(metrics.presentation_class, "product_info")
        prompt = provider.call_args.args[0]
        self.assertIn("same_identity", prompt)
        self.assertIn("另一個阿月是幹啥的", prompt)
        self.assertIn("不要改回通用身份介紹", provider.call_args.kwargs["system_prompt"])

    def test_general_provider_failure_does_not_keyword_route_to_matching_copy(self):
        slc = self._slice([])
        slc.payload["message"] = "我只是說朋友最近在聊配對，沒有要問 App"
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            side_effect=Exception("provider down"),
        ):
            reply, _card_decision, metrics = synthesize(slc)

        self.assertEqual(metrics.reply_source, "general_fallback")
        self.assertNotIn("我不會隨機配對", reply)

    def test_no_write_proposed_returns_graceful_not_found_reply(self):
        # 寫入任務因候選查詢 not_found 而沒有提出寫入時，fallback 必須
        # 優雅告知找不到，而不是假裝已處理或回罐頭句。
        slc = self._slice([
            {"task_id": "t2", "status": "ok", "tool": "calendar.find_my_event",
             "result": {"no_write_proposed": True, "not_found_queries": ["出國"]}},
        ])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            side_effect=Exception("provider down"),
        ):
            reply, _card_decision, _metrics = synthesize(slc)
        self.assertIn("出國", reply)
        self.assertIn("找不到", reply)
        self.assertNotIn("我在。你可以跟我聊聊", reply)

    def test_typed_calendar_clarification_is_rephrased_by_llm_or_safe_fallback(self):
        slc = self._slice([
            {"task_id": "calendar-1", "status": "ok", "tool": "calendar.submit_commands",
             "result": {"calendar_command_result": {
                 "status": "needs_clarification",
                 "clarification": {"code": "missing_fields", "message": "請補上：結束時間。"},
             }}},
        ])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
        ) as mock_call:
            reply, _card_decision, _metrics = synthesize(slc)
        self.assertTrue(reply)
        mock_call.assert_called_once()

    def test_confirmed_domain_reply_is_returned_without_llm_rewrite(self):
        slc = self._slice([
            {"task_id": "confirm", "status": "ok", "tool": None, "result": [
                {"ok": True, "tool_name": "profile.start_assessment",
                 "data": {"reply": "好，我們從第一題開始。"}},
            ]},
        ])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
        ) as mock_call:
            reply, _card_decision, _metrics = synthesize(slc)
        self.assertEqual(reply, "好，我們從第一題開始。")
        mock_call.assert_not_called()

    def test_place_internals_stripped_from_prompt(self):
        """address_summary/map_url/provider/place_id/photo_url must not reach the model."""
        slc = self._slice([
            {"task_id": "t1", "status": "ok", "tool": "places.search_nearby",
             "result": {"places": [
                 {"name": "青埔香雞排", "category": "restaurant", "distance_m": 927,
                  "address_summary": "337台灣桃園市大園區…", "map_url": "https://maps.google.com/?cid=1",
                  "provider": "google", "place_id": "ChIJxxx", "photo_url": "https://places.googleapis.com/v1/x"},
             ]}},
            {"task_id": "t2", "status": "ok", "tool": "places.resolve_place",
             "result": {"found": True, "place": {
                 "name": "老宋牛肉麵", "category": "restaurant", "distance_m": 267,
                 "address_summary": "地址", "map_url": "https://maps.google.com/?cid=2",
                 "provider": "google", "place_id": "ChIJyyy", "photo_url": ""}}},
            {"task_id": "t3", "status": "ok", "tool": "calendar.list_my_events",
             "result": {"events": [{"title": "看電視", "date": "2026-08-11", "start_time": "20:00"}]}},
        ])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content="找到幾家店，卡片在下面"),
        ) as mock_call:
            synthesize(slc)
        prompt = mock_call.call_args[0][0]
        self.assertNotIn("address_summary", prompt)
        self.assertNotIn("map_url", prompt)
        self.assertNotIn("place_id", prompt)
        self.assertNotIn("photo_url", prompt)
        self.assertNotIn("provider", prompt)
        self.assertNotIn("https://maps.google.com", prompt)
        self.assertNotIn("ChIJ", prompt)
        self.assertIn("青埔香雞排", prompt)
        self.assertIn("老宋牛肉麵", prompt)
        self.assertIn("看電視", prompt)


if __name__ == "__main__":
    unittest.main()
