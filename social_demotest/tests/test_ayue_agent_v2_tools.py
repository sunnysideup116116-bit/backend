import unittest
from unittest.mock import patch

from services.ayue_agent.contracts import AgentTurnContext, ToolCall, TurnClockV1
from services.ayue_agent.router import generate_clarification_reply_v2, generate_final_reply_v2
from services.ayue_agent.contracts import AgentTurnContextV2
from services.ayue_agent.tools import (
    _calendar_find_event,
    _counterparty_summary,
    _match_latest_outcome,
    _recent_context,
    _relationship_evidence,
    _self_profile,
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

    def test_self_profile_summary_projects_completed_owner_fields_without_internal_ids(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room", message="我是誰",
            user_profile={
                "display_name": "小安", "initial_interest": "想認識願意一起看展的人",
                "big_five": {"O": 7, "E": 6, "summary": "好奇又願意主動開話題"},
                "deep_profile": {"values": ["真誠溝通"], "ideal_future": "有穩定自在的生活"},
                "current_context": "最近想去駁二看展", "profile_location": {"city": "高雄市", "district": "鹽埕區"},
                "profile_memory_preview": [
                    {"label": "獨立電影", "stance": "like", "owner_user_id": "owner"},
                    {"label": "抽菸", "stance": "avoid"},
                    {"label": "seed_user_08", "stance": "like"},
                ],
            },
        )
        result = _self_profile(ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["initial_interest"], "想認識願意一起看展的人")
        self.assertEqual(result.data["personality_summary"], "好奇又願意主動開話題")
        self.assertEqual(result.data["values"], ["真誠溝通"])
        self.assertEqual(result.data["location"], "高雄市鹽埕區")
        self.assertEqual(result.data["preferences"], ["喜歡獨立電影", "避免抽菸"])
        self.assertNotIn("seed_user_08", str(result.data))

    def test_self_profile_summary_returns_at_most_eight_safe_durable_preferences(self):
        preferences = [
            {"label": f"偏好{i}", "stance": "like", "evidence": "不得輸出"}
            for i in range(10)
        ]
        result = _self_profile(AgentTurnContext(
            user_id="owner", room_id="room", message="你記得我哪些偏好",
            user_profile={"profile_memory_preview": preferences},
        ))
        self.assertEqual(len(result.data["preferences"]), 8)
        self.assertNotIn("evidence", str(result.data))

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

    def test_pending_counterparty_keeps_identity_anonymous(self):
        match = {
            "from_user": "owner", "to_user": "other", "status": "pending",
            "reason_items": [{"kind": "shared_value", "text": "小樂也重視坦誠溝通"}],
        }
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="他是誰")
        with patch("services.ayue_agent.tools.get_counterparty_match_source", return_value={
            "ambiguous": False, "match": match,
        }), patch("services.ayue_agent.public_relationship_projection.profiles_coll.find_one", return_value={
            "display_name": "小樂", "initial_interest": "小樂想找人旅行",
        }):
            result = _counterparty_summary(ctx)
        self.assertEqual(result.data["display_name"], "對方")
        self.assertEqual(result.data["initial_interest"], "對方想找人旅行")
        self.assertNotIn("小樂", str(result.data))

    def test_named_shared_calendar_event_resolves_its_verified_companion_not_latest_match(self):
        event = {
            "source_type": "date", "participants": ["owner", "small-wen"],
            "title": "與對方的約會", "activity": "看電影", "timezone": "Asia/Taipei",
            "start_at": __import__("datetime").datetime(2026, 8, 5, 11, 0),
            "end_at": __import__("datetime").datetime(2026, 8, 5, 13, 0),
        }
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="看電影是跟誰去")
        with patch("services.ayue_agent.tools.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.tools.find_owned_events", return_value=[event]), \
             patch("services.ayue_agent.tools.matches_coll.find_one", return_value={
                 "from_user": "owner", "to_user": "small-wen", "status": "accepted",
             }), patch("services.ayue_agent.public_relationship_projection.profiles_coll.find_one", return_value={
                 "display_name": "小玟",
             }):
            result = _calendar_find_event(ctx, {"event_hint": "看電影", "date_hint": ""})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["companion_display_name"], "小玟")
        self.assertNotIn("small-wen", str(result.data))

    def test_personal_calendar_event_does_not_guess_a_companion(self):
        event = {
            "source_type": "personal", "participants": ["owner"], "title": "看電影",
            "timezone": "Asia/Taipei", "start_at": __import__("datetime").datetime(2026, 8, 5, 11, 0),
            "end_at": __import__("datetime").datetime(2026, 8, 5, 13, 0),
        }
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="看電影是跟誰去")
        with patch("services.ayue_agent.tools.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.tools.find_owned_events", return_value=[event]):
            result = _calendar_find_event(ctx, {"event_hint": "看電影", "date_hint": ""})
        self.assertFalse(result.data["companion_known"])
        self.assertEqual(result.data["event_kind"], "personal")

    def test_calendar_find_resolves_a_named_accepted_contact_without_a_date(self):
        event = {
            "source_type": "date", "participants": ["owner", "small-kui"], "title": "與對方的約會",
            "activity": "看電影", "timezone": "Asia/Taipei",
            "start_at": __import__("datetime").datetime(2026, 8, 11, 11, 0),
            "end_at": __import__("datetime").datetime(2026, 8, 11, 13, 0),
        }
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="我跟小葵的約會是什麼時候")
        with patch("services.ayue_agent.tools.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.tools.accepted_contact_ids_by_display_name", return_value=["small-kui"]), \
             patch("services.ayue_agent.tools.find_owned_events", return_value=[event]) as find_events, \
             patch("services.ayue_agent.tools.matches_coll.find_one", return_value={
                 "from_user": "owner", "to_user": "small-kui",
             }), patch("services.ayue_agent.tools._display_name", return_value="小葵"):
            result = _calendar_find_event(ctx, {
                "event_hint": "約會", "date_hint": "", "companion_hint": "小葵",
            })
        self.assertTrue(result.ok)
        self.assertEqual(result.data["date"], "2026-08-11")
        self.assertEqual(result.data["companion_display_name"], "小葵")
        self.assertEqual(find_events.call_args.kwargs["companion_user_id"], "small-kui")
        fallback = _reply_from_observation("calendar.find_my_event", result.data)
        self.assertIn("2026-08-11", fallback)
        self.assertIn(f"{result.data['start_time']}–{result.data['end_time']}", fallback)
        self.assertIn("看電影", fallback)
        self.assertIn("小葵", fallback)

    def test_same_display_name_fails_closed_without_reading_calendar_contents(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="我跟小葵看電影是什麼時候")
        with patch("services.ayue_agent.tools.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.tools.accepted_contact_ids_by_display_name", return_value=["contact-a", "contact-b"]), \
             patch("services.ayue_agent.tools.find_owned_events") as find_events:
            result = _calendar_find_event(ctx, {
                "event_hint": "看電影", "date_hint": "", "companion_hint": "小葵",
            })
        self.assertEqual(result.data["status"], "ambiguous")
        self.assertEqual(result.data["reason_code"], "companion_ambiguous")
        find_events.assert_not_called()

    def test_ambiguous_calendar_result_returns_at_most_three_safe_candidates(self):
        events = [{
            "source_type": "personal", "participants": ["owner"], "title": f"看電影 {index}",
            "timezone": "Asia/Taipei",
            "start_at": __import__("datetime").datetime(2026, 8, 5 + index, 11, 0),
            "end_at": __import__("datetime").datetime(2026, 8, 5 + index, 13, 0),
        } for index in range(5)]
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="我的電影行程")
        with patch("services.ayue_agent.tools.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.tools.find_owned_events", return_value=events):
            result = _calendar_find_event(ctx, {"event_hint": "看電影", "date_hint": ""})
        self.assertEqual(result.data["status"], "ambiguous")
        self.assertEqual(len(result.data["candidates"]), 3)
        self.assertNotIn("participants", str(result.data))

    def test_unresolved_or_ambiguous_companion_returns_a_specific_clarification_reason(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="我跟小葵的約會呢")
        with patch("services.ayue_agent.tools.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.tools.accepted_contact_ids_by_display_name", return_value=[]):
            missing = _calendar_find_event(ctx, {
                "event_hint": "約會", "date_hint": "", "companion_hint": "小葵",
            })
        self.assertEqual(missing.data["reason_code"], "companion_not_found")
        self.assertIn("稱呼", _reply_from_observation("calendar.find_my_event", missing.data))

        with patch("services.ayue_agent.tools.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.tools.accepted_contact_ids_by_display_name", return_value=["a", "b"]), \
             patch("services.ayue_agent.tools.find_owned_events") as find_events:
            ambiguous = _calendar_find_event(ctx, {
                "event_hint": "約會", "date_hint": "", "companion_hint": "小葵",
            })
        self.assertEqual(ambiguous.data["reason_code"], "companion_ambiguous")
        self.assertIn("同名", _reply_from_observation("calendar.find_my_event", ambiguous.data))
        find_events.assert_not_called()

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
