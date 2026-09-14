"""Public Pi operation-batch regression tests."""
import unittest

from services.ayue_agent.shared.operation_batches import OperationBatchManager
from services.ayue_agent.shared.test_store import MemoryCollection


class OperationBatchTests(unittest.TestCase):
    def setUp(self):
        self.collection = MemoryCollection()
        self.manager = OperationBatchManager(self.collection)
        self.manager.ensure_indexes()

    def test_pending_lookup_never_restores_a_dag_batch(self):
        self.collection.insert_one({
            "_id": "old", "user_id": "candy", "room_id": "ayue-room",
            "surface": "public_ayue", "source_engine": "dag", "status": "active",
            "updated_at": 1, "items": [],
        })
        status, rows = self.manager.pending_lookup(
            user_id="candy", room_id="ayue-room", surface="public_ayue",
        )
        self.assertEqual(status, "empty")
        self.assertEqual(rows, [])

    def _create(self):
        return self.manager.create_batch(
            user_id="candy",
            room_id="ayue-room",
            surface="public_ayue",
            operations=[
                {
                    "kind": "date_invitation",
                    "request": "幫我約它",
                    "source_excerpt": "幫我約它，然後也在 9/20 新增行事曆",
                },
                {
                    "kind": "calendar_create",
                    "request": "在 9/20 新增我要跟他出去的個人行事曆",
                    "source_excerpt": "然後也在 9/20 新增行事曆，我要跟他出去",
                },
            ],
            source_message="幫我約它，然後也在 9/20 新增行事曆，我要跟他出去",
            source_sent_at="2026-09-14T03:23:51+08:00",
            timezone="Asia/Taipei",
            source_clock={"local_iso": "2026-09-14T03:23:51+08:00"},
            context_messages=[
                {"role": "assistant", "content": "我會先推 kkk。", "sent_at": "2026-09-14T03:22:00+08:00"},
                {"role": "user", "content": "好啊", "sent_at": "2026-09-14T03:22:10+08:00"},
            ],
            origin_run_id="run-1",
        )

    def test_keeps_natural_language_and_no_semantic_reference_mapping(self):
        batch = self._create()
        current = self.manager.current_item(batch)
        self.assertEqual(current["request"], "幫我約它")
        self.assertNotIn("contact_ref", batch)
        self.assertNotIn("event_ref", batch)
        context = self.manager.prompt_context(batch)
        self.assertEqual(context["original_clock"]["local_iso"], "2026-09-14T03:23:51+08:00")
        self.assertEqual(context["remaining_operations"][0]["kind"], "calendar_create")

    def test_confirmation_advances_exactly_one_item(self):
        batch = self._create()
        batch = self.manager.bind_confirmation(batch["_id"], "choice-1")
        self.assertEqual(self.manager.current_item(batch)["status"], "awaiting_confirmation")

        advanced = self.manager.resolve_choice(
            user_id="candy",
            room_id="ayue-room",
            surface="public_ayue",
            choice_id="choice-1",
            outcome="completed",
            result_summary="已送出邀約。",
        )
        self.assertEqual(advanced["current_index"], 1)
        self.assertEqual(advanced["items"][0]["status"], "completed")
        self.assertEqual(advanced["items"][1]["status"], "active")

        duplicate = self.manager.resolve_choice(
            user_id="candy",
            room_id="ayue-room",
            surface="public_ayue",
            choice_id="choice-1",
            outcome="completed",
            result_summary="不可重播",
        )
        self.assertIsNone(duplicate)

    def test_cancel_one_continues_and_cancel_all_stops_remaining(self):
        batch = self._create()
        self.manager.bind_confirmation(batch["_id"], "choice-1")
        advanced = self.manager.resolve_choice(
            user_id="candy", room_id="ayue-room", surface="public_ayue",
            choice_id="choice-1", outcome="cancelled", result_summary="取消邀約。",
        )
        self.assertEqual(advanced["current_index"], 1)
        self.assertEqual(advanced["items"][0]["status"], "cancelled")
        stopped = self.manager.cancel_all(batch["_id"])
        self.assertEqual(stopped["status"], "cancelled")
        self.assertEqual(stopped["items"][1]["status"], "cancelled")

    def test_new_batch_pauses_old_and_rooms_are_isolated(self):
        first = self._create()
        second = self._create()
        self.assertEqual(self.manager.get(first["_id"])["status"], "paused")
        self.assertEqual(self.manager.latest_pending(
            user_id="candy", room_id="ayue-room", surface="public_ayue",
        )["_id"], second["_id"])
        self.assertIsNone(self.manager.latest_pending(
            user_id="candy", room_id="other-room", surface="public_ayue",
        ))

    def test_correction_is_preserved_as_natural_language(self):
        batch = self._create()
        updated = self.manager.append_correction(
            batch["_id"], "改成邀請 kkk", sent_at="2026-09-14T03:25:00+08:00",
        )
        item = self.manager.current_item(updated)
        self.assertEqual(item["corrections"][0]["content"], "改成邀請 kkk")
        self.assertEqual(item["request"], "幫我約它")


if __name__ == "__main__":
    unittest.main()
