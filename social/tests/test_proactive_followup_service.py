import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from unittest.mock import Mock, patch

from bson.objectid import ObjectId

from services.message_use_service import metadata_for_use
from services.proactive_followup_service import (
    FOLLOWUP_MAX_CANDIDATES,
    apply_followup_proposal,
    claim_delivery_slot,
    claim_followup_candidate,
    commit_delivery_slot,
    followup_candidate_snapshot,
    followup_delivery_claim_is_current,
    ensure_indexes,
    is_proactive_care_enabled,
    mark_followup_asked,
    next_quiet_end,
    owner_busy_until,
    stage_delivery_slot,
    _schedule_window,
    unanswered_followup_until,
    validate_followup_proposal,
)


class ProactiveFollowupServiceTests(unittest.TestCase):
    def test_validation_requires_owner_evidence_and_rejects_calendar_text(self):
        recent = {"message_kind": "real_world_update"}
        valid = {
            "action": "create", "topic": "週末看展", "question_goal": "確認感覺",
            "timing": "after_days", "wait_days": 3,
            "evidence_span": "週末想去看展", "confidence": .95,
            "subject": "owner", "reason_code": "test",
        }
        accepted = validate_followup_proposal(valid, "週末想去看展", recent)
        self.assertEqual(accepted["action"], "create")
        self.assertEqual(accepted["subject"], "owner")
        self.assertEqual(
            validate_followup_proposal(accepted, "週末想去看展", recent)["action"],
            "create",
        )
        rejected = dict(valid, question_goal="查看行事曆")
        self.assertIsNone(validate_followup_proposal(rejected, "週末想去看展", recent))
        self.assertIsNone(validate_followup_proposal(dict(valid, evidence_span="不存在"), "週末想去看展", recent))

    def test_legacy_setting_fails_closed_only_for_known_values(self):
        self.assertTrue(is_proactive_care_enabled({}))
        self.assertTrue(is_proactive_care_enabled({"proactive_frequency": "normal"}))
        self.assertFalse(is_proactive_care_enabled({"proactive_frequency": "unexpected"}))
        self.assertFalse(is_proactive_care_enabled({"proactive_care_enabled": False, "proactive_frequency": "3600"}))

    def test_validated_proposal_survives_the_persistence_boundary(self):
        owner_message = "週末想去看展"
        proposal = validate_followup_proposal(
            {
                "action": "create", "topic": "週末看展",
                "question_goal": "確認看展後感覺", "timing": "after_days",
                "wait_days": 3, "evidence_span": owner_message,
                "confidence": .95, "subject": "owner",
            },
            owner_message,
            {"message_kind": "real_world_update"},
        )
        with patch("services.proactive_followup_service.is_owned_public_ai_room", return_value=True), \
             patch("services.proactive_followup_service._source_is_reusable", return_value=(True, {"content": owner_message})), \
             patch("services.proactive_followup_service.expire_stale_followups"), \
             patch("services.proactive_followup_service.list_followup_candidates", return_value=[]), \
             patch("services.proactive_followup_service.PROACTIVE_FOLLOWUPS.insert_one") as insert:
            result = apply_followup_proposal(
                "owner", "room", str(ObjectId()), owner_message, 100,
                proposal, {"message_kind": "real_world_update"},
                now=101, mode="shadow",
            )
        self.assertEqual(result["status"], "created")
        self.assertEqual(insert.call_args.args[0]["active_slot"], 1)

    def test_quiet_window_uses_taipei_clock(self):
        # 2026-09-05 23:00 Taipei.
        night = datetime(2026, 9, 5, 15, 0, tzinfo=timezone.utc).timestamp()
        end = next_quiet_end(night)
        self.assertEqual(datetime.fromtimestamp(end, timezone.utc).hour, 1)
        daytime = datetime(2026, 9, 5, 3, 0, tzinfo=timezone.utc).timestamp()
        self.assertIsNone(next_quiet_end(daytime))

    def test_calendar_projection_only_returns_busy_end(self):
        end = datetime(2026, 9, 6, 13, 0, tzinfo=timezone.utc)
        with patch(
            "services.proactive_followup_service.calendar_events_coll.find_one",
            return_value={"end_at": end, "title": "private"},
        ) as find:
            busy_until, available = owner_busy_until("owner", now=end.timestamp() - 1800)
        self.assertTrue(available)
        self.assertEqual(busy_until, end.timestamp())
        self.assertEqual(find.call_args.args[1], {"end_at": 1, "_id": 0})
        self.assertNotIn("title", find.call_args.args[1])

    def test_numeric_date_uses_next_day_window_without_exposing_calendar_details(self):
        source_time = datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc).timestamp()
        available, expires = _schedule_window(
            "9/15 想去看展", source_time,
            {"timing": "近期"},
            {"wait_days": 3},
            now=source_time,
        )
        self.assertEqual(datetime.fromtimestamp(available, timezone.utc).date().isoformat(), "2026-09-16")
        self.assertEqual(expires - available, 3 * 86400)

    def test_specific_weekday_is_not_shifted_by_coarse_week_timing(self):
        source_time = datetime(2026, 9, 7, 4, 0, tzinfo=timezone.utc).timestamp()
        available, _ = _schedule_window(
            "下週五去看展", source_time,
            {"timing": "下週"},
            {"wait_days": 3},
            now=source_time,
        )
        self.assertEqual(
            datetime.fromtimestamp(available, timezone.utc).date().isoformat(),
            "2026-09-19",
        )

    def test_apply_caps_candidates_across_rooms(self):
        proposal = {
            "action": "create", "topic": "新活動", "question_goal": "確認結果",
            "timing": "after_days", "wait_days": 3,
            "evidence_span": "我想去爬山", "confidence": .95,
            "subject": "owner", "reason_code": "test",
        }
        source = {
            "content": "我想去爬山",
            "metadata": {"message_use": metadata_for_use("ordinary")},
        }
        with patch("services.proactive_followup_service.is_owned_public_ai_room", return_value=True), \
             patch("services.proactive_followup_service._source_is_reusable", return_value=(True, source)), \
             patch("services.proactive_followup_service.list_followup_candidates", return_value=[{}, {}, {}]) as listed, \
             patch("services.proactive_followup_service.PROACTIVE_FOLLOWUPS.insert_one") as insert:
            result = apply_followup_proposal(
                "owner", "room", str(ObjectId()), "我想去爬山", 100.0,
                proposal, {"message_kind": "real_world_update"}, now=200.0, mode="shadow",
            )
        self.assertEqual(result["status"], "at_capacity")
        self.assertEqual(listed.call_count, 1)
        insert.assert_not_called()

    def test_snapshot_binding_keeps_update_on_the_candidate_the_model_saw(self):
        shown = [
            {"_id": "candidate-a", "revision": 3, "topic": "看展"},
            {"_id": "candidate-b", "revision": 7, "topic": "爬山"},
        ]
        reordered = list(reversed(shown))
        with patch("services.proactive_followup_service.list_followup_candidates", side_effect=[shown, reordered]), \
             patch("services.proactive_followup_service.is_owned_public_ai_room", return_value=True), \
             patch("services.proactive_followup_service._source_is_reusable", return_value=(True, {"content": "看展取消了"})), \
             patch("services.proactive_followup_service.expire_stale_followups"), \
             patch("services.proactive_followup_service.PROACTIVE_FOLLOWUPS.update_one", return_value=Mock(modified_count=1)) as update:
            snapshot = followup_candidate_snapshot("owner", "room", now=100)
            result = apply_followup_proposal(
                "owner", "room", str(ObjectId()), "看展取消了", 100,
                {
                    "action": "close", "existing_slot": 1,
                    "evidence_span": "看展取消了", "confidence": .95,
                    "subject": "owner",
                },
                {"message_kind": "real_world_update"},
                now=101,
                mode="on",
                candidate_snapshot=snapshot,
            )
        self.assertEqual(result["status"], "updated")
        self.assertEqual(update.call_args.args[0]["_id"], "candidate-a")
        self.assertEqual(update.call_args.args[0]["revision"], 3)

    def test_active_slot_unique_conflict_keeps_concurrent_create_at_three(self):
        occupied = {1, 2}
        lock = Lock()

        def insert(candidate):
            with lock:
                slot = candidate["active_slot"]
                if slot in occupied:
                    from pymongo.errors import DuplicateKeyError
                    raise DuplicateKeyError("slot")
                occupied.add(slot)

        proposal = {
            "action": "create", "topic": "學做菜", "question_goal": "確認進度",
            "timing": "after_days", "wait_days": 3,
            "evidence_span": "學做菜", "confidence": .95, "subject": "owner",
        }
        with patch("services.proactive_followup_service.is_owned_public_ai_room", return_value=True), \
             patch("services.proactive_followup_service._source_is_reusable", return_value=(True, {"content": "學做菜"})), \
             patch("services.proactive_followup_service.expire_stale_followups"), \
             patch("services.proactive_followup_service.list_followup_candidates", return_value=[{}, {}]), \
             patch("services.proactive_followup_service.PROACTIVE_FOLLOWUPS.insert_one", side_effect=insert), \
             patch("services.proactive_followup_service.PROACTIVE_FOLLOWUPS.find_one", return_value=None):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda suffix: apply_followup_proposal(
                        "owner", f"room-{suffix}", f"64b64c8f000000000000000{suffix}",
                        "學做菜", 100, proposal,
                        {"message_kind": "real_world_update"}, now=101, mode="on",
                    ),
                    (1, 2),
                ))
        self.assertEqual(sorted(item["status"] for item in results), ["at_capacity", "created"])
        self.assertEqual(len(occupied), FOLLOWUP_MAX_CANDIDATES)

    def test_index_setup_enforces_one_owner_per_active_slot(self):
        with patch("services.proactive_followup_service.expire_stale_followups"), \
             patch("services.proactive_followup_service._backfill_active_slots"), \
             patch("services.proactive_followup_service.PROACTIVE_FOLLOWUPS.create_index") as create:
            ensure_indexes()
        active_slot_call = next(
            call for call in create.call_args_list
            if call.kwargs.get("name") == "proactive_followup_active_slot"
        )
        self.assertTrue(active_slot_call.kwargs["unique"])
        self.assertEqual(
            active_slot_call.kwargs["partialFilterExpression"],
            {"active_slot": {"$exists": True}},
        )

    def test_expired_processing_lease_can_be_reclaimed(self):
        candidate = {
            "_id": "candidate", "status": "processing",
            "lease_until": 99, "expires_at": 1000,
        }
        with patch(
            "services.proactive_followup_service.PROACTIVE_FOLLOWUPS.find_one_and_update",
            return_value={**candidate, "attempt_count": 2},
        ) as claim:
            result = claim_followup_candidate(candidate, now=100)
        self.assertIsNotNone(result)
        processing_branch = claim.call_args.args[0]["$or"][1]
        self.assertEqual(processing_branch["status"], "processing")
        self.assertEqual(processing_branch["lease_until"], {"$lte": 100})

    def test_unanswered_care_blocks_a_new_delivery_for_seven_days(self):
        now = 1_000_000.0
        profile = {
            "user_id": "owner", "proactive_care_enabled": True,
            "proactive_care_last_sent_at": now - 49 * 3600,
            "proactive_care_delivery_times": [now - 49 * 3600],
        }
        asked_cursor = Mock()
        asked_cursor.sort.return_value.limit.return_value = [
            {"room_id": "room", "last_asked_at": now - 49 * 3600},
        ]
        with patch("services.proactive_followup_service.profiles_coll.find_one", return_value=profile), \
             patch("services.proactive_followup_service.PROACTIVE_FOLLOWUPS.find", return_value=asked_cursor), \
             patch("services.proactive_followup_service.messages_coll.find_one", return_value=None), \
             patch("services.proactive_followup_service.profiles_coll.find_one_and_update") as claim:
            token = claim_delivery_slot("owner", "new-candidate", now=now)
        self.assertIsNone(token)
        claim.assert_not_called()

    def test_delivery_recheck_observes_a_pause_during_generation(self):
        with patch(
            "services.proactive_followup_service.PROACTIVE_FOLLOWUPS.find_one",
            return_value={"_id": "candidate"},
        ), patch(
            "services.proactive_followup_service.profiles_coll.find_one",
            return_value={"proactive_care_enabled": False},
        ):
            current = followup_delivery_claim_is_current(
                "owner", "candidate", "candidate-lease", "delivery-lease",
                now=100,
            )
        self.assertFalse(current)

    def test_delivery_commit_atomically_writes_marker_and_rate_limits(self):
        with patch(
            "services.proactive_followup_service.profiles_coll.update_one",
            return_value=Mock(matched_count=1, modified_count=1),
        ) as update:
            committed = commit_delivery_slot(
                "owner",
                event_key="event-1",
                now=100.0,
                message_id="message-1",
                message="最近還好嗎？",
                origin_room_id="room",
                require_enabled_flag=True,
            )
        self.assertTrue(committed)
        query, change = update.call_args.args
        self.assertTrue(query["proactive_care_enabled"])
        self.assertEqual(
            query["proactive_care_pending_delivery.event_key"], "event-1",
        )
        self.assertNotIn("proactive_care_delivery_claim_id", query)
        self.assertEqual(
            change["$set"]["proactive_care_delivery"]["message_id"],
            "message-1",
        )
        self.assertEqual(change["$push"]["proactive_care_delivery_times"]["$each"], [100.0])
        self.assertEqual(
            change["$push"]["proactive_care_delivery_history"]["$each"][0]["room_id"],
            "room",
        )
        self.assertIn("proactive_care_pending_delivery", change["$unset"])
        self.assertIn("proactive_care_delivery_claim_id", change["$unset"])

    def test_staging_persists_delivery_independently_from_candidate(self):
        with patch(
            "services.proactive_followup_service.profiles_coll.update_one",
            return_value=Mock(matched_count=1, modified_count=1),
        ) as update:
            pending = stage_delivery_slot(
                "owner",
                "delivery-token",
                candidate_id="candidate",
                candidate_token="candidate-token",
                now=100.0,
                event_key="event-1",
                message="最近還好嗎？",
                origin_room_id="room",
                require_enabled_flag=True,
            )
        self.assertEqual(pending["event_key"], "event-1")
        self.assertEqual(
            update.call_args.args[1]["$set"]["proactive_care_pending_delivery"],
            pending,
        )

    def test_mark_asked_records_message_without_owning_recovery(self):
        with patch(
            "services.proactive_followup_service.PROACTIVE_FOLLOWUPS.update_one",
            return_value=Mock(modified_count=1),
        ) as update:
            marked = mark_followup_asked(
                {"_id": "candidate"},
                "candidate-token",
                now=100.0,
                message_id="message-1",
            )
        self.assertTrue(marked)
        self.assertNotIn("delivery_claim_token", update.call_args.args[1]["$set"])

    def test_unknown_write_results_are_confirmed_by_readback(self):
        with patch(
            "services.proactive_followup_service.PROACTIVE_FOLLOWUPS.update_one",
            side_effect=RuntimeError("unknown write result"),
        ), patch(
            "services.proactive_followup_service.PROACTIVE_FOLLOWUPS.find_one",
            return_value={"_id": "candidate"},
        ):
            marked = mark_followup_asked(
                {"_id": "candidate"},
                "candidate-token",
                now=100.0,
                message_id="message-1",
            )
        self.assertTrue(marked)

        with patch(
            "services.proactive_followup_service.profiles_coll.update_one",
            side_effect=RuntimeError("unknown write result"),
        ), patch(
            "services.proactive_followup_service.profiles_coll.find_one",
            return_value={"_id": "profile"},
        ):
            committed = commit_delivery_slot(
                "owner",
                event_key="event-1",
                now=100.0,
                message_id="message-1",
                message="最近還好嗎？",
                origin_room_id="room",
            )
        self.assertTrue(committed)

    def test_pending_delivery_blocks_expired_claim_replacement(self):
        profile = {
            "user_id": "owner",
            "proactive_care_enabled": True,
            "proactive_care_delivery_claim_until": 90.0,
            "proactive_care_pending_delivery": {"event_key": "event-1"},
        }
        with patch(
            "services.proactive_followup_service.profiles_coll.find_one",
            return_value=profile,
        ), patch(
            "services.proactive_followup_service.profiles_coll.find_one_and_update",
        ) as claim:
            token = claim_delivery_slot("owner", "new-candidate", now=100.0)
        self.assertIsNone(token)
        claim.assert_not_called()

    def test_older_unanswered_care_still_blocks_when_latest_was_answered(self):
        now = 1_000_000.0
        asked_cursor = Mock()
        asked_cursor.sort.return_value.limit.return_value = [
            {"room_id": "latest-room", "last_asked_at": now - 3600},
            {"room_id": "older-room", "last_asked_at": now - 2 * 86400},
        ]
        with patch(
            "services.proactive_followup_service.PROACTIVE_FOLLOWUPS.find",
            return_value=asked_cursor,
        ), patch(
            "services.proactive_followup_service.profiles_coll.find_one",
            return_value={},
        ), patch(
            "services.proactive_followup_service.messages_coll.find_one",
            side_effect=[{"_id": "response"}, None],
        ):
            blocked_until, available = unanswered_followup_until("owner", now=now)
        self.assertTrue(available)
        self.assertEqual(blocked_until, now - 2 * 86400 + 7 * 86400)

    def test_expired_candidate_still_keeps_unanswered_cooldown(self):
        now = 1_000_000.0
        asked_cursor = Mock()
        asked_cursor.sort.return_value.limit.return_value = [
            {
                "room_id": "expired-room",
                "status": "expired",
                "last_asked_at": now - 4 * 86400,
            },
        ]
        with patch(
            "services.proactive_followup_service.PROACTIVE_FOLLOWUPS.find",
            return_value=asked_cursor,
        ) as find, patch(
            "services.proactive_followup_service.profiles_coll.find_one",
            return_value={},
        ), patch(
            "services.proactive_followup_service.messages_coll.find_one",
            return_value=None,
        ):
            blocked_until, available = unanswered_followup_until("owner", now=now)
        self.assertNotIn("status", find.call_args.args[0])
        self.assertTrue(available)
        self.assertEqual(blocked_until, now + 3 * 86400)

    def test_delivery_history_keeps_cooldown_after_candidate_is_closed(self):
        now = 1_000_000.0
        asked_cursor = Mock()
        asked_cursor.sort.return_value.limit.return_value = []
        profile = {
            "proactive_care_delivery_history": [{
                "message_id": "message-1",
                "room_id": "room",
                "asked_at": now - 2 * 86400,
            }],
        }
        with patch(
            "services.proactive_followup_service.PROACTIVE_FOLLOWUPS.find",
            return_value=asked_cursor,
        ), patch(
            "services.proactive_followup_service.profiles_coll.find_one",
            return_value=profile,
        ), patch(
            "services.proactive_followup_service.messages_coll.find_one",
            return_value=None,
        ):
            blocked_until, available = unanswered_followup_until("owner", now=now)
        self.assertTrue(available)
        self.assertEqual(blocked_until, now + 5 * 86400)

    def test_apply_is_idempotent_after_terminal_source(self):
        proposal = {
            "action": "create", "topic": "新活動", "question_goal": "確認結果",
            "timing": "after_days", "wait_days": 3,
            "evidence_span": "我想去爬山", "confidence": .95,
            "subject": "owner", "reason_code": "test",
        }
        source = {
            "content": "我想去爬山",
            "metadata": {"message_use": metadata_for_use("ordinary")},
        }
        duplicate = __import__("pymongo.errors", fromlist=["DuplicateKeyError"]).DuplicateKeyError("duplicate")
        with patch("services.proactive_followup_service.is_owned_public_ai_room", return_value=True), \
             patch("services.proactive_followup_service._source_is_reusable", return_value=(True, source)), \
             patch("services.proactive_followup_service.list_followup_candidates", return_value=[]), \
             patch("services.proactive_followup_service.PROACTIVE_FOLLOWUPS.insert_one", side_effect=duplicate), \
             patch("services.proactive_followup_service.PROACTIVE_FOLLOWUPS.find_one", return_value={"status": "completed"}):
            result = apply_followup_proposal(
                "owner", "room", str(ObjectId()), "我想去爬山", 100.0,
                proposal, {"message_kind": "real_world_update"}, now=200.0, mode="shadow",
            )
        self.assertEqual(result["status"], "already_exists")


if __name__ == "__main__":
    unittest.main()
