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

    def test_start_assessment_uses_confirmation_id_from_manager_payload(self):
        ctx = self._ctx()
        with patch("services.ayue_agent.v3.write_executors.start_assessment_session",
                   return_value={"status": "started", "reply": "第一題來囉"}) as start, \
             patch("services.ayue_agent.v3.write_executors.TOOL_CALLS.find_one_and_update",
                   return_value=None) as claim, \
             patch("services.ayue_agent.v3.write_executors.TOOL_CALLS.update_one"):
            ok, reply, code = execute_write(
                "profile.start_assessment", {"kind": "basic"}, ctx, MagicMock(), "confirm-run", 0,
                payload={"_confirmation_id": "assessment-confirmation-42"},
            )

        self.assertTrue(ok)
        self.assertEqual(reply, "第一題來囉")
        expected_key = "confirmation:assessment-confirmation-42:big_five"
        self.assertEqual(claim.call_args.args[0]["idempotency_key"], expected_key)
        self.assertEqual(start.call_args.kwargs["idempotency_key"], expected_key)

    def test_calendar_legacy_tool_fails_closed_without_domain_write(self):
        ctx = self._ctx()
        with patch("services.calendar_service.create_personal_event") as create, \
             patch("services.calendar_service.update_personal_event") as update, \
             patch("services.calendar_service.cancel_event") as cancel:
            ok, reply, code = execute_write(
                "calendar.cancel_my_event", {"event_hint": "出國"}, ctx, MagicMock(), "run1", 0,
                confirmation_id="legacy-c1",
                payload={"batch": [{"tool": "calendar.cancel_my_event", "data": {}}]},
            )
        self.assertFalse(ok)
        self.assertEqual(code, "calendar_legacy_tool_disabled")
        create.assert_not_called()
        update.assert_not_called()
        cancel.assert_not_called()

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

    def test_assessment_unknown_kind_returns_error(self):
        ctx = self._ctx()
        payload, reply = prepare_write_confirmation(
            "profile.start_assessment", {"kind": "weird"}, ctx, MagicMock(),
        )
        self.assertIsNone(payload)
        self.assertIn("探索類型", reply)


if __name__ == "__main__":
    unittest.main()
