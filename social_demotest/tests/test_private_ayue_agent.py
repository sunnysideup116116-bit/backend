import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from services.ayue_agent.contracts import AgentResult
from services.ayue_agent.private_runtime import (
    PrivateAgentTurnContext,
    _calendar_range_for_message,
    _calendar_reply,
    _is_partner_event_detail_query,
    is_private_calendar_query,
    private_agent_mode_for_user,
    run_private_agent_turn,
)


class PrivateAyueAgentTests(unittest.TestCase):
    def setUp(self):
        self._mode = os.environ.get("AYUE_PRIVATE_AGENTIC_MODE")
        self._allowlist = os.environ.get("AYUE_PRIVATE_AGENTIC_USER_ALLOWLIST")

    def tearDown(self):
        for key, value in (("AYUE_PRIVATE_AGENTIC_MODE", self._mode), ("AYUE_PRIVATE_AGENTIC_USER_ALLOWLIST", self._allowlist)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_partner_calendar_questions_are_detected(self):
        for message in (
            "他這週什麼時候有空？",
            "我想看他的行事曆",
            "他幾月幾號不行？",
            "他哪幾天不能？",
            "對方下週什麼時候沒空？",
            "這個月有哪些時間不方便？",
        ):
            self.assertTrue(is_private_calendar_query(message), message)
        self.assertTrue(_is_partner_event_detail_query("他那天在做什麼？"))
        self.assertFalse(is_private_calendar_query("幫我想一個聊天話題"))

    def test_calendar_range_defaults_to_31_days_and_parses_specific_date(self):
        now = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
        start, end, truncated = _calendar_range_for_message("他幾月幾號不行？", now)
        self.assertEqual(end - start, timedelta(days=31))
        self.assertFalse(truncated)
        start, end, truncated = _calendar_range_for_message("他 8 月 1 日不行", now)
        self.assertEqual(start.date().isoformat(), "2026-07-31")  # UTC start of 8/1 Taiwan
        self.assertEqual(end - start, timedelta(days=1))
        self.assertFalse(truncated)

    def test_calendar_range_parses_next_week(self):
        now = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)  # Thursday in Taiwan
        start, end, _ = _calendar_range_for_message("對方下週哪天沒空？", now)
        self.assertEqual(end - start, timedelta(days=7))
        self.assertEqual(start.date().isoformat(), "2026-08-02")  # Taiwan Monday midnight in UTC

    def test_busy_reply_never_contains_event_details(self):
        reply = _calendar_reply(True, [{"start_at": "2026-08-01T01:00:00+00:00", "end_at": "2026-08-01T03:00:00+00:00", "busy": "true"}])
        self.assertIn("已有安排", reply)
        self.assertIn("不能透露", reply)
        self.assertNotIn("activity", reply)
        self.assertNotIn("location", reply)

    def test_private_flag_defaults_off_and_honors_allowlist(self):
        os.environ.pop("AYUE_PRIVATE_AGENTIC_MODE", None)
        os.environ.pop("AYUE_PRIVATE_AGENTIC_USER_ALLOWLIST", None)
        self.assertEqual(private_agent_mode_for_user("seed_user_04"), "off")
        os.environ["AYUE_PRIVATE_AGENTIC_MODE"] = "on"
        os.environ["AYUE_PRIVATE_AGENTIC_USER_ALLOWLIST"] = "seed_user_04"
        self.assertEqual(private_agent_mode_for_user("seed_user_04"), "on")
        self.assertEqual(private_agent_mode_for_user("seed_user_10"), "off")

    def test_calendar_turn_returns_busy_only_answer(self):
        ctx = PrivateAgentTurnContext(
            user_id="seed_user_04", other_id="seed_user_10", room_id="private-room",
            message="他幾月幾號不行？", match_doc={"_id": "match-1", "status": "accepted"}, user_profile={},
        )
        busy = [{"start_at": "2026-08-01T01:00:00+00:00", "end_at": "2026-08-01T03:00:00+00:00", "busy": "true"}]
        with patch("services.ayue_agent.private_runtime._partner_busy", return_value=(True, busy)) as busy_tool:
            with patch("services.ayue_agent.private_runtime._trace"):
                result = run_private_agent_turn(ctx, mode="on")
        self.assertIsInstance(result, AgentResult)
        self.assertTrue(result.handled)
        self.assertEqual(result.conversation_intent, "private_partner_availability")
        self.assertNotIn("activity", result.reply)
        self.assertEqual(len(busy_tool.call_args.args), 3)

    def test_event_detail_question_is_refused_without_calendar_content(self):
        ctx = PrivateAgentTurnContext(
            user_id="seed_user_04", other_id="seed_user_10", room_id="private-room",
            message="他那天在做什麼？", match_doc={"_id": "match-1", "status": "accepted"}, user_profile={},
        )
        with patch("services.ayue_agent.private_runtime._partner_busy", return_value=(True, [])):
            with patch("services.ayue_agent.private_runtime._trace"):
                result = run_private_agent_turn(ctx, mode="on")
        self.assertIn("不能看到或透露內容", result.reply)

    def test_renderer_receives_no_advisory_profile_data(self):
        ctx = PrivateAgentTurnContext(
            user_id="seed_user_04", other_id="seed_user_10", room_id="private-room",
            message="我該怎麼跟他聊？", match_doc={"_id": "match-1", "status": "accepted"}, user_profile={},
        )
        advisory = {"private_secret": "never disclose"}
        facts = [{"evidence_id": "shared:summary", "visibility": "shared_fact", "value": "你們都聊過電影"}]
        with patch("services.ayue_agent.private_runtime._partner_advisory_profile", return_value=advisory):
            with patch("services.ayue_agent.private_runtime._shared_and_consented_facts", return_value=facts):
                with patch("services.ayue_agent.private_runtime._strategy", return_value={"pace": "normal"}):
                    with patch("services.ayue_agent.private_runtime._render_advice", return_value="可以從最近看的電影聊起。") as render:
                        with patch("services.ayue_agent.private_runtime._trace"):
                            result = run_private_agent_turn(ctx, mode="on")
        self.assertTrue(result.handled)
        self.assertEqual(render.call_args.args[2], facts)
        self.assertNotIn("private_secret", str(render.call_args.args[2]))


if __name__ == "__main__":
    unittest.main()