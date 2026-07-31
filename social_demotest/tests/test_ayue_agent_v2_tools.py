import unittest
from unittest.mock import patch

from services.ayue_agent.contracts import AgentTurnContext, ToolCall, TurnClockV1
from services.ayue_agent.router import generate_clarification_reply_v2, generate_final_reply_v2
from services.ayue_agent.contracts import AgentTurnContextV2
from services.ayue_agent.tools import (
    _counterparty_summary,
    _match_latest_outcome,
    _recent_context,
    _relationship_evidence,
    execute_tool,
)
from services.ayue_agent.runtime import _reply_from_observation


class AyueAgentV2ToolTests(unittest.TestCase):
    def test_current_time_output_matches_the_registered_schema(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="今天幾月幾號")
        clock = TurnClockV1(
            timezone="Asia/Taipei",
            utc_iso="2026-07-30T06:30:00+00:00",
            local_iso="2026-07-30T14:30:00+08:00",
            local_date="2026-07-30",
            local_time="14:30",
            weekday_zh_tw="星期四",
            temporal_references={},
        )
        result = execute_tool(ToolCall(name="system.get_current_time"), ctx, clock=clock)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["version"], "v1")

    def test_canonical_status_renderer_answers_accepted_without_planner_guessing(self):
        self.assertEqual(
            _reply_from_observation("match.get_status", {"state": "accepted", "counterparty": "對方"}),
            "有，對方也已經接受了，聊天室已經開啟。",
        )

    def test_latest_outcome_includes_acceptance(self):
        match = {
            "from_user": "owner", "to_user": "other", "status": "accepted",
            "last_decision": {"actor": "other", "action": "accept"},
        }
        with patch("services.ayue_agent.tools.matches_coll.find_one", return_value=match):
            result = _match_latest_outcome("owner")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "accepted")
        self.assertEqual(
            _reply_from_observation("match.get_latest_outcome", result.data),
            "對方已經接受了，聊天室也已經開啟。",
        )

    def test_relationship_result_never_exposes_internal_user_id(self):
        match = {
            "from_user": "owner", "to_user": "seed_user_08", "status": "accepted",
            "relationship_memory": {"shared_summary": "一起聊過旅行"},
        }
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="對方是怎樣的人")
        with patch("services.ayue_agent.tools.matches_coll.find", return_value=[match]), \
             patch("services.ayue_agent.public_relationship_projection.profiles_coll.find_one", return_value={}):
            result = _relationship_evidence(ctx, None)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["relationships"][0]["counterparty"], "對方")
        self.assertNotIn("other_id", result.data["relationships"][0])
        self.assertNotIn("seed_user_08", str(result.data))

    def test_counterparty_summary_uses_only_public_reason_and_no_internal_id(self):
        match = {
            "from_user": "owner", "to_user": "seed_user_08", "status": "accepted",
            "reason_items": [
                {"kind": "context_pair", "text": "@seed_user_08 最近提到私人行程"},
                {"kind": "shared_graph", "text": "你們都偏好居酒屋"},
            ],
            "match_context_snapshot": {"candidate": {"current_context": "不應出現"}},
        }
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="他是誰")
        with patch("services.ayue_agent.tools.get_counterparty_match_source", return_value={
                 "ambiguous": False, "match": match,
             }), \
             patch("services.ayue_agent.public_relationship_projection.profiles_coll.find_one", return_value={}):
            result = _counterparty_summary(ctx)
        self.assertTrue(result.ok)
        self.assertTrue(result.data["found"])
        self.assertEqual(result.data["match_state"], "accepted")
        self.assertTrue(result.data["chat_opened"])
        self.assertEqual(result.data["display_name"], "對方")
        self.assertEqual(result.data["safe_summary"], "你們都偏好居酒屋")
        self.assertNotIn("seed_user_08", str(result.data))
        self.assertNotIn("不應出現", str(result.data))

    def test_recent_context_tool_is_read_only_and_returns_existing_value(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room", message="你記得我最近想去哪嗎",
            user_profile={"current_context": "近期規劃前往合掌村旅行", "current_context_revision": 7},
        )
        with patch("services.ayue_agent.tools.profiles_coll.find_one", return_value={
            "current_context": "近期規劃前往合掌村旅行", "current_context_revision": 7,
        }):
            result = _recent_context(ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.data, {
            "current_context": "近期規劃前往合掌村旅行", "revision": 7, "exists": True,
        })

    def test_counterparty_summary_is_available_through_registry_executor(self):
        match = {
            "from_user": "owner", "to_user": "other", "status": "accepted",
            "reason_items": [{"kind": "shared_value", "text": "你們都重視坦誠溝通"}],
        }
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="他是誰")
        with patch("services.ayue_agent.tools.get_counterparty_match_source", return_value={
                 "ambiguous": False, "match": match,
             }), \
             patch("services.ayue_agent.public_relationship_projection.profiles_coll.find_one", return_value={"display_name": "小安"}):
            result = execute_tool(ToolCall(name="match.get_counterparty_summary"), ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["display_name"], "小安")
        self.assertEqual(result.data["safe_summary"], "你們都重視坦誠溝通")

    def test_counterparty_summary_exposes_only_approved_public_profile_fields(self):
        match = {
            "from_user": "owner", "to_user": "seed_user_08", "status": "accepted",
            "reason_items": [{"kind": "shared_context", "text": "你們近期都提到京都賞櫻"}],
            "distinctive_tags": ["京都", "真誠溝通"], "recommendation_tier": "grounded",
        }
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="他是誰")
        public_profile = {
            "display_name": "小安", "current_context": "最近正在規劃京都賞櫻",
            "initial_interest": "能一起慢慢旅行的人", "big_five": {"summary": "溫和而願意傾聽"},
            "profile_memory_summary": "不應公開", "calendar": "不應公開",
        }
        with patch("services.ayue_agent.tools.get_counterparty_match_source", return_value={
                 "ambiguous": False, "match": match,
             }), patch("services.ayue_agent.public_relationship_projection.profiles_coll.find_one", return_value=public_profile):
            result = _counterparty_summary(ctx)
        self.assertEqual(result.data["display_name"], "小安")
        self.assertEqual(result.data["recent_context"], "最近正在規劃京都賞櫻")
        self.assertEqual(result.data["initial_interest"], "能一起慢慢旅行的人")
        self.assertEqual(result.data["personality_summary"], "溫和而願意傾聽")
        self.assertEqual(result.data["verified_common_ground"], ["你們近期都提到京都賞櫻"])
        self.assertNotIn("seed_user_08", str(result.data))
        self.assertNotIn("不應公開", str(result.data))

    def test_recent_context_is_available_through_registry_executor(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room", message="你記得嗎",
            user_profile={"current_context": "近期規劃前往合掌村旅行", "current_context_revision": 7},
        )
        with patch("services.ayue_agent.tools.profiles_coll.find_one", return_value={
            "current_context": "近期規劃前往合掌村旅行", "current_context_revision": 7,
        }):
            result = execute_tool(ToolCall(name="profile.get_recent_context"), ctx)
        self.assertTrue(result.ok)
        self.assertTrue(result.data["exists"])
        self.assertEqual(result.data["revision"], 7)

    def test_recent_context_prefers_newer_stored_projection_over_turn_snapshot(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room", message="你記得嗎",
            user_profile={"current_context": "舊情境", "current_context_revision": 1},
        )
        with patch("services.ayue_agent.tools.profiles_coll.find_one", return_value={
            "current_context": "近期規劃前往合掌村旅行", "current_context_revision": 2,
        }) as read:
            result = _recent_context(ctx)
        read.assert_called_once()
        self.assertEqual(result.data["revision"], 2)
        self.assertIn("合掌村", result.data["current_context"])

    def test_composer_fallback_answers_from_verified_recent_context(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="你有記得我想去合掌村嗎")
        observations = [{"tool": "profile.get_recent_context", "result": {
            "current_context": "近期規劃前往合掌村旅行", "revision": 7, "exists": True,
        }}]
        with patch("services.ayue_agent.router.generate_chat_completion", side_effect=RuntimeError("offline")):
            reply = generate_final_reply_v2(ctx, observations)
        self.assertIn("合掌村", reply)
        self.assertNotIn("工具", reply)

    def test_composer_reports_llm_and_deterministic_fallback_outcomes(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="最近還好嗎")
        outcomes = []
        with patch("services.ayue_agent.router.generate_chat_completion", return_value="我在呀。"):
            reply = generate_final_reply_v2(ctx, [], outcome_sink=outcomes.append)
        self.assertEqual(reply, "我在呀。")
        self.assertEqual(outcomes, ["llm_reply"])

        outcomes.clear()
        with patch("services.ayue_agent.router.generate_chat_completion", return_value="我目前沒有工具可以處理"):
            fallback = generate_final_reply_v2(ctx, [], outcome_sink=outcomes.append)
        self.assertNotIn("工具", fallback)
        self.assertEqual(outcomes, ["deterministic_fallback:internal_meta_rejected"])

        outcomes.clear()
        with patch("services.ayue_agent.router.generate_chat_completion", side_effect=TimeoutError("provider timeout")):
            generate_final_reply_v2(ctx, [], outcome_sink=outcomes.append)
        self.assertEqual(outcomes, ["deterministic_fallback:provider_error"])

    def test_clarification_composer_rejects_canned_internal_failure_text(self):
        ctx = AgentTurnContextV2(
            user_id="owner", room_id="room", message="你幫我約個人一起去好不好",
        )
        with patch(
            "services.ayue_agent.router.generate_chat_completion",
            return_value="我需要再確認一下你的意思，暫時不會執行任何操作。",
        ):
            reply = generate_clarification_reply_v2(ctx, topic="match_target")
        self.assertEqual(
            reply,
            "你是想要我幫你找一位新的旅伴或對象，還是邀請一位你已經認識的人？",
        )
        self.assertNotIn("暫時不會執行", reply)


if __name__ == "__main__":
    unittest.main()
