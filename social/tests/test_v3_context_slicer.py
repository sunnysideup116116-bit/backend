import unittest

from services.ayue_agent.contracts import PublicAgentTurnContext, TurnClockV1
from services.conversation_compaction_contracts import ConversationSummaryV1
from services.ayue_agent.v3.context_slicer import slice_for_agent


def _clock():
    return TurnClockV1(
        timezone="Asia/Taipei", utc_iso="2026-08-04T12:00:00+00:00",
        local_iso="2026-08-04T20:00:00+08:00", local_date="2026-08-04",
        local_time="20:00", weekday_zh_tw="星期二",
    )


class V3ContextSlicerTests(unittest.TestCase):
    def setUp(self):
        self.turn = PublicAgentTurnContext(
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
        # Planner resolves cross-turn language before Calendar receives this slice.
        self.assertIn("clock", s.payload)
        self.assertNotIn("recent_messages", s.payload)
        self.assertNotIn("active_proposal", s.payload)
        self.assertNotIn("user_location", s.payload)

    def test_owner_relationship_memory_is_visible_only_to_relationship_agent(self):
        self.turn.owner_relationship_memories = [{
            "topic": "聊天感受",
            "owner_view": "本人覺得和對方聊天很自在",
            "source": "owner_private_relationship_memory",
        }]
        relationship = slice_for_agent("relationship", self.turn, prior_observations=[])
        match = slice_for_agent("match", self.turn, prior_observations=[])
        calendar = slice_for_agent("calendar", self.turn, prior_observations=[])
        self.assertEqual(
            relationship.payload["owner_private_relationship_memories"][0]["topic"],
            "聊天感受",
        )
        self.assertNotIn("owner_private_relationship_memories", match.payload)
        self.assertNotIn("owner_private_relationship_memories", calendar.payload)

    def test_places_slice_excludes_calendar_and_match(self):
        s = slice_for_agent("places", self.turn, prior_observations=[])
        self.assertEqual(s.agent, "places")
        self.assertIn("user_location", s.payload)
        self.assertIn("clock", s.payload)
        self.assertNotIn("active_proposal", s.payload)
        self.assertNotIn("recent_context", s.payload)

    def test_places_slice_ignores_legacy_candidate_projection(self):
        self.turn.recent_place_candidates = {
            "snapshot_version": "v3-place-presentation-v1",
            "candidates": [{
                "reference": "place_ref_0123456789abcdef01234567",
                "ordinal": 1,
                "label": "旧店",
                "category": "cafe",
                "address_summary": "高雄市",
            }],
        }

        sliced = slice_for_agent("places", self.turn, prior_observations=[])

        self.assertNotIn("recent_place_candidates", sliced.payload)

    def test_places_slice_ignores_legacy_place_followup(self):
        self.turn.place_followup = {
            "fields": {"date": "2026-09-12", "start_time": "08:30"},
            "resolved_place": {
                "reference": "place_ref_0123456789abcdef01234567",
                "ordinal": 3,
                "label": "JINLIANFA 金聯發",
            },
            "abandonment_requested": True,
        }

        sliced = slice_for_agent("places", self.turn, prior_observations=[])

        self.assertNotIn("place_followup", sliced.payload)

    def test_match_slice_excludes_calendar_details(self):
        s = slice_for_agent("match", self.turn, prior_observations=[])
        self.assertEqual(s.agent, "match")
        self.assertIn("clock", s.payload)
        self.assertIn("active_proposal", s.payload)
        self.assertNotIn("user_location", s.payload)
        self.assertNotIn("recent_context", s.payload)

    def test_match_slice_carries_bounded_search_and_allowed_actions(self):
        self.turn.match_search = {
            "status": "running", "step": "vector_search", "cancellable": True,
        }
        self.turn.active_proposal = {
            "status": "pending", "counterparty": "對方",
            "user_can_decide": True, "allowed_actions": ["cancelled"],
            "proposal_revision": 9,
        }

        sliced = slice_for_agent("match", self.turn, prior_observations=[])

        self.assertEqual(sliced.payload["match_search"]["status"], "running")
        self.assertEqual(sliced.payload["active_proposal"]["allowed_actions"], ["cancelled"])
        self.assertNotIn("proposal_revision", sliced.payload["active_proposal"])

    def test_match_slice_carries_bounded_continuity_and_event_invitation(self):
        self.turn.conversation_continuity = ConversationSummaryV1(
            active_topics=["週末想逛市集"], owner_goals=["找人一起去"],
        )
        self.turn.active_event_invitation = {
            "status": "draft", "stage": "waiting_user", "event_title": "港邊市集",
            "counterparty": "小安", "user_can_decide": True, "proposal_revision": 3,
        }
        sliced = slice_for_agent("match", self.turn, prior_observations=[])
        self.assertEqual(sliced.payload["active_event_invitation"], {
            "status": "draft", "event_title": "港邊市集", "counterparty": "小安",
            "user_can_decide": True,
        })
        self.assertEqual(sliced.payload["conversation_continuity"], {
            "active_topics": ["週末想逛市集"], "owner_goals": ["找人一起去"],
            "known_continuity": [], "unresolved_questions": [],
            "ayue_commitments": [], "recent_decisions": [],
        })

    def test_web_slice_excludes_internal_profile_and_history(self):
        self.turn.recent_messages = [
            {"role": "user", "content": f"msg {i}"} for i in range(8)
        ]
        s = slice_for_agent("web", self.turn, prior_observations=[])
        self.assertEqual(s.agent, "web")
        self.assertNotIn("recent_context", s.payload)
        self.assertNotIn("active_proposal", s.payload)
        self.assertNotIn("recent_messages", s.payload)

    def test_all_agent_slices_remove_a_duplicated_current_message(self):
        self.turn.recent_messages = [
            {"role": "assistant", "content": "剛剛推薦 A 店和 B 店。"},
            {"role": "user", "content": self.turn.message},
        ]
        history_agents = {"match", "profile", "product_info", "synthesizer"}
        for agent in ("calendar", "places", "web", "match", "relationship", "profile", "product_info", "synthesizer"):
            with self.subTest(agent=agent):
                sliced = slice_for_agent(agent, self.turn, prior_observations=[])
                if agent not in history_agents:
                    self.assertNotIn("recent_messages", sliced.payload)
                    continue
                self.assertEqual(len(sliced.payload["recent_messages"]), 1)
                self.assertEqual(sliced.payload["recent_messages"][0]["role"], "assistant")
                self.assertEqual(
                    sliced.payload["recent_messages"][0]["content"],
                    "剛剛推薦 A 店和 B 店。",
                )

    def test_relationship_slice_contains_only_public_relationship_fields(self):
        s = slice_for_agent("relationship", self.turn, prior_observations=[])
        self.assertEqual(s.agent, "relationship")
        self.assertIn("clock", s.payload)
        self.assertNotIn("recent_messages", s.payload)
        self.assertNotIn("active_proposal", s.payload)

    def test_profile_slice_excludes_match_and_calendar(self):
        s = slice_for_agent("profile", self.turn, prior_observations=[])
        self.assertEqual(s.agent, "profile")
        self.assertIn("recent_context", s.payload)
        self.assertIn("relevant_memories", s.payload)
        self.assertNotIn("active_proposal", s.payload)
        self.assertNotIn("user_location", s.payload)

    def test_synthesizer_slice_contains_all_observations_and_user_preferences(self):
        prior = [{"task_id": "t1", "tool": "calendar.list_my_events", "result": {"events": []}}]
        s = slice_for_agent("synthesizer", self.turn, prior_observations=prior)
        self.assertEqual(s.agent, "synthesizer")
        self.assertIn("observations", s.payload)
        self.assertEqual(s.payload["observations"], prior)
        self.assertIn("recent_messages", s.payload)
        self.assertIn("user_preferences", s.payload)
        self.assertEqual(s.payload["user_preferences"], ["喜歡戶外活動"])

    def test_prior_observations_injected_into_dependent_places_slice(self):
        prior = [{"task_id": "t2", "tool": "places.search_nearby", "result": {"places": [{"name": "陽明山國家公園"}]}}]
        s = slice_for_agent("places", self.turn, prior_observations=prior)
        self.assertIn("prior_observations", s.payload)
        self.assertEqual(s.payload["prior_observations"], prior)

    def test_calendar_write_slice_carries_find_candidates(self):
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
