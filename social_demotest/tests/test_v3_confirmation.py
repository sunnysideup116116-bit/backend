# social_demotest/tests/test_v3_confirmation.py
import unittest
from unittest.mock import MagicMock, patch

from services.ayue_agent.v3.confirmation import ConfirmationManager


class V3ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.coll = MagicMock()
        self.mgr = ConfirmationManager(self.coll)

    def test_create_confirmation_stores_pending_with_ttl(self):
        self.mgr.create_confirmation(
            user_id="owner", agent_name="calendar",
            tool_name="calendar.cancel_my_event",
            arguments={"event_hint": "某個事件"},
            ttl_seconds=900,
        )
        self.coll.insert_one.assert_called_once()
        doc = self.coll.insert_one.call_args[0][0]
        self.assertEqual(doc["user_id"], "owner")
        self.assertEqual(doc["tool_name"], "calendar.cancel_my_event")
        self.assertEqual(doc["status"], "pending")
        self.assertEqual(doc["agent_name"], "calendar")

    def test_list_active_returns_only_pending_unexpired(self):
        self.coll.find.return_value = [
            {"_id": "c1", "user_id": "owner", "tool_name": "calendar.cancel_my_event",
             "arguments": {"event_hint": "某個事件"}, "status": "pending", "agent_name": "calendar",
             "expires_at": 9999999999.0},
            {"_id": "c2", "user_id": "owner", "tool_name": "match.start_search",
             "arguments": {}, "status": "pending", "agent_name": "match",
             "expires_at": 9999999999.0},
        ]
        actives = self.mgr.list_active(user_id="owner")
        self.assertEqual(len(actives), 2)

    def test_cancel_all_marks_confirmations_cancelled(self):
        self.coll.update_many.return_value = MagicMock(modified_count=2)
        self.mgr.cancel_all(user_id="owner")
        self.coll.update_many.assert_called_once()

    def test_execute_confirmed_runs_only_oldest_active_confirmation(self):
        # One confirm reply maps to exactly one side effect: only the oldest
        # pending confirmation executes; siblings stay pending for the next
        # confirm/cancel.
        self.coll.find.return_value = [
            {"_id": "c1", "user_id": "owner", "tool_name": "calendar.cancel_my_event",
             "arguments": {"event_hint": "某個事件"}, "status": "pending", "agent_name": "calendar",
             "payload": {}, "created_at": 1.0, "expires_at": 1e18},
            {"_id": "c2", "user_id": "owner", "tool_name": "calendar.create_my_event",
             "arguments": {"title": "新行程"}, "status": "pending", "agent_name": "calendar",
             "payload": {}, "created_at": 2.0, "expires_at": 1e18},
        ]
        # claim c1 succeeds, final status write succeeds; c2 never claimed
        self.coll.update_one.side_effect = [
            MagicMock(modified_count=1),
            MagicMock(modified_count=1),
        ]
        executed = []

        def fake_executor(tool_name, arguments, user_id, payload=None):
            executed.append(tool_name)
            return MagicMock(ok=True, data={"cancelled": True}, error_code=None)

        results = self.mgr.execute_confirmed(user_id="owner", executor=fake_executor)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[0]["confirmation_id"], "c1")
        self.assertEqual(executed, ["calendar.cancel_my_event"])
        # c2 was never claimed: no second claim update and no final write
        self.assertEqual(self.coll.update_one.call_count, 2)

    def test_execute_confirmed_oldest_first_even_when_insert_order_differs(self):
        # Sorting is by created_at, not by document order.
        self.coll.find.return_value = [
            {"_id": "c2", "user_id": "owner", "tool_name": "calendar.cancel_my_event",
             "arguments": {"event_hint": "x"}, "status": "pending", "agent_name": "calendar",
             "payload": {}, "created_at": 9.0, "expires_at": 1e18},
            {"_id": "c1", "user_id": "owner", "tool_name": "calendar.create_my_event",
             "arguments": {"title": "新行程"}, "status": "pending", "agent_name": "calendar",
             "payload": {}, "created_at": 1.0, "expires_at": 1e18},
        ]
        self.coll.update_one.side_effect = [
            MagicMock(modified_count=1),
            MagicMock(modified_count=1),
        ]
        executed = []

        def fake_executor(tool_name, arguments, user_id, payload=None):
            executed.append(tool_name)
            return MagicMock(ok=True, data={}, error_code=None)

        results = self.mgr.execute_confirmed(user_id="owner", executor=fake_executor)
        self.assertEqual(results[0]["confirmation_id"], "c1")
        self.assertEqual(executed, ["calendar.create_my_event"])

    def test_execute_confirmed_returns_empty_when_claim_lost(self):
        self.coll.find.return_value = [
            {"_id": "c1", "user_id": "owner", "tool_name": "calendar.cancel_my_event",
             "arguments": {}, "status": "pending", "agent_name": "calendar",
             "payload": {}, "created_at": 1.0, "expires_at": 1e18},
        ]
        self.coll.update_one.return_value = MagicMock(modified_count=0)

        def fake_executor(tool_name, arguments, user_id, payload=None):
            raise AssertionError("executor must not run when the claim is lost")

        results = self.mgr.execute_confirmed(user_id="owner", executor=fake_executor)
        self.assertEqual(results, [])

    def test_tuple_result_is_ok(self):
        # write_executors returns (ok, reply, error_code); a tuple must be
        # treated as a success and its reply surfaced.
        self.coll.find.return_value = [
            {"_id": "c1", "user_id": "owner", "tool_name": "calendar.create_my_event",
             "arguments": {"title": "牛排"}, "status": "pending", "agent_name": "calendar",
             "payload": {}, "created_at": 1.0, "expires_at": 1e18},
        ]
        self.coll.update_one.side_effect = [
            MagicMock(modified_count=1),  # claim
            MagicMock(modified_count=1),  # final status write
        ]

        def fake_executor(tool_name, arguments, user_id, payload=None):
            return (True, "已新增行程：牛排。", None)

        results = self.mgr.execute_confirmed(user_id="owner", executor=fake_executor)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[0]["data"].get("reply"), "已新增行程：牛排。")
        self.assertIsNone(results[0]["error_code"])

    def test_tuple_failure_reported_with_error_code(self):
        self.coll.find.return_value = [
            {"_id": "c1", "user_id": "owner", "tool_name": "calendar.update_my_event",
             "arguments": {}, "status": "pending", "agent_name": "calendar",
             "payload": {}, "created_at": 1.0, "expires_at": 1e18},
        ]
        self.coll.update_one.side_effect = [
            MagicMock(modified_count=1),
            MagicMock(modified_count=1),
        ]

        def fake_executor(tool_name, arguments, user_id, payload=None):
            return (False, "這筆行程剛剛有變動", "stale_revision")

        results = self.mgr.execute_confirmed(user_id="owner", executor=fake_executor)
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["error_code"], "stale_revision")

    def test_create_confirmations_merge_across_records(self):
        # Two separate create confirmations (planner split them into two
        # sub-tasks) must be executed as ONE batch with one confirm reply.
        self.coll.find.return_value = [
            {"_id": "c1", "user_id": "owner", "tool_name": "calendar.create_my_event",
             "arguments": {"title": "吃牛排", "date": "2026-08-12"}, "status": "pending",
             "agent_name": "calendar", "payload": {}, "batch": [],
             "created_at": 1.0, "expires_at": 1e18},
            {"_id": "c2", "user_id": "owner", "tool_name": "calendar.create_my_event",
             "arguments": {"title": "看醫生", "date": "2026-08-09"}, "status": "pending",
             "agent_name": "calendar", "payload": {}, "batch": [],
             "created_at": 2.0, "expires_at": 1e18},
        ]
        self.coll.update_one.side_effect = [
            MagicMock(modified_count=1),  # claim c1
            MagicMock(modified_count=1),  # final status write
        ]
        self.coll.update_many.return_value = MagicMock(modified_count=1)
        seen = {}

        def fake_executor(tool_name, arguments, user_id, payload=None):
            seen["payload"] = payload
            return (True, "已新增行程：兩筆。", None)

        results = self.mgr.execute_confirmed(user_id="owner", executor=fake_executor)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        # batch merges c1's item first, then the sibling's item (new format)
        self.assertEqual(seen["payload"]["batch"], [
            {"tool": "calendar.create_my_event",
             "arguments": {"title": "吃牛排", "date": "2026-08-12"}, "data": {}},
            {"tool": "calendar.create_my_event",
             "arguments": {"title": "看醫生", "date": "2026-08-09"}, "data": {}},
        ])
        self.coll.update_many.assert_called_once()
        merged_ids = self.coll.update_many.call_args[0][0]
        self.assertEqual(merged_ids["_id"]["$in"], ["c2"])

    def test_stale_confirmation_does_not_overwrite_terminal_state(self):
        self.coll.find.return_value = [
            {"_id": "c1", "user_id": "owner", "tool_name": "calendar.cancel_my_event",
             "arguments": {"event_hint": "某個事件"}, "status": "pending", "agent_name": "calendar",
             "payload": {}, "created_at": 1.0, "expires_at": 1e18},
        ]
        self.coll.update_one.side_effect = [
            MagicMock(modified_count=1),  # claim succeeds
            MagicMock(modified_count=0),  # final CAS miss (state changed)
        ]

        def stale_executor(tool_name, arguments, user_id, payload=None):
            return MagicMock(ok=False, data={}, error_code="stale_revision")

        results = self.mgr.execute_confirmed(user_id="owner", executor=stale_executor)
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["error_code"], "stale_revision")


if __name__ == "__main__":
    unittest.main()