# social_demotest/tests/test_v3_synthesizer.py
import unittest
from unittest.mock import patch

from services.ayue_agent.v3.contracts import AgentContextSlice
from services.ayue_agent.v3.synthesizer import synthesize
from services.ai_service import ToolCallResult


def _fc_result(content="", tool_calls=None):
    return ToolCallResult(content=content, tool_calls=tool_calls or [])


class V3SynthesizerTests(unittest.TestCase):
    def _slice(self, observations):
        return AgentContextSlice(agent="synthesizer", payload={
            "message": "你幫我看看行程和附近餐廳",
            "recent_messages": [],
            "recent_context": "",
            "user_location": "台北市",
            "clock": {"timezone": "Asia/Taipei", "local_date": "2026-08-04", "local_time": "20:00"},
            "observations": observations,
        })

    def _candidate_cards(self):
        return [
            {"name": "義式料理餐廳", "category": "restaurant", "distance_label": "726 公尺",
             "map_url": "https://maps.example.com/a", "place_id": "abc"},
            {"name": "小酒館", "category": "bar", "distance_label": "200 公尺",
             "map_url": "https://maps.example.com/b", "place_id": "def"},
        ]

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
        self.assertNotIn("我在。你可以跟我聊聊", reply)
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
            reply, _card_decision, _metrics = synthesize(slc)
        self.assertIn("沒能查到", reply)

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