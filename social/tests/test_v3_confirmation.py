import unittest
from unittest.mock import MagicMock, patch

from services.ayue_agent.v3.confirmation import ConfirmationManager


class V3ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.coll = MagicMock()
        self.mgr = ConfirmationManager(self.coll)

    def _record(self, *, confirmation_id="c1", created_at=1.0, tool_name="calendar.submit_commands"):
        arguments = {}
        payload = {"calendar_plan_version": 1, "plans": []}
        origin_run_id = "run-preview"
        return {
            "_id": confirmation_id,
            "user_id": "owner",
            "tool_name": tool_name,
            "arguments": arguments,
            "payload": payload,
            "status": "pending",
            "agent_name": "calendar",
            "created_at": created_at,
            "expires_at": 1e18,
            "origin_run_id": origin_run_id,
            "request_fingerprint": self.mgr._request_fingerprint(
                tool_name=tool_name,
                arguments=arguments,
                payload=payload,
                origin_run_id=origin_run_id,
            ),
            "preview_fingerprint": "preview-digest",
            "presented_message_id": "message-1",
            "presented_at": created_at + 0.5,
        }

    @patch("services.ayue_agent.v3.confirmation.time.time", return_value=100.0)
    def test_create_confirmation_supersedes_old_pending_and_binds_preview(self, _now):
        self.mgr.create_confirmation(
            user_id="owner",
            agent_name="calendar",
            tool_name="calendar.submit_commands",
            arguments={},
            payload={"calendar_plan_version": 1, "plans": []},
            ttl_seconds=900,
            origin_run_id="run-preview",
            preview="Cancel tomorrow's event?",
        )
        self.coll.update_many.assert_called_once_with(
            {"user_id": "owner", "status": {"$in": ["prepared", "pending"]}},
            {"$set": {"status": "superseded", "superseded_at": 100.0}},
        )
        doc = self.coll.insert_one.call_args[0][0]
        self.assertEqual(doc["status"], "prepared")
        self.assertEqual(doc["origin_run_id"], "run-preview")
        self.assertTrue(doc["request_fingerprint"])
        self.assertFalse(doc["preview_fingerprint"])
        self.assertTrue(doc["preview_source_fingerprint"])
        self.assertNotIn("preview", doc)

    @patch("services.ayue_agent.v3.confirmation.time.time", return_value=100.0)
    def test_planner_projection_contains_no_ids_arguments_or_payload(self, _now):
        self.coll.find.return_value = [self._record()]
        projection = self.mgr.planner_projection(user_id="owner")
        self.assertEqual(projection[0]["domain"], "calendar")
        self.assertLessEqual(projection[0]["expires_in_seconds"], 900)
        text = repr(projection)
        for forbidden in ("confirmation_id", "event_revision", "arguments", "payload", "run-preview"):
            self.assertNotIn(forbidden, text)

    def test_execute_confirmed_runs_newest_bound_confirmation_only(self):
        older = self._record(confirmation_id="old", created_at=1.0)
        newer = self._record(
            confirmation_id="new",
            created_at=2.0,
            tool_name="calendar.submit_commands",
        )
        self.coll.find.return_value = [older, newer]
        self.coll.update_one.side_effect = [
            MagicMock(modified_count=1),
            MagicMock(modified_count=1),
        ]
        executed = []

        def executor(tool_name, arguments, user_id, payload=None):
            executed.append((tool_name, payload["_confirmation_id"]))
            return (True, "done", None)

        results = self.mgr.execute_confirmed(user_id="owner", executor=executor)
        self.assertEqual(executed, [("calendar.submit_commands", "new")])
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[0]["confirmation_id"], "new")

    def test_unbound_legacy_confirmation_is_rejected_without_execution(self):
        record = self._record()
        record.pop("origin_run_id")
        record.pop("request_fingerprint")
        self.coll.find.return_value = [record]
        executor = MagicMock()

        results = self.mgr.execute_confirmed(user_id="owner", executor=executor)

        executor.assert_not_called()
        self.assertEqual(results[0]["error_code"], "confirmation_unbound")

    def test_tampered_confirmation_is_rejected_without_execution(self):
        record = self._record()
        record["payload"]["event_revision"] = 99
        self.coll.find.return_value = [record]
        executor = MagicMock()

        results = self.mgr.execute_confirmed(user_id="owner", executor=executor)

        executor.assert_not_called()
        self.assertEqual(results[0]["error_code"], "confirmation_unbound")

    def test_execute_confirmed_returns_empty_when_claim_lost(self):
        self.coll.find.return_value = [self._record()]
        self.coll.update_one.return_value = MagicMock(modified_count=0)
        executor = MagicMock()

        results = self.mgr.execute_confirmed(user_id="owner", executor=executor)

        self.assertEqual(results, [])
        executor.assert_not_called()

    @patch("services.ayue_agent.v3.confirmation.time.time", return_value=100.0)
    def test_calendar_confirmation_stays_prepared_until_persisted_preview_is_marked(self, _now):
        self.coll.update_one.return_value = MagicMock(modified_count=1)
        self.mgr.create_confirmation(
            user_id="owner", agent_name="calendar", tool_name="calendar.submit_commands",
            arguments={}, payload={"plans": []}, origin_run_id="run-preview",
            preview="要新增行程嗎？",
        )
        self.coll.find.return_value = []
        self.assertEqual(self.mgr.list_active(user_id="owner"), [])
        executed = []
        self.assertEqual(
            self.mgr.execute_confirmed(
                user_id="owner",
                executor=lambda *args: executed.append(args) or (True, "done", None),
            ),
            [],
        )
        self.assertEqual(executed, [])
        self.assertTrue(self.mgr.bind_final_preview(
            user_id="owner", origin_run_id="run-preview", final_content="要新增行程嗎？",
        ))
        self.assertTrue(self.mgr.mark_presented(
            user_id="owner", origin_run_id="run-preview", message_id="message-1",
            persisted_content="要新增行程嗎？",
        ))

    def test_non_calendar_confirmation_keeps_existing_pending_lifecycle(self):
        from services.ayue_agent.v3.test_store import MemoryCollection

        manager = ConfirmationManager(MemoryCollection())
        manager.create_confirmation(
            user_id="owner", agent_name="relationship",
            tool_name="relationship.start_date_coordination",
            arguments={}, payload={}, origin_run_id="run-relationship",
            preview="要建立約會邀請嗎？",
        )
        active = manager.list_active(user_id="owner")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["status"], "pending")

    def test_preview_fingerprint_is_bound_to_exact_persisted_content(self):
        from services.ayue_agent.v3.test_store import MemoryCollection

        manager = ConfirmationManager(MemoryCollection())
        manager.create_confirmation(
            user_id="owner", agent_name="calendar", tool_name="calendar.submit_commands",
            arguments={}, payload={"plans": []}, origin_run_id="run-preview",
            preview="原始預覽",
        )
        self.assertTrue(manager.bind_final_preview(
            user_id="owner", origin_run_id="run-preview", final_content="最終預覽",
        ))
        self.assertFalse(manager.mark_presented(
            user_id="owner", origin_run_id="run-preview", message_id="message-1",
            persisted_content="原始預覽",
        ))
        self.assertTrue(manager.mark_presented(
            user_id="owner", origin_run_id="run-preview", message_id="message-1",
            persisted_content="最終預覽",
        ))

    def test_supersede_and_confirm_race_never_cancels_executing_mutation(self):
        from services.ayue_agent.v3.test_store import MemoryCollection

        manager = ConfirmationManager(MemoryCollection())
        manager.create_confirmation(
            user_id="owner", agent_name="calendar", tool_name="calendar.submit_commands",
            arguments={}, payload={"plans": []}, origin_run_id="run-preview",
            preview="預覽",
        )
        manager.bind_final_preview(
            user_id="owner", origin_run_id="run-preview", final_content="預覽",
        )
        manager.mark_presented(
            user_id="owner", origin_run_id="run-preview", message_id="message-1",
            persisted_content="預覽",
        )
        record = manager.list_active(user_id="owner")[0]
        manager._coll.update_one(
            {"_id": record["_id"], "status": "pending"},
            {"$set": {"status": "executing"}},
        )
        result = manager.supersede_active(user_id="owner", tool_name="calendar.submit_commands")
        self.assertEqual(result["status"], "already_executing")
        self.assertEqual(manager._coll.find({"_id": record["_id"]})[0]["status"], "executing")

    def test_tuple_failure_preserves_stale_revision_code(self):
        self.coll.find.return_value = [self._record()]
        self.coll.update_one.side_effect = [
            MagicMock(modified_count=1),
            MagicMock(modified_count=0),
        ]

        results = self.mgr.execute_confirmed(
            user_id="owner",
            executor=lambda *_args, **_kwargs: (False, "stale", "stale_revision"),
        )

        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["error_code"], "stale_revision")


if __name__ == "__main__":
    unittest.main()
