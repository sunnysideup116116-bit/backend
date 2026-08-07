import unittest

from services.ayue_agent.contracts import AgentTurnContextV2, TurnClockV1
from services.ayue_agent.v3.context_slicer import slice_for_agent


def _clock():
    return TurnClockV1(
        timezone="Asia/Taipei", utc_iso="2026-08-04T12:00:00+00:00",
        local_iso="2026-08-04T20:00:00+08:00", local_date="2026-08-04",
        local_time="20:00", weekday_zh_tw="星期二",
    )


class V3ContextSlicerTests(unittest.TestCase):
    def setUp(self):
        self.turn = AgentTurnContextV2(
            user_id="owner", room_id="room", message="幫我看看這週末有什麼安排",
            recent_messages=[{"role": "user", "content": "上次去過那家咖啡廳"}],
            recent_context="最近想找個安靜的地方放鬆",
            user_location="台北市",
            relevant_memories=["喜歡戶外活動"],
            clock=_clock(),
        )

    def test_calendar_slice_excludes_match_and_places(self):
        s = slice_for_agent("calendar", self.turn, prior_observations=[])
        self.assertEqual(s.agent, "calendar")
        # calendar slice should contain events/time/history but NOT match proposal or places
        self.assertIn("clock", s.payload)
        self.assertIn("recent_messages", s.payload)
        self.assertNotIn("active_proposal", s.payload)
        self.assertNotIn("user_location", s.payload)

    def test_places_slice_excludes_calendar_and_match(self):
        s = slice_for_agent("places", self.turn, prior_observations=[])
        self.assertEqual(s.agent, "places")
        self.assertIn("user_location", s.payload)
        self.assertIn("clock", s.payload)
        self.assertNotIn("active_proposal", s.payload)
        self.assertNotIn("recent_context", s.payload)

    def test_match_slice_excludes_calendar_details(self):
        s = slice_for_agent("match", self.turn, prior_observations=[])
        self.assertEqual(s.agent, "match")
        self.assertIn("clock", s.payload)
        self.assertIn("active_proposal", s.payload)
        self.assertNotIn("user_location", s.payload)
        self.assertNotIn("recent_context", s.payload)

    def test_relationship_slice_excludes_calendar_and_match_details(self):
        s = slice_for_agent("relationship", self.turn, prior_observations=[])
        self.assertEqual(s.agent, "relationship")
        self.assertIn("mentioned_contacts", s.payload)
        self.assertIn("recent_messages", s.payload)
        self.assertNotIn("active_proposal", s.payload)

    def test_profile_slice_excludes_match_and_calendar(self):
        s = slice_for_agent("profile", self.turn, prior_observations=[])
        self.assertEqual(s.agent, "profile")
        self.assertIn("recent_context", s.payload)
        self.assertIn("relevant_memories", s.payload)
        self.assertNotIn("active_proposal", s.payload)
        self.assertNotIn("user_location", s.payload)

    def test_synthesizer_slice_contains_all_observations(self):
        prior = [{"task_id": "t1", "tool": "calendar.list_my_events", "result": {"events": []}}]
        s = slice_for_agent("synthesizer", self.turn, prior_observations=prior)
        self.assertEqual(s.agent, "synthesizer")
        self.assertIn("observations", s.payload)
        self.assertEqual(s.payload["observations"], prior)
        self.assertIn("recent_messages", s.payload)

    def test_prior_observations_injected_into_dependent_places_slice(self):
        prior = [{"task_id": "t2", "tool": "places.search_nearby", "result": {"places": [{"name": "陽明山國家公園"}]}}]
        s = slice_for_agent("places", self.turn, prior_observations=prior)
        self.assertIn("prior_observations", s.payload)
        self.assertEqual(s.payload["prior_observations"], prior)

    def test_calendar_write_slice_carries_find_candidates(self):
        # 修改/取消行程：read task 的 find_my_event ambiguous+candidates 必須
        # 完整進入 write task 的 context，write agent 才能比對候選後再提出寫入。
        prior = [{
            "task_id": "t1", "status": "ok", "tool": "calendar.find_my_event",
            "result": {
                "status": "ambiguous", "reason_code": "event_ambiguous",
                "candidates": [
                    {"activity": "吃牛排", "date": "2026-08-12", "start_time": "18:00", "end_time": "20:00"},
                    {"activity": "吃牛排", "date": "2026-08-15", "start_time": "12:00", "end_time": "13:00"},
                ],
            },
            "error_code": None, "skip_reason": None,
        }]
        s = slice_for_agent("calendar", self.turn, prior_observations=prior)
        self.assertIn("prior_observations", s.payload)
        self.assertEqual(s.payload["prior_observations"], prior)

    def test_calendar_write_slice_carries_found_event(self):
        prior = [{
            "task_id": "t1", "status": "ok", "tool": "calendar.find_my_event",
            "result": {
                "status": "found", "reason_code": "",
                "activity": "吃牛排", "date": "2026-08-12",
                "start_time": "18:00", "end_time": "20:00",
                "event_kind": "personal", "candidates": [],
            },
            "error_code": None, "skip_reason": None,
        }]
        s = slice_for_agent("calendar", self.turn, prior_observations=prior)
        self.assertEqual(s.payload["prior_observations"], prior)

    def test_unknown_agent_raises(self):
        with self.assertRaises(ValueError):
            slice_for_agent("nope", self.turn, prior_observations=[])


if __name__ == "__main__":
    unittest.main()