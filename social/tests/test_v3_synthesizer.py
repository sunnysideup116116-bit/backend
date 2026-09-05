# tests/test_v3_synthesizer.py
import unittest
from unittest.mock import patch

import config
from services.ayue_agent.v3.contracts import AgentContextSlice
from services.ayue_agent.v3.synthesizer import (
    _build_prompt,
    _compose_public_reply_tool_schema,
    _parse_composed_reply,
    _synthesizer_system_prompt,
    _web_research_fallback,
    synthesize,
)
from services.ai_service import ToolCallResult


def _fc_result(content="", tool_calls=None):
    return ToolCallResult(content=content, tool_calls=tool_calls or [])


class V3SynthesizerTests(unittest.TestCase):
    def setUp(self):
        # Most of this file exercises the retained server-owned card/ref
        # contract. Demo rendering behavior is covered explicitly with the
        # switch disabled in focused tests below.
        self._old_public_place_cards_enabled = getattr(
            config, "AYUE_PUBLIC_PLACE_CARDS_ENABLED", False,
        )
        config.AYUE_PUBLIC_PLACE_CARDS_ENABLED = True

    def tearDown(self):
        config.AYUE_PUBLIC_PLACE_CARDS_ENABLED = self._old_public_place_cards_enabled

    def test_active_composer_schemas_expose_only_runtime_fields(self):
        ordinary_schema = _compose_public_reply_tool_schema()["function"]["parameters"]
        self.assertIn("messages", ordinary_schema["properties"])
        self.assertIn("card_intent", ordinary_schema["properties"])
        self.assertNotIn("itinerary", ordinary_schema["properties"])
        self.assertNotIn("blocks", ordinary_schema["properties"])
        self.assertNotIn("card_mode", ordinary_schema["properties"])

    def test_legacy_itinerary_presentation_class_is_normalized(self):
        parsed = _parse_composed_reply(
            _fc_result(tool_calls=[{
                "name": "compose_public_reply",
                "arguments": {
                    "messages": ["先去活動，再到 Cafe A 休息。"],
                    "presentation_class": "itinerary",
                    "card_intent": "curated",
                    "selected_candidate_refs": ["place_1"],
                },
            }]),
            [{"candidate_ref": "place_1", "name": "Cafe A"}],
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[2], "grounded_recommendation")

    def test_unknown_presentation_class_uses_safe_default(self):
        parsed = _parse_composed_reply(
            _fc_result(tool_calls=[{
                "name": "compose_public_reply",
                "arguments": {
                    "messages": ["Cafe A 可以作為活動後的休息點。"],
                    "presentation_class": "provider_specific_layout",
                    "card_intent": "curated",
                    "selected_candidate_refs": ["place_1"],
                },
            }]),
            [{"candidate_ref": "place_1", "name": "Cafe A"}],
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[2], "conversation")

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

    def test_itinerary_mode_uses_natural_compose_and_selected_refs(self):
        slc = self._slice([
            {
                "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
                "result": {"places": [{"name": "午餐店"}, {"name": "咖啡廳"}]},
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
                    "messages": ["週六可以先安排午餐，再看時間決定要不要到咖啡廳休息。"],
                    "presentation_class": "grounded_recommendation",
                    "card_intent": "curated",
                    "selected_candidate_refs": ["place_1"],
                    "recommended_candidate_refs": ["place_1"],
                    "discussed_candidate_refs": ["place_1", "place_2"],
                },
            }]),
        ):
            reply, card_decision, metrics = synthesize(slc, candidate_cards=cards)
        self.assertIn("週六可以先安排午餐", reply)
        self.assertIn("到咖啡廳休息", reply)
        self.assertEqual(card_decision["selected_candidate_refs"], ["place_1"])
        self.assertEqual(
            [block["candidate_refs"] for block in metrics.presentation_blocks if block["candidate_refs"]],
            [["place_1"]],
        )
        self.assertTrue(all(not block["markdown"] for block in metrics.presentation_blocks if block["candidate_refs"]))
        self.assertEqual(metrics.presentation_class, "grounded_recommendation")

    def test_itinerary_can_use_plain_natural_text_without_rigid_schema(self):
        slc = self._slice([{
            "task_id": "w1", "status": "ok", "tool": "web.research",
            "result": {"primary_activity": {"title": "鹽埕夜間市集", "venue": "駁二"}},
        }], presentation_mode="itinerary")
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content="週六可以先去逛市集，之後再看現場狀況決定要不要找地方休息。"),
        ) as provider:
            reply, card_decision, metrics = synthesize(slc)
        self.assertIn("週六可以先去逛市集", reply)
        self.assertIn("看現場狀況決定", reply)
        self.assertIsNone(card_decision)
        self.assertEqual(metrics.reply_source, "llm")
        self.assertNotIn("10:00", reply)
        self.assertNotIn("12:00", reply)
        self.assertNotIn("15:00", reply)
        system_prompt = provider.call_args.kwargs["system_prompt"]
        self.assertIn("ordinary compose contract", system_prompt)
        self.assertIn("不需要固定段落、標題、時間軸或專用渲染資料", system_prompt)
        self.assertNotIn("block-based exception", system_prompt)

    def test_multi_candidate_reply_can_use_adaptive_markdown_without_cards(self):
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"places": [{"name": "A 店"}, {"name": "B 店"}]},
        }])
        cards = [
            {"candidate_ref": "place_a", "name": "A 店", "category": "restaurant"},
            {"candidate_ref": "place_b", "name": "B 店", "category": "cafe"},
        ]
        composed = _fc_result(tool_calls=[{
            "name": "compose_public_reply",
            "arguments": {
                "messages": [
                    "我會先這樣比較：\n\n- **A 店**：距離近，適合直接吃飯。\n- **B 店**：比較適合吃完後坐著聊。",
                ],
                "presentation_class": "grounded_recommendation",
            },
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.public_place_cards_enabled",
            return_value=False,
        ), patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=composed,
        ) as provider:
            reply, card_decision, metrics = synthesize(slc, candidate_cards=cards)
        self.assertIn("- **A 店**", reply)
        self.assertIn("- **B 店**", reply)
        self.assertIsNone(card_decision)
        self.assertEqual(metrics.presentation_blocks, [])
        self.assertIn("多個候選、比較、步驟或清楚分組", provider.call_args.kwargs["system_prompt"])

    def test_disabled_cards_do_not_prompt_browse_or_curated_presentation(self):
        prompt = _synthesizer_system_prompt(
            "grounded_result", True, "default", place_cards_enabled=False,
        )
        self.assertIn("card_intent 固定使用 none", prompt)
        self.assertNotIn("card_intent=browse", prompt)
        self.assertNotIn("card_intent=curated", prompt)
        self.assertIn("candidate_ref", prompt)
        disabled_schema = _compose_public_reply_tool_schema(place_cards_enabled=False)
        self.assertEqual(
            disabled_schema["function"]["parameters"]["properties"]["card_intent"]["enum"],
            ["none"],
        )

    def test_disabled_place_fallback_also_has_no_cards(self):
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"places": [{"name": "候選店"}]},
        }])
        cards = [{"candidate_ref": "place_a", "name": "候選店", "category": "restaurant"}]
        with patch(
            "services.ayue_agent.v3.synthesizer.public_place_cards_enabled",
            return_value=False,
        ), patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            side_effect=RuntimeError("provider unavailable"),
        ):
            reply, card_decision, metrics = synthesize(slc, candidate_cards=cards)
        self.assertIn("候選店", reply)
        self.assertIsNone(card_decision)
        self.assertEqual(metrics.presentation_blocks, [])

    def test_disabled_cards_keep_compose_contract_without_raw_token_streaming(self):
        """Cards-off changes rendering only, not grounded composition safety."""
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"places": [{"name": "候選店"}]},
        }])
        cards = [{"candidate_ref": "place_a", "name": "候選店", "category": "restaurant"}]
        tokens: list[str] = []

        def _collect(fragment: str) -> None:
            tokens.append(fragment)

        with patch(
            "services.ayue_agent.v3.synthesizer.public_place_cards_enabled",
            return_value=False,
        ), patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content="候選店離你很近，可以直接列入晚餐考慮。"),
        ) as provider:
            reply, card_decision, metrics = synthesize(
                slc, candidate_cards=cards, on_token=_collect,
            )
        self.assertIn("候選店", reply)
        self.assertIsNone(card_decision)
        self.assertEqual(metrics.presentation_blocks, [])
        tools = provider.call_args.args[1]
        self.assertEqual(tools[0]["function"]["name"], "compose_public_reply")
        self.assertIsNone(provider.call_args.kwargs["on_token"])
        self.assertEqual(tokens, [])

    def test_cards_off_accepts_grounded_compose_with_discussed_refs(self):
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"places": [{"name": "A 店"}, {"name": "B 店"}]},
        }])
        cards = [
            {"candidate_ref": "place_a", "name": "A 店", "category": "restaurant"},
            {"candidate_ref": "place_b", "name": "B 店", "category": "cafe"},
        ]
        composed = _fc_result(tool_calls=[{
            "name": "compose_public_reply",
            "arguments": {
                "messages": ["A 店和 B 店都在附近，A 店的距離更近。"],
                "presentation_class": "grounded_recommendation",
                "card_intent": "none",
                "selected_candidate_refs": [],
                "recommended_candidate_refs": [],
                "discussed_candidate_refs": ["place_a", "place_b"],
                "presented_candidates": [
                    {"candidate_ref": "place_a", "presented_ordinal": 1},
                    {"candidate_ref": "place_b", "presented_ordinal": 2},
                ],
            },
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.public_place_cards_enabled",
            return_value=False,
        ), patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=composed,
        ):
            reply, card_decision, metrics = synthesize(slc, candidate_cards=cards)
        self.assertEqual(reply, "A 店和 B 店都在附近,A 店的距離更近。")
        self.assertEqual(metrics.reply_source, "llm")
        self.assertIsNone(metrics.fallback_reason)
        self.assertIsNone(card_decision)
        self.assertEqual(metrics.presentation_blocks, [])
        self.assertEqual(metrics.presented_candidate_refs, ["place_a", "place_b"])
        self.assertNotIn("我還沒能完成完整比較", reply)

    def test_cards_off_still_rejects_unknown_discussed_candidate_ref(self):
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"requested_limit": 1, "places": [{"name": "A 店"}]},
        }])
        cards = [{"candidate_ref": "place_a", "name": "A 店", "category": "restaurant"}]
        composed = _fc_result(tool_calls=[{
            "name": "compose_public_reply",
            "arguments": {
                "messages": ["我找到了候選。"],
                "presentation_class": "grounded_recommendation",
                "card_intent": "none",
                "discussed_candidate_refs": ["place_invented"],
            },
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.public_place_cards_enabled",
            return_value=False,
        ), patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=composed,
        ):
            reply, card_decision, metrics = synthesize(slc, candidate_cards=cards)
        self.assertEqual(metrics.reply_source, "observation_fallback")
        self.assertEqual(metrics.fallback_reason, "empty_content")
        self.assertIsNone(card_decision)
        self.assertNotIn("place_invented", reply)
        self.assertNotIn("我還沒能完成完整比較", reply)

    def test_cards_off_derives_presented_order_from_exact_unique_labels(self):
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"places": [{"name": "A 店"}, {"name": "B 店"}]},
        }])
        cards = [
            {"candidate_ref": "place_a", "name": "A 店", "category": "restaurant"},
            {"candidate_ref": "place_b", "name": "B 店", "category": "cafe"},
        ]
        with patch(
            "services.ayue_agent.v3.synthesizer.public_place_cards_enabled",
            return_value=False,
        ), patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "compose_public_reply",
                "arguments": {
                    "messages": ["1. B 店\n2. A 店"],
                    "presentation_class": "grounded_recommendation",
                    "card_intent": "none",
                    "presented_candidates": [
                        {"candidate_ref": "place_b", "presented_ordinal": 1},
                        {"candidate_ref": "place_a", "presented_ordinal": 2},
                    ],
                },
            }]),
        ):
            _reply, _decision, metrics = synthesize(slc, candidate_cards=cards)
        self.assertEqual(metrics.presented_candidate_refs, ["place_b", "place_a"])

    def test_cards_off_plain_content_derives_presented_refs(self):
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"places": [{"name": "A 店"}, {"name": "B 店"}, {"name": "C 店"}]},
        }])
        cards = [
            {"candidate_ref": "place_a", "name": "A 店", "category": "restaurant"},
            {"candidate_ref": "place_b", "name": "B 店", "category": "cafe"},
            {"candidate_ref": "place_c", "name": "C 店", "category": "bar"},
        ]
        with patch(
            "services.ayue_agent.v3.synthesizer.public_place_cards_enabled",
            return_value=False,
        ), patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content="附近有 B 店、A 店和 C 店，都可以先參考。"),
        ):
            reply, card_decision, metrics = synthesize(slc, candidate_cards=cards)
        self.assertEqual(reply, "附近有 B 店、A 店和 C 店,都可以先參考。")
        self.assertIsNone(card_decision)
        self.assertEqual(
            metrics.presented_candidate_refs,
            [],
        )

    def test_cards_off_plain_content_does_not_bind_duplicate_names(self):
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"places": [{"name": "同名店"}, {"name": "同名店"}]},
        }])
        cards = [
            {"candidate_ref": "place_a", "name": "同名店", "category": "restaurant"},
            {"candidate_ref": "place_b", "name": "同名店", "category": "cafe"},
        ]
        with patch(
            "services.ayue_agent.v3.synthesizer.public_place_cards_enabled",
            return_value=False,
        ), patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content="我找到同名店，請再指定一間。"),
        ):
            _reply, _card_decision, metrics = synthesize(slc, candidate_cards=cards)
        self.assertEqual(metrics.presented_candidate_refs, [])

    def test_cards_off_presented_binding_requires_supplied_ref_and_contiguous_ordinal(self):
        cards = [
            {"candidate_ref": "place_a", "name": "A 店", "category": "cafe"},
            {"candidate_ref": "place_b", "name": "B 店", "category": "cafe"},
        ]
        for bindings in (
            [{"candidate_ref": "place_unknown", "presented_ordinal": 1}],
            [{"candidate_ref": "place_a", "presented_ordinal": 2}],
        ):
            parsed = _parse_composed_reply(_fc_result(tool_calls=[{
                "name": "compose_public_reply",
                "arguments": {
                    "messages": ["1. A 店"],
                    "presentation_class": "grounded_recommendation",
                    "card_intent": "none",
                    "presented_candidates": bindings,
                },
            }]), cards)
            self.assertIsNone(parsed)

    def test_persistent_place_reference_cannot_leak_into_public_message(self):
        parsed = _parse_composed_reply(
            _fc_result(tool_calls=[{
                "name": "compose_public_reply",
                "arguments": {
                    "messages": ["就選 place_ref_0123456789abcdef01234567。"],
                    "presentation_class": "grounded_recommendation",
                    "card_intent": "none",
                    "discussed_candidate_refs": ["place_a"],
                },
            }]),
            [{"candidate_ref": "place_a", "name": "A 店"}],
        )
        self.assertIsNone(parsed)

    def test_itinerary_card_intent_none_does_not_render_candidates(self):
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"places": [{"name": "晚餐店"}]},
        }], presentation_mode="itinerary")
        cards = [{"candidate_ref": "place_1", "name": "晚餐店", "category": "restaurant"}]
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "compose_public_reply",
                "arguments": {
                    "messages": ["這次先給你行程方向，地點卡片先不放。"],
                    "presentation_class": "grounded_recommendation",
                    "card_intent": "none",
                },
            }]),
        ):
            reply, card_decision, _metrics = synthesize(slc, candidate_cards=cards)
        self.assertIn("這次先給你行程方向", reply)
        self.assertIn("地點卡片先不放", reply)
        self.assertIsNone(card_decision)

    def test_itinerary_provider_failure_uses_observation_fallback_without_invented_times(self):
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"places": [{"name": "晚餐店"}]},
        }], presentation_mode="itinerary")
        cards = [{"candidate_ref": "place_1", "name": "晚餐店", "category": "restaurant"}]
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            side_effect=RuntimeError("provider unavailable"),
        ):
            reply, card_decision, metrics = synthesize(slc, candidate_cards=cards)
        self.assertIn("晚餐店", reply)
        self.assertNotIn("10:00", reply)
        self.assertNotIn("12:00", reply)
        self.assertNotIn("15:00", reply)
        self.assertIsNone(card_decision)
        self.assertEqual(metrics.presentation_blocks, [])
        self.assertEqual(metrics.fallback_reason, "provider_error")

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

    def test_calendar_read_cannot_be_synthesized_as_completed_write_or_reminder_offer(self):
        slc = self._slice([{
            "task_id": "t1", "status": "ok", "tool": "calendar.list_my_events",
            "result": {
                "events": [{
                    "title": "牙醫", "date": "2026-08-26",
                    "start_time": "15:00", "end_time": "17:00",
                }],
            },
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(
                content="好，8/26 下午 3 點到 5 點牙醫，我幫你記進行事曆。要設提醒嗎？",
            ),
        ):
            reply, card_decision, metrics = synthesize(slc)

        self.assertIn("牙醫", reply)
        self.assertNotIn("幫你記進行事曆", reply)
        self.assertNotIn("提醒", reply)
        self.assertIsNone(card_decision)
        self.assertEqual(metrics.reply_source, "observation_fallback")
        self.assertEqual(metrics.fallback_reason, "unsupported_claim")

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
            reply, _decision, metrics = synthesize(slc)
        self.assertEqual(reply, "這次行程指令格式無法驗證，請重新描述需求。")
        self.assertEqual(metrics.reply_source, "verified_observation")
        call.assert_not_called()

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

    def test_mixed_server_owned_reply_does_not_hide_other_observations(self):
        candidate_ref = "place_candidate_mixed_0001"
        slc = self._slice([
            {
                "task_id": "calendar1", "status": "ok",
                "tool": "calendar.verify_recent_mutation",
                "result": {"calendar_mutation_verification": {
                    "status": "verified_success", "action": "cancel",
                    "label": "看牙醫", "outcome": "success",
                }},
            },
            {
                "task_id": "places1", "status": "ok",
                "tool": "places.search_nearby",
                "result": {"places": [{"name": "Cafe A"}]},
            },
        ])
        cards = [{
            "candidate_ref": candidate_ref, "name": "Cafe A",
            "category": "cafe", "distance_label": "300 公尺",
        }]
        with patch(
            "services.ayue_agent.v3.synthesizer.public_place_cards_enabled",
            return_value=False,
        ), patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content="附近可以參考 Cafe A。"),
        ) as provider:
            reply, card_decision, metrics = synthesize(slc, candidate_cards=cards)

        provider.assert_called_once()
        prompt = provider.call_args.args[0]
        self.assertIn("Cafe A", prompt)
        self.assertNotIn("看牙醫", prompt)
        self.assertIn("Cafe A", reply)
        self.assertIn("已取消", reply)
        self.assertIsNone(card_decision)
        self.assertEqual(metrics.presentation_blocks, [])

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

    def test_composed_reply_binds_recommendation_refs_to_selected_cards(self):
        result = _fc_result(tool_calls=[{
            "name": "compose_public_reply",
            "arguments": {
                "messages": ["先選 A，因為目前公開資訊直接支持它。"],
                "presentation_class": "grounded_recommendation",
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

    def test_composed_reply_rejects_recommendation_hidden_from_cards(self):
        result = _fc_result(tool_calls=[{
            "name": "compose_public_reply",
            "arguments": {
                "messages": ["推薦 A。"],
                "presentation_class": "grounded_recommendation",
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
        """Plain replies use no tool, and forward raw tokens to the provider."""
        slc = self._slice([])
        tokens: list[str] = []
        on_token = tokens.append
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(content="沒有候選"),
        ) as mock_call:
            reply, card_decision, _metrics = synthesize(
                slc, candidate_cards=None, on_token=on_token,
            )
        self.assertIsNone(card_decision)
        called_tools = mock_call.call_args[0][1] if mock_call.call_args else []
        self.assertEqual(called_tools, [])
        self.assertIs(mock_call.call_args.kwargs["on_token"], on_token)

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
        self.assertIn("小酒館", reply)
        self.assertIn("義式料理餐廳", reply)
        self.assertNotIn("###", reply)
        self.assertNotIn("我在。你可以跟我聊聊", reply)
        self.assertIsNone(card_decision)
        self.assertEqual(_metrics.presentation_blocks, [])

    def test_answered_web_provider_failure_keeps_direct_findings_without_template(self):
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
        self.assertIn("全聯公開活動頁", reply)
        self.assertNotIn("### ", reply)
        self.assertNotIn("目前已確認", reply)
        self.assertNotIn("尚未確認", reply)
        self.assertEqual(metrics.presentation_class, "grounded_recommendation")

    def test_web_fallback_is_minimal_and_preserves_typed_limitations(self):
        reply = _web_research_fallback(self._web_result(
            status="partial",
            findings=[{
                "claim": "The verified source lists a 19:00 start time.",
                "relation": "direct",
                "source_urls": ["https://example.com/verified"],
            }],
            limitations=["The source does not confirm the end time."],
        ))
        self.assertIn("19:00", reply)
        self.assertIn("does not confirm the end time", reply)
        self.assertNotIn("###", reply)
        self.assertNotIn("目前已確認", reply)
        self.assertNotIn("尚未確認", reply)

    def test_unavailable_web_fallback_does_not_repeat_the_same_limitation(self):
        reply = _web_research_fallback(self._web_result(
            status="insufficient_evidence",
            execution_status="unavailable",
            findings=[],
            limitations=["目前沒有找到能直接支援使用者原始問題的公開證據。"],
        ))
        self.assertEqual(reply, "這次 Web 查證沒有完成，目前無法確認你問的資訊。")
        self.assertNotIn("目前沒有找到能直接支援", reply)

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
        self.assertEqual(provider.call_args.args[1], [])
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

    def test_web_only_typed_non_answered_statuses_are_llm_first(self):
        cases = [
            ("partial", "completed"),
            ("insufficient_evidence", "completed"),
            ("partial", "degraded"),
            ("insufficient_evidence", "unavailable"),
        ]
        for status, execution_status in cases:
            with self.subTest(status=status, execution_status=execution_status):
                slc = self._slice([{
                    "task_id": "web1", "status": "ok", "tool": None,
                    "result": self._web_result(
                        status=status,
                        execution_status=execution_status,
                        findings=[] if status == "insufficient_evidence" else None,
                        limitations=[f"limitation:{status}:{execution_status}"],
                    ),
                }])
                with patch(
                    "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
                    return_value=_fc_result(
                        content=f"Natural Web answer for {status}/{execution_status}.",
                    ),
                ) as provider:
                    reply, _card_decision, metrics = synthesize(slc)
                provider.assert_called_once()
                self.assertEqual(metrics.reply_source, "llm")
                self.assertTrue(metrics.used_llm)
                self.assertIn("Natural Web answer", reply)

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

    def test_calendar_empty_result_survives_places_provider_fallback(self):
        candidates = [{
            "candidate_ref": "place_a", "name": "Cafe A", "category": "cafe",
        }]
        slc = self._slice([
            {
                "task_id": "calendar1", "status": "ok", "tool": "calendar.list_my_events",
                "result": {"events": [], "range": "2026-08-15"},
            },
            {
                "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
                "result": {"places": [{"name": "Cafe A"}]},
            },
        ])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            side_effect=RuntimeError("provider unavailable"),
        ):
            reply, _card_decision, metrics = synthesize(slc, candidate_cards=candidates)
        self.assertEqual(metrics.reply_source, "observation_fallback")
        self.assertIn("2026-08-15", reply)
        self.assertIn("目前沒有行程", reply)
        self.assertIn("Cafe A", reply)

    def test_plain_places_invalid_provider_output_uses_deterministic_fallback(self):
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
        provider.assert_called_once()
        self.assertNotIn("###", reply)
        self.assertIn("最近咖啡", reply)
        self.assertIn("中距咖啡", reply)
        self.assertNotIn("遠一點咖啡", reply)
        self.assertIsNone(card_decision)
        self.assertEqual(metrics.presentation_blocks, [])
        self.assertEqual(metrics.fallback_reason, "provider_error")

    def test_plain_places_empty_result_uses_deterministic_fallback_after_provider_failure(self):
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
        provider.assert_called_once()
        self.assertIn("目前沒有找到符合條件的地點", reply)
        self.assertIsNone(card_decision)
        self.assertEqual(metrics.presentation_blocks, [])
        self.assertEqual(metrics.fallback_reason, "provider_error")

    def test_places_success_uses_llm_and_separates_candidate_pool_from_cards(self):
        candidates = [
            {"candidate_ref": "place_a", "name": "Cafe A", "category": "cafe", "distance_label": "100 公尺", "distance_m": 100},
            {"candidate_ref": "place_b", "name": "Cafe B", "category": "cafe", "distance_label": "120 公尺", "distance_m": 120},
            {"candidate_ref": "place_c", "name": "Cafe C", "category": "cafe", "distance_label": "300 公尺", "distance_m": 300},
        ]
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {
                "requested_limit": 3,
                "ordering": "distance",
                "places": [{"name": item["name"]} for item in candidates],
            },
        }])
        slc.payload["message"] = "找 3 家咖啡廳，最後挑最近的 2 家"
        composed = _fc_result(tool_calls=[{
            "name": "compose_public_reply",
            "arguments": {
                "messages": ["我會選 Cafe A 和 Cafe B；A 最近，B 只多 20 公尺。"],
                "presentation_class": "grounded_recommendation",
                "card_intent": "explicit_set",
                "selected_candidate_refs": ["place_a", "place_b"],
                "recommended_candidate_refs": ["place_a", "place_b"],
                "discussed_candidate_refs": ["place_a", "place_b", "place_c"],
            },
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=composed,
        ) as provider:
            reply, card_decision, metrics = synthesize(slc, candidate_cards=candidates)

        provider.assert_called_once()
        prompt = provider.call_args.args[0]
        for candidate in candidates:
            self.assertIn(candidate["candidate_ref"], prompt)
        self.assertEqual(metrics.reply_source, "llm")
        self.assertTrue(metrics.used_llm)
        self.assertIn("Cafe A", reply)
        self.assertIn("Cafe B", reply)
        self.assertNotIn("- **Cafe A**", reply)
        self.assertNotIn("- **Cafe B**", reply)
        self.assertEqual(card_decision["selected_candidate_refs"], ["place_a", "place_b"])
        self.assertEqual(card_decision["indices"], [0, 1])
        self.assertEqual(
            [block["candidate_refs"] for block in metrics.presentation_blocks],
            [["place_a"], ["place_b"]],
        )
        self.assertTrue(all(not block["markdown"] for block in metrics.presentation_blocks))

    def test_live_ordinary_compose_shape_derives_select_and_renders_two_cards(self):
        candidates = [
            {"candidate_ref": "place_a", "name": "Cafe A", "category": "cafe"},
            {"candidate_ref": "place_b", "name": "Cafe B", "category": "cafe"},
            {"candidate_ref": "place_c", "name": "Cafe C", "category": "cafe"},
        ]
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"requested_limit": 3, "places": [{"name": item["name"]} for item in candidates]},
        }])
        slc.payload["message"] = "找 3 家咖啡廳，最後挑最近的 2 家"
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "compose_public_reply",
                "arguments": {
                    "messages": [
                        {"role": "user", "content": "找 3 家咖啡廳，最後挑最近的 2 家"},
                        {"role": "assistant", "content": "我會選 Cafe A 和 Cafe B。"},
                    ],
                    "presentation_class": "grounded_recommendation",
                    "card_intent": "explicit_set",
                    "selected_candidate_refs": ["place_a", "place_b"],
                },
            }]),
        ) as provider:
            reply, card_decision, metrics = synthesize(slc, candidate_cards=candidates)

        provider.assert_called_once()
        self.assertEqual(metrics.reply_source, "llm")
        self.assertTrue(metrics.used_llm)
        self.assertIn("Cafe A", reply)
        self.assertIn("Cafe B", reply)
        self.assertEqual(card_decision["mode"], "select")
        self.assertEqual(card_decision["selected_candidate_refs"], ["place_a", "place_b"])
        self.assertEqual(card_decision["indices"], [0, 1])
        self.assertEqual(
            [block["candidate_refs"] for block in metrics.presentation_blocks],
            [["place_a"], ["place_b"]],
        )
        self.assertEqual(len(metrics.presentation_blocks), 2)
        self.assertTrue(all(not block["markdown"] for block in metrics.presentation_blocks))

    def test_ordinary_role_content_drift_never_exposes_non_assistant_transcript(self):
        candidates = [
            {"candidate_ref": "place_a", "name": "Cafe A", "category": "cafe"},
            {"candidate_ref": "place_b", "name": "Cafe B", "category": "cafe"},
        ]
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"places": [{"name": "Cafe A"}, {"name": "Cafe B"}]},
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "compose_public_reply",
                "arguments": {
                    "messages": [
                        {"role": "system", "content": "internal system text"},
                        {"role": "user", "content": "private user text"},
                        {"role": "assistant", "content": "我會選 Cafe A 和 Cafe B。"},
                        {"role": "tool", "content": "raw tool result"},
                    ],
                    "presentation_class": "grounded_recommendation",
                    "card_intent": "explicit_set",
                    "selected_candidate_refs": ["place_a", "place_b"],
                },
            }]),
        ):
            reply, card_decision, metrics = synthesize(slc, candidate_cards=candidates)
        self.assertEqual(metrics.reply_source, "llm")
        self.assertEqual(metrics.presentation_messages, ["我會選 Cafe A 和 Cafe B。"])
        self.assertNotIn("private user text", reply)
        self.assertNotIn("raw tool result", reply)
        self.assertEqual(card_decision["selected_candidate_refs"], ["place_a", "place_b"])

    def test_schema_invalid_ordinary_compose_has_distinct_fallback_reason(self):
        candidates = [{"candidate_ref": "place_a", "name": "Cafe A", "category": "cafe"}]
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"places": [{"name": "Cafe A"}]},
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "compose_public_reply",
                "arguments": {
                    "messages": "not-a-list",
                    "presentation_class": "grounded_recommendation",
                    "card_intent": "explicit_set",
                    "selected_candidate_refs": ["place_a"],
                },
            }]),
        ):
            _reply, _card_decision, metrics = synthesize(slc, candidate_cards=candidates)
        self.assertEqual(metrics.reply_source, "observation_fallback")
        self.assertEqual(metrics.fallback_reason, "compose_schema_invalid")

    def test_malformed_legacy_ordinary_blocks_are_ignored(self):
        candidates = [
            {"candidate_ref": "place_a", "name": "Cafe A", "category": "cafe"},
            {"candidate_ref": "place_b", "name": "Cafe B", "category": "cafe"},
        ]
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"places": [{"name": "Cafe A"}, {"name": "Cafe B"}]},
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "compose_public_reply",
                "arguments": {
                    "messages": ["Cafe A 和 Cafe B 都值得比較。"],
                    "presentation_class": "grounded_recommendation",
                    "card_intent": "explicit_set",
                    "selected_candidate_refs": ["place_a", "place_b"],
                    "blocks": [{
                        "type": "candidate_refs",
                        "candidate_refs": ["place_a", "place_b"],
                    }],
                },
            }]),
        ):
            reply, card_decision, metrics = synthesize(slc, candidate_cards=candidates)
        self.assertEqual(metrics.reply_source, "llm")
        self.assertIn("Cafe A", reply)
        self.assertEqual(card_decision["selected_candidate_refs"], ["place_a", "place_b"])

    def test_explicit_final_count_one_selects_one_card(self):
        candidates = [
            {"candidate_ref": "place_a", "name": "Cafe A", "category": "cafe"},
            {"candidate_ref": "place_b", "name": "Cafe B", "category": "cafe"},
        ]
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"requested_limit": 2, "places": [{"name": "Cafe A"}, {"name": "Cafe B"}]},
        }])
        slc.payload["message"] = "找附近咖啡廳，最後推薦 1 家"
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "compose_public_reply",
                "arguments": {
                    "messages": ["我會推薦 Cafe A。"],
                    "presentation_class": "grounded_recommendation",
                    "card_intent": "explicit_set",
                    "selected_candidate_refs": ["place_a"],
                    "recommended_candidate_refs": ["place_a"],
                    "discussed_candidate_refs": ["place_a", "place_b"],
                },
            }]),
        ):
            _reply, card_decision, metrics = synthesize(slc, candidate_cards=candidates)
        self.assertEqual(metrics.reply_source, "llm")
        self.assertEqual(card_decision["selected_candidate_refs"], ["place_a"])
        self.assertEqual(card_decision["indices"], [0])

    def test_explicit_browse_count_three_selects_three_cards(self):
        candidates = [
            {"candidate_ref": f"place_{index}", "name": f"Cafe {index}", "category": "cafe"}
            for index in range(3)
        ]
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"requested_limit": 3, "places": [{"name": item["name"]} for item in candidates]},
        }])
        slc.payload["message"] = "給我 3 家咖啡廳看看"
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "compose_public_reply",
                "arguments": {
                    "messages": ["先給你這 3 家比較。"],
                    "presentation_class": "grounded_recommendation",
                    "card_intent": "explicit_set",
                    "selected_candidate_refs": [item["candidate_ref"] for item in candidates],
                    "discussed_candidate_refs": [item["candidate_ref"] for item in candidates],
                },
            }]),
        ):
            _reply, card_decision, _metrics = synthesize(slc, candidate_cards=candidates)
        self.assertEqual(len(card_decision["selected_candidate_refs"]), 3)
        self.assertEqual(card_decision["indices"], [0, 1, 2])

    def test_places_web_partial_uses_llm_and_preserves_limitation(self):
        candidate_ref = "place_a"
        candidates = [{
            "candidate_ref": candidate_ref, "name": "Cafe A", "category": "cafe",
            "distance_label": "100 公尺",
        }]
        web = self._web_result(
            status="partial",
            findings=[{
                "claim": "Cafe A 的公開頁面列出週六營業到 22:00。",
                "relation": "direct",
                "subject_ref": candidate_ref,
                "source_urls": ["https://example.com/cafe-a"],
            }],
            limitations=["尚未確認其他候選的週六營業時間。"],
        )
        slc = self._slice([
            {"task_id": "places1", "status": "ok", "tool": "places.search_nearby",
             "result": {"requested_limit": 2, "places": [{"name": "Cafe A"}]}},
            {"task_id": "web1", "status": "ok", "tool": None, "result": web},
        ])
        composed = _fc_result(tool_calls=[{
            "name": "compose_public_reply",
            "arguments": {
                "messages": ["Cafe A 有週六營業到 22:00 的直接公開資訊，但其他候選尚未確認。"],
                "presentation_class": "grounded_recommendation",
                    "card_intent": "curated",
                "selected_candidate_refs": [candidate_ref],
                "recommended_candidate_refs": [candidate_ref],
                "discussed_candidate_refs": [candidate_ref],
            },
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=composed,
        ) as provider:
            reply, card_decision, metrics = synthesize(slc, candidate_cards=candidates)
        provider.assert_called_once()
        self.assertEqual(metrics.reply_source, "llm")
        self.assertIn("週六營業到 22:00", reply)
        self.assertIn("其他候選尚未確認", reply)
        self.assertEqual(card_decision["selected_candidate_refs"], [candidate_ref])

    def test_invalid_candidate_ref_composition_uses_places_fallback(self):
        candidates = [{"candidate_ref": "place_a", "name": "Cafe A", "category": "cafe"}]
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"requested_limit": 1, "places": [{"name": "Cafe A"}]},
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "compose_public_reply",
                "arguments": {
                    "messages": ["我推薦不存在的候選。"],
                    "presentation_class": "grounded_recommendation",
                    "card_intent": "explicit_set",
                    "selected_candidate_refs": ["place_invented"],
                    "recommended_candidate_refs": ["place_invented"],
                    "discussed_candidate_refs": ["place_invented"],
                },
            }]),
        ) as provider:
            reply, card_decision, metrics = synthesize(slc, candidate_cards=candidates)
        provider.assert_called_once()
        self.assertEqual(metrics.reply_source, "observation_fallback")
        self.assertEqual(metrics.fallback_reason, "unsupported_claim")
        self.assertNotIn("place_invented", str(card_decision))
        self.assertIn("Cafe A", reply)

    def test_unverified_place_limitation_is_accepted_as_grounded_composition(self):
        candidates = [{
            "candidate_ref": "place_a", "name": "Cafe A", "category": "cafe",
        }]
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"places": [{"name": "Cafe A", "category": "cafe"}]},
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "compose_public_reply",
                "arguments": {
                    "messages": ["目前沒有足夠資料確認是否適合約會。"],
                    "presentation_class": "grounded_recommendation",
                    "card_intent": "explicit_set",
                    "selected_candidate_refs": ["place_a"],
                },
            }]),
        ):
            reply, _card_decision, metrics = synthesize(slc, candidate_cards=candidates)
        self.assertEqual(metrics.reply_source, "llm")
        self.assertIn("目前沒有足夠資料確認是否適合約會", reply)

    def test_unsupported_place_quality_claim_is_prompt_constrained_not_posthoc_rejected(self):
        candidates = [{
            "candidate_ref": "place_a", "name": "Cafe A", "category": "cafe",
            "distance_label": "100 公尺",
        }]
        slc = self._slice([{
            "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
            "result": {"places": [{"name": "Cafe A", "category": "cafe"}]},
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "compose_public_reply",
                "arguments": {
                    "messages": ["Cafe A，這家很安靜，很適合約會。"],
                    "presentation_class": "grounded_recommendation",
                    "card_intent": "explicit_set",
                    "selected_candidate_refs": ["place_a"],
                },
            }]),
        ) as provider:
            reply, _card_decision, metrics = synthesize(slc, candidate_cards=candidates)
        provider.assert_called_once()
        self.assertEqual(metrics.reply_source, "llm")
        self.assertIsNone(metrics.fallback_reason)
        self.assertIn("很安靜", reply)
        self.assertIn("適合約會", reply)
        self.assertIn("Cafe A", reply)

    def test_prompt_requires_unsupported_qualitative_claims_to_be_unverified(self):
        prompt = _synthesizer_system_prompt("grounded_result", True)
        self.assertIn("Affirmative atmosphere, quality, or date-suitability claims require matching typed Web findings or other typed evidence", prompt)
        self.assertIn("state that it is unverified", prompt)

    def test_current_match_is_not_aggregate_contact_count_authority(self):
        prompt = _build_prompt({
            "message": "我一共配到幾個人？",
            "observations": [{
                "task_id": "m1", "status": "ok", "tool": "match.get_counterparty_summary",
                "result": {"display_name": "小哲", "match_state": "accepted"},
            }],
        }, [])
        self.assertIn('"accepted_contact_aggregate": "unavailable"', prompt)
        self.assertIn('"current_match_observation": "singleton_only"', prompt)
        self.assertIn('"count_authority": "unavailable"', prompt)
        system = _synthesizer_system_prompt("grounded_result", False)
        self.assertIn("不得從 `counterparty`、`display_name` 或 current match 推導", system)

    def test_accepted_contact_list_is_aggregate_count_authority(self):
        prompt = _build_prompt({
            "message": "我一共配到幾個人？",
            "observations": [{
                "task_id": "r1", "status": "ok", "tool": "relationship.list_accepted_contacts",
                "result": {"contacts": [{"display_name": "小哲"}], "truncated": False, "total_count": 1},
            }],
        }, [])
        self.assertIn('"accepted_contact_aggregate": "available"', prompt)
        self.assertIn('"count_authority": "relationship.list_accepted_contacts.total_count_only"', prompt)

    def test_unavailable_place_web_lookup_uses_fallback_after_invalid_composition(self):
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
        provider.assert_called_once()
        self.assertIn("Web 查證沒有完成", reply)
        self.assertIn("不能確認", reply)
        self.assertIn("咖啡店 0", reply)
        self.assertIn("咖啡店 1", reply)
        self.assertNotIn("###", reply)
        self.assertNotIn("。。", reply)
        self.assertIsNone(card_decision)
        self.assertEqual(metrics.presentation_blocks, [])
        self.assertEqual(metrics.fallback_reason, "provider_error")

    def test_calendar_places_success_composes_schedule_and_recommendation(self):
        candidate_ref = "place_calendar_0001"
        candidates = [{
            "candidate_ref": candidate_ref,
            "name": "Cafe A",
            "category": "cafe",
            "distance_label": "300 m",
        }]
        slc = self._slice([
            {
                "task_id": "calendar1", "status": "ok", "tool": "calendar.list_my_events",
                "result": {"events": [{
                    "title": "Team dinner", "date": "2026-08-12", "start_time": "19:00",
                }]},
            },
            {
                "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
                "result": {"places": [{"name": "Cafe A", "category": "cafe"}]},
            },
        ])
        composed = _fc_result(tool_calls=[{
            "name": "compose_public_reply",
            "arguments": {
                "messages": ["你週三 19:00 有 Team dinner；附近的 Cafe A 可以當作候選。"],
                "presentation_class": "grounded_recommendation",
                "card_intent": "curated",
                "selected_candidate_refs": [candidate_ref],
                "recommended_candidate_refs": [candidate_ref],
                "discussed_candidate_refs": [candidate_ref],
            },
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=composed,
        ) as provider:
            reply, card_decision, metrics = synthesize(slc, candidate_cards=candidates)
        provider.assert_called_once()
        self.assertEqual(metrics.reply_source, "llm")
        self.assertIn("Team dinner", reply)
        self.assertIn("19:00", reply)
        self.assertIn("Cafe A", reply)
        self.assertEqual(card_decision["selected_candidate_refs"], [candidate_ref])

    def test_mixed_calendar_places_web_unavailable_composes_all_successful_observations(self):
        candidate_ref = "place_candidate_calendar_0001"
        candidates = [{
            "candidate_ref": candidate_ref,
            "name": "Cafe A",
            "category": "cafe",
            "distance_label": "300 m",
        }]
        slc = self._slice([
            {
                "task_id": "calendar1", "status": "ok", "tool": "calendar.list_my_events",
                "result": {"events": [{
                    "title": "Team dinner", "date": "2026-08-12", "start_time": "19:00",
                }]},
            },
            {
                "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
                "result": {"places": [{"name": "Cafe A", "category": "cafe"}]},
            },
            {
                "task_id": "web1", "status": "ok", "tool": None,
                "result": self._web_result(
                    status="insufficient_evidence",
                    execution_status="unavailable",
                    findings=[],
                    limitations=["Web verification unavailable"],
                ),
            },
        ])
        composed = _fc_result(tool_calls=[{
            "name": "compose_public_reply",
            "arguments": {
                "messages": [
                    "Team dinner is scheduled for 2026-08-12 at 19:00. Cafe A is a nearby candidate. Web verification unavailable.",
                ],
                "presentation_class": "grounded_recommendation",
                "card_intent": "curated",
                "selected_candidate_refs": [candidate_ref],
                "recommended_candidate_refs": [candidate_ref],
                "discussed_candidate_refs": [candidate_ref],
            },
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=composed,
        ) as provider:
            reply, card_decision, metrics = synthesize(slc, candidate_cards=candidates)

        provider.assert_called_once()
        prompt = provider.call_args.args[0]
        self.assertIn("Team dinner", prompt)
        self.assertIn('"execution_status": "unavailable"', prompt)
        self.assertIn("Team dinner", reply)
        self.assertIn("19:00", reply)
        self.assertIn("Cafe A", reply)
        self.assertIn("Web verification unavailable", reply)
        self.assertEqual(card_decision["indices"], [0])
        self.assertEqual(metrics.reply_source, "llm")

    def test_mixed_calendar_places_web_insufficient_evidence_keeps_calendar(self):
        slc = self._slice([
            {
                "task_id": "calendar1", "status": "ok", "tool": "calendar.list_my_events",
                "result": {"events": [{
                    "title": "Team dinner", "date": "2026-08-12", "start_time": "19:00",
                }]},
            },
            {
                "task_id": "places1", "status": "ok", "tool": "places.search_nearby",
                "result": {"places": [{"name": "Cafe A", "category": "cafe"}]},
            },
            {
                "task_id": "web1", "status": "ok", "tool": None,
                "result": self._web_result(
                    status="insufficient_evidence",
                    execution_status="completed",
                    findings=[],
                    limitations=["Web evidence is insufficient"],
                ),
            },
        ])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(
                content="Team dinner remains scheduled for 2026-08-12 at 19:00; Web evidence is insufficient.",
            ),
        ) as provider:
            reply, _card_decision, metrics = synthesize(slc, candidate_cards=[])

        provider.assert_called_once()
        self.assertIn("Team dinner", reply)
        self.assertIn("19:00", reply)
        self.assertIn("Web evidence is insufficient", reply)
        self.assertEqual(metrics.reply_source, "llm")

    def test_insufficient_place_web_lookup_labels_cards_as_unconfirmed_after_fallback(self):
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
        provider.assert_called_once()
        self.assertIn("Web 資訊不足", reply)
        self.assertIn("沒有可確認的營業時間", reply)
        self.assertNotIn("不是已確認推薦", reply)
        self.assertNotIn("###", reply)
        self.assertIsNone(card_decision)
        self.assertEqual(_metrics.presentation_blocks, [])

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
        self.assertIsNone(card_decision)

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

    def test_match_status_provider_failure_uses_verified_state_fallback(self):
        slc = self._slice([{
            "task_id": "m1", "status": "ok", "tool": "match.get_status",
            "result": {
                "state": "waiting_other", "scope": "live_match",
                "is_terminal": False, "chat_opened": False,
                "counterparty": "對方", "revision": 2,
                "updated_at": None, "reason_code": None,
            },
        }])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            side_effect=Exception("provider down"),
        ):
            reply, _card_decision, metrics = synthesize(slc)

        self.assertIn("等對方回覆", reply)
        self.assertNotIn("沒接好", reply)
        self.assertEqual(metrics.reply_source, "observation_fallback")

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

    def test_date_invitation_product_info_answers_the_actual_question_naturally(self):
        slc = self._slice([{
            "task_id": "product_info", "status": "ok", "tool": None,
            "result": {"product_info": {
                "manifest_version": "v6",
                "topics": [],
                "knowledge_sections": ["relationship.date_invitation"],
                "facts": {"relationship.date_invitation": {
                    "available_in_public_ayue": True,
                    "requires_confirmation": True,
                }},
            }},
        }])
        slc.payload["message"] = "欸所以約會卡是啥？"
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(
                content="約會卡就是放在你們聊天室裡的共同約會小卡，等對方接受後，你們可以一起填寫約會資料；雙方確認完成後才會同步到行事曆。",
            ),
        ) as provider:
            reply, card_decision, metrics = synthesize(slc)
        self.assertIn("約會卡就是放在你們聊天室裡的共同約會小卡", reply)
        self.assertIn("雙方確認完成後才會同步到行事曆", reply)
        self.assertIsNone(card_decision)
        self.assertEqual(metrics.reply_source, "llm")
        self.assertIn("欸所以約會卡是啥", provider.call_args.args[0])
        self.assertIn("relationship.date_invitation", provider.call_args.args[0])

    def test_date_invitation_product_info_answers_calendar_timing(self):
        slc = self._slice([{
            "task_id": "product_info", "status": "ok", "tool": None,
            "result": {"product_info": {
                "manifest_version": "v6",
                "topics": [],
                "knowledge_sections": ["relationship.date_invitation"],
                "facts": {"relationship.date_invitation": {
                    "available_in_public_ayue": True,
                    "waits_for_partner_acceptance": True,
                    "participants_fill_details_later": True,
                }},
            }},
        }])
        slc.payload["message"] = "資料喬好之後會自己進行事曆嗎？"
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion_with_tools",
            return_value=_fc_result(
                content="還要等你們雙方都確認，確認完成後才會同步到行事曆喔～",
            ),
        ) as provider:
            reply, card_decision, metrics = synthesize(slc)
        self.assertIn("雙方都確認", reply)
        self.assertIn("同步到行事曆", reply)
        self.assertIsNone(card_decision)
        self.assertEqual(metrics.reply_source, "llm")
        self.assertIn("資料喬好之後會自己進行事曆嗎", provider.call_args.args[0])

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
        mock_call.assert_not_called()

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
