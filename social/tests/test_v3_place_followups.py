import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.ayue_agent.v3.calendar_commands import CalendarCommand
from services.ayue_agent.v3.place_followups import (
    PlaceFollowupPersistenceError,
    abandonment_requested,
    clear_followup,
    clear_runtime_state,
    get_followup,
    merge_command,
    public_projection,
    save_followup,
)


class V3PlaceFollowupTests(unittest.TestCase):
    def setUp(self):
        clear_runtime_state()
        self.collection = patch(
            "services.ayue_agent.v3.place_followups._collection",
            return_value=None,
        )
        self.collection.start()

    def tearDown(self):
        self.collection.stop()
        clear_runtime_state()

    def _command(self, *, date="2026-08-26", start="08:30", end="10:00"):
        return CalendarCommand(
            action="create", date=date, start_time=start, end_time=end,
        )

    def test_followups_are_isolated_by_user_and_room(self):
        save_followup(
            "owner", "room-a", self._command(date="2026-08-26"),
            missing_fields=["title"],
        )
        save_followup(
            "owner", "room-b", self._command(date="2026-08-27"),
            missing_fields=["title"],
        )

        self.assertEqual(get_followup("owner", "room-a")["command"]["date"], "2026-08-26")
        self.assertEqual(get_followup("owner", "room-b")["command"]["date"], "2026-08-27")
        clear_followup("owner", "room-a")
        self.assertIsNone(get_followup("owner", "room-a"))
        self.assertIsNotNone(get_followup("owner", "room-b"))

    def test_followup_merge_keeps_saved_time_when_place_is_added_later(self):
        record = save_followup(
            "owner", "room", self._command(), missing_fields=["title"],
        )
        later = CalendarCommand(action="create", title="第二間", draft_mode="none")
        merged = merge_command(later, record)
        self.assertEqual(merged.title, "第二間")
        self.assertEqual(merged.date, "2026-08-26")
        self.assertEqual(merged.start_time, "08:30")
        self.assertEqual(merged.end_time, "10:00")
        self.assertEqual(merged.draft_mode, "continue")

    def test_followup_has_no_short_cache_expiry(self):
        with patch("services.ayue_agent.v3.place_followups.time.time", return_value=100.0):
            save_followup("owner", "room", self._command(), missing_fields=["title"])
        with patch("services.ayue_agent.v3.place_followups.time.time", return_value=3_600.0):
            record = get_followup("owner", "room")
        self.assertIsNotNone(record)
        self.assertNotIn("expires_at", record)

    def test_abandonment_requires_explicit_draft_cancellation_language(self):
        for message in (
            "算了不用，但給我他的詳細資料",
            "不用再幫我排了",
            "取消這次安排",
        ):
            with self.subTest(message=message):
                self.assertTrue(abandonment_requested(message))
        for message in ("不用排隊的店", "這間店不用預約", "幫我查詳細資料"):
            with self.subTest(message=message):
                self.assertFalse(abandonment_requested(message))

    def test_existing_resolved_place_cannot_be_replaced_by_model_title(self):
        record = save_followup(
            "owner", "room", self._command(),
            resolution={
                "status": "resolved",
                "reference": "place_ref_0123456789abcdef01234567",
                "label": "可信店家",
            },
        )
        later = CalendarCommand(action="create", title="另一間店", start_time="09:00")
        merged = merge_command(later, record)
        self.assertEqual(merged.title, "可信店家")
        self.assertEqual(merged.location, "可信店家")

    def test_partial_update_preserves_trusted_place_and_origin(self):
        trusted = {
            "status": "resolved",
            "reference": "place_ref_0123456789abcdef01234567",
            "ordinal": 2,
            "label": "可信店家",
            "origin_run_id": "presentation-run",
        }
        save_followup("owner", "room", self._command(), resolution=trusted)
        updated = save_followup(
            "owner", "room", self._command(date="2026-08-27", start="09:00", end="10:00"),
            missing_fields=["title"], resolution=None,
        )

        self.assertEqual(updated["resolved_place"]["reference"], trusted["reference"])
        self.assertEqual(updated["resolved_place"]["label"], "可信店家")
        self.assertEqual(updated["resolved_place"]["origin_run_id"], "presentation-run")
        self.assertEqual(
            get_followup("owner", "room")["resolved_place"]["origin_run_id"],
            "presentation-run",
        )

    def test_mongo_partial_update_does_not_clear_trusted_place(self):
        class FakeCollection:
            def __init__(self):
                self.record = None

            def find_one(self, _query):
                return self.record

            def update_one(self, _query, update, upsert=False):
                if self.record is None:
                    self.record = {}
                self.record.update(update.get("$set", {}))
                return SimpleNamespace(matched_count=1)

        fake = FakeCollection()
        trusted = {
            "status": "resolved",
            "reference": "place_ref_0123456789abcdef01234567",
            "label": "可信店家",
            "origin_run_id": "presentation-run",
        }
        with patch.dict("os.environ", {"AYUE_TEST_MODE": "off"}, clear=False), patch(
            "services.ayue_agent.v3.place_followups._collection",
            return_value=fake,
        ):
            save_followup("owner", "room", self._command(), resolution=trusted)
            save_followup("owner", "room", self._command(date="2026-08-27"), resolution=None)

        self.assertEqual(fake.record["resolved_place"]["label"], "可信店家")
        self.assertEqual(fake.record["resolved_place"]["origin_run_id"], "presentation-run")

    def test_public_projection_keeps_only_safe_fields(self):
        record = save_followup(
            "owner", "room", self._command(),
            missing_fields=["title"],
            resolution={
                "status": "resolved",
                "reference": "place_ref_0123456789abcdef01234567",
                "ordinal": 2,
                "label": "第二間店",
                "provider_place_id": "secret-provider-id",
            },
        )
        projection = public_projection(record)
        self.assertEqual(projection["resolved_place"]["label"], "第二間店")
        self.assertNotIn("provider_place_id", str(projection))
        self.assertNotIn("expires_at", str(projection))

    def test_production_never_silently_falls_back_to_process_memory(self):
        with patch.dict("os.environ", {"AYUE_TEST_MODE": "off"}, clear=False), patch(
            "services.ayue_agent.v3.place_followups._collection",
            return_value=None,
        ):
            with self.assertRaises(PlaceFollowupPersistenceError):
                save_followup(
                    "owner", "room", self._command(), missing_fields=["title"],
                )


if __name__ == "__main__":
    unittest.main()
