import unittest
from unittest.mock import MagicMock, patch

from services.ayue_agent.contracts import AgentTurnContext
from services.ayue_agent.v3.write_executors import execute_write, prepare_write_confirmation


class V3WriteExecutorsTests(unittest.TestCase):
    def _ctx(self):
        return AgentTurnContext(user_id="owner", room_id="room", message="確認")

    def test_start_search_queues_job(self):
        ctx = self._ctx()
        turn = MagicMock()
        with patch("services.ayue_agent.v3.write_executors.start_match_search",
                   return_value={"status": "queued"}) as start, \
             patch("services.ayue_agent.v3.write_executors.TOOL_CALLS.find_one_and_update",
                   return_value=None), \
             patch("services.ayue_agent.v3.write_executors.TOOL_CALLS.update_one"):
            ok, reply, code = execute_write(
                "match.start_search", {}, ctx, turn, "run1", 0,
                confirmation_id="conf1",
            )
        self.assertTrue(ok)
        self.assertIn("1–3 分鐘", reply)
        start.assert_called_once()
        self.assertEqual(start.call_args.kwargs["idempotency_key"], "confirmation:conf1")

    def test_start_search_idempotent_replay(self):
        ctx = self._ctx()
        turn = MagicMock()
        with patch("services.ayue_agent.v3.write_executors.start_match_search") as start, \
             patch("services.ayue_agent.v3.write_executors.TOOL_CALLS.find_one_and_update",
                   return_value={"result": {"reply": "我已經處理過這次搜尋。"}}):
            ok, reply, code = execute_write(
                "match.start_search", {}, ctx, turn, "run1", 0,
                confirmation_id="conf1",
            )
        self.assertTrue(ok)
        self.assertEqual(reply, "我已經處理過這次搜尋。")
        start.assert_not_called()

    def test_decide_active_proposal_uses_revision_cas(self):
        ctx = self._ctx()
        turn = MagicMock()
        turn.active_proposal = {"user_can_decide": True, "proposal_revision": 2}
        with patch("services.ayue_agent.v3.write_executors.decide_active_proposal",
                   return_value={"status": "success"}) as decide:
            ok, reply, code = execute_write(
                "match.decide_active_proposal", {"decision": "interested"},
                ctx, turn, "run1", 0,
                payload={"proposal_revision": 2},
            )
        self.assertTrue(ok)
        self.assertEqual(decide.call_args.kwargs["expected_revision"], 2)

    def test_decide_active_proposal_not_actionable(self):
        ctx = self._ctx()
        turn = MagicMock()
        turn.active_proposal = {"user_can_decide": False}
        ok, reply, code = execute_write(
            "match.decide_active_proposal", {"decision": "interested"},
            ctx, turn, "run1", 0,
        )
        self.assertFalse(ok)
        self.assertEqual(code, "decision_not_actionable")

    def test_decide_active_proposal_stale_reports_latest(self):
        ctx = self._ctx()
        turn = MagicMock()
        turn.active_proposal = {"user_can_decide": True, "proposal_revision": 1}
        with patch("services.ayue_agent.v3.write_executors.decide_active_proposal",
                   return_value={"stale": True, "current_status": "accepted"}):
            ok, reply, code = execute_write(
                "match.decide_active_proposal", {"decision": "interested"},
                ctx, turn, "run1", 0,
                payload={"proposal_revision": 1},
            )
        self.assertTrue(ok)
        self.assertIn("互相接受", reply)
        self.assertEqual(code, "stale_revision")

    def test_start_assessment_unknown_kind(self):
        ctx = self._ctx()
        ok, reply, code = execute_write(
            "profile.start_assessment", {"kind": "weird"}, ctx, MagicMock(), "run1", 0,
            confirmation_id="c1",
        )
        self.assertFalse(ok)
        self.assertEqual(code, "assessment_unknown_kind")

    def test_start_assessment_ok(self):
        ctx = self._ctx()
        with patch("services.ayue_agent.v3.write_executors.start_assessment_session",
                   return_value={"status": "started", "reply": "我們開始吧"}) as start, \
             patch("services.ayue_agent.v3.write_executors.TOOL_CALLS.find_one_and_update",
                   return_value=None), \
             patch("services.ayue_agent.v3.write_executors.TOOL_CALLS.update_one"):
            ok, reply, code = execute_write(
                "profile.start_assessment", {"kind": "basic"}, ctx, MagicMock(), "run1", 0,
                confirmation_id="c1",
            )
        self.assertTrue(ok)
        self.assertEqual(start.call_args.args[1], "big_five")

    def test_calendar_create_batch_executes_all_with_indexed_keys(self):
        ctx = self._ctx()
        turn = MagicMock()
        created = []
        def fake_create(user_id, args, *, agent_action_key=None):
            created.append((args.get("title"), agent_action_key))
            return {
                "event_id": "e", "source_type": "personal", "title": args.get("title"),
                "start_at": __import__("datetime").datetime(2026, 8, 20, 10, 0,
                    tzinfo=__import__("datetime").timezone(__import__("datetime").timedelta(hours=8))),
                "end_at": __import__("datetime").datetime(2026, 8, 20, 11, 0,
                    tzinfo=__import__("datetime").timezone(__import__("datetime").timedelta(hours=8))),
                "timezone": "Asia/Taipei", "revision": 1,
            }
        with patch("services.calendar_service.create_personal_event",
                   side_effect=fake_create) as create:
            ok, reply, code = execute_write(
                "calendar.create_my_event", {}, ctx, turn, "run1", 0,
                confirmation_id="batch-c1",
                payload={
                    "batch": [
                        {"tool": "calendar.create_my_event",
                         "arguments": {"title": "牛排", "date": "2026-08-12", "start_time": "18:00", "end_time": "20:00"},
                         "data": {}},
                        {"tool": "calendar.create_my_event",
                         "arguments": {"title": "看醫生", "date": "2026-08-09", "start_time": "08:30", "end_time": "12:05"},
                         "data": {}},
                    ],
                },
            )
        self.assertTrue(ok)
        self.assertIsNone(code)
        self.assertEqual(create.call_count, 2)
        self.assertEqual(created[0][1], "calendar-confirmation:batch-c1:0")
        self.assertEqual(created[1][1], "calendar-confirmation:batch-c1:1")
        self.assertIn("牛排", reply)
        self.assertIn("看醫生", reply)

    def test_calendar_mixed_batch_update_and_cancel(self):
        ctx = self._ctx()
        turn = MagicMock()
        with patch("services.calendar_service.update_personal_event",
                   return_value={
                       "event_id": "e1", "source_type": "personal", "title": "看醫生",
                       "start_at": __import__("datetime").datetime(2026, 8, 10, 0, 30,
                           tzinfo=__import__("datetime").timezone.utc),
                       "end_at": __import__("datetime").datetime(2026, 8, 10, 4, 5,
                           tzinfo=__import__("datetime").timezone.utc),
                       "timezone": "Asia/Taipei", "revision": 2,
                   }) as update, \
             patch("services.calendar_service.cancel_event",
                   return_value={
                       "event_id": "e2", "source_type": "personal", "title": "出國",
                       "start_at": __import__("datetime").datetime(2026, 8, 20, 0, 0,
                           tzinfo=__import__("datetime").timezone.utc),
                       "end_at": __import__("datetime").datetime(2026, 8, 20, 1, 0,
                           tzinfo=__import__("datetime").timezone.utc),
                       "timezone": "Asia/Taipei", "revision": 3, "status": "cancelled",
                   }) as cancel:
            ok, reply, code = execute_write(
                "calendar.update_my_event", {}, ctx, turn, "run1", 0,
                confirmation_id="batch-mix",
                payload={
                    "batch": [
                        {"tool": "calendar.update_my_event",
                         "arguments": {"event_hint": "看醫生", "date": "2026-08-10"},
                         "data": {"event_id": "e1", "event_revision": 1, "event_source_type": "personal"}},
                        {"tool": "calendar.cancel_my_event",
                         "arguments": {"event_hint": "出國"},
                         "data": {"event_id": "e2", "event_revision": 2, "event_source_type": "personal"}},
                    ],
                },
            )
        self.assertTrue(ok)
        self.assertIsNone(code)
        update.assert_called_once()
        self.assertEqual(update.call_args.kwargs["agent_action_key"], "calendar-confirmation:batch-mix:0")
        cancel.assert_called_once()
        self.assertEqual(cancel.call_args.kwargs["agent_action_key"], "calendar-confirmation:batch-mix:1")
        self.assertIn("看醫生", reply)
        self.assertIn("出國", reply)

    def test_unknown_write_tool_fails(self):
        ctx = self._ctx()
        ok, reply, code = execute_write("nope.tool", {}, ctx, MagicMock(), "run1", 0)
        self.assertFalse(ok)
        self.assertEqual(code, "write_executor_not_registered")


class V3WritePreflightTests(unittest.TestCase):
    def _ctx(self):
        return AgentTurnContext(user_id="owner", room_id="room", message="確認")

    def test_start_search_not_ready_returns_error_reply(self):
        ctx = self._ctx()
        turn = MagicMock()
        with patch("services.ayue_agent.v3.write_executors.assess_match_opportunity") as assess:
            assess.return_value = MagicMock(state="not_ready", reason_codes=("profile_basis_insufficient",),
                                            missing_basis=("preferences",))
            payload, reply = prepare_write_confirmation("match.start_search", {}, ctx, turn)
        self.assertIsNone(payload)
        self.assertIn("多了解你的方向", reply)

    def test_start_search_ready_returns_payload(self):
        ctx = self._ctx()
        turn = MagicMock()
        with patch("services.ayue_agent.v3.write_executors.assess_match_opportunity") as assess:
            assess.return_value = MagicMock(state="ready", reason_codes=())
            payload, reply = prepare_write_confirmation("match.start_search", {}, ctx, turn)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["action"], "match.start_search")
        self.assertIn("確認", reply)

    def test_match_decision_preview_binds_proposal_revision(self):
        ctx = self._ctx()
        turn = MagicMock()
        turn.active_proposal = {
            "user_can_decide": True,
            "proposal_revision": 4,
            "counterparty": "小安",
        }
        payload, reply = prepare_write_confirmation(
            "match.decide_active_proposal",
            {"decision": "interested"},
            ctx,
            turn,
        )
        self.assertEqual(payload["data"]["proposal_revision"], 4)
        self.assertIn("小安", reply)

    def test_calendar_create_preview(self):
        ctx = self._ctx()
        turn = MagicMock()
        with patch("services.ayue_agent.v3.write_executors.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.write_executors.normalize_form", return_value={
                 "date": "2026-08-10", "start_time": "19:00", "end_time": "20:00", "title": "晚餐",
             }), \
             patch("services.ayue_agent.v3.write_executors._parse_local_interval",
                   return_value=(MagicMock(), MagicMock(), None)), \
             patch("services.ayue_agent.v3.write_executors.conflicts_for_viewer", return_value=[]):
            payload, reply = prepare_write_confirmation(
                "calendar.create_my_event",
                {"title": "晚餐", "date": "2026-08-10", "start_time": "19:00", "end_time": "20:00"},
                ctx, turn,
            )
        self.assertIsNotNone(payload)
        self.assertIn("晚餐", reply)
        self.assertIn("確認", reply)

    def test_calendar_cancel_not_found_returns_error(self):
        ctx = self._ctx()
        turn = MagicMock()
        with patch("services.ayue_agent.v3.write_executors.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.write_executors.resolve_owned_event",
                   return_value=(None, "not_found")):
            payload, reply = prepare_write_confirmation(
                "calendar.cancel_my_event", {"event_hint": "不存在的行程"}, ctx, turn,
            )
        self.assertIsNone(payload)
        self.assertIn("找不到", reply)

    def test_assessment_unknown_kind_returns_error(self):
        ctx = self._ctx()
        payload, reply = prepare_write_confirmation(
            "profile.start_assessment", {"kind": "weird"}, ctx, MagicMock(),
        )
        self.assertIsNone(payload)
        self.assertIn("探索類型", reply)


if __name__ == "__main__":
    unittest.main()
