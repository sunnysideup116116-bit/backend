import unittest
from unittest.mock import Mock, patch

from services.ayue_agent.proactive_care import ProactiveCareDecision, schedule_proactive_care
from services.ayue_agent.proactive_scheduler import backfill_missing_proactive_due_times, run_due_proactive_care_once


class _Cursor(list):
    def sort(self, *_args):
        return self

    def limit(self, _count):
        return self


class ProactiveSchedulerTests(unittest.TestCase):
    def setUp(self):
        pending = patch(
            "services.ayue_agent.proactive_scheduler.list_pending_delivery_slots",
            return_value=[],
        )
        pending.start()
        self.addCleanup(pending.stop)

    def test_schedule_persists_server_side_due_time(self):
        with patch("services.ayue_agent.proactive_care.profiles_coll.update_one") as update:
            schedule_proactive_care("owner", "60", last_activity=100.0, now=100.0)
        payload = update.call_args.args[1]
        self.assertEqual(payload["$set"]["next_proactive_care_at"], 160.0)
        self.assertEqual(payload["$set"]["proactive_frequency"], "60")

    def test_due_care_is_persisted_without_a_browser_poll(self):
        candidate = {
            "_id": "candidate-1", "user_id": "owner", "room_id": "room-1",
            "source_message_id": "64b64c8f0000000000000001",
            "available_at": 3600.0, "next_attempt_at": 3600.0,
            "expires_at": 10000.0, "revision": 1,
            "topic": "週末看展", "question_goal": "確認感覺",
            "evidence_span": "週末想去看展",
        }
        user = {"user_id": "owner", "proactive_care_enabled": True,
                "last_user_activity_at": 0.0, "mediator_calendar_access": False}
        decision = ProactiveCareDecision(message="週末看展後感覺如何？", focus="follow_up", grounding_span="週末想去看展", confidence=.9)
        pending = {
            "event_key": "proactive-care:candidate-1:r1",
            "candidate_id": "candidate-1", "candidate_token": "claim",
            "delivery_token": "delivery", "message": decision.message,
            "origin_room_id": "room-1", "asked_at": 3600.0,
        }
        user["proactive_care_pending_delivery"] = pending
        persisted = {"_id": "m1", "content": decision.message, "timestamp": 3601.0}
        with patch("services.ayue_agent.proactive_scheduler.followup_mode_for_user", return_value="on"), \
             patch("services.ayue_agent.proactive_scheduler.expire_stale_followups"), \
             patch("services.ayue_agent.proactive_scheduler.PROACTIVE_FOLLOWUPS.find", return_value=_Cursor([candidate])), \
             patch("services.ayue_agent.proactive_scheduler.is_owned_public_ai_room", return_value=True), \
             patch("services.ayue_agent.proactive_scheduler.profiles_coll.find_one", return_value=user), \
             patch("services.ayue_agent.proactive_scheduler.messages_coll.find_one", return_value={"metadata": {"message_use": {"version": "message-use-v1", "use": "ordinary"}}}), \
             patch("services.ayue_agent.proactive_scheduler.next_quiet_end", return_value=None), \
             patch("services.ayue_agent.proactive_scheduler.unanswered_followup_until", return_value=(None, True)), \
             patch("services.ayue_agent.proactive_scheduler.claim_delivery_slot", return_value="delivery"), \
             patch("services.ayue_agent.proactive_scheduler.claim_followup_candidate", return_value=("claim", candidate)), \
             patch("services.ayue_agent.proactive_scheduler.build_proactive_care_context", return_value=Mock()), \
             patch("services.ayue_agent.proactive_scheduler.generate_proactive_care_outcome", return_value=(decision, "generated")), \
             patch("services.ayue_agent.proactive_scheduler.stage_delivery_slot", return_value=pending), \
             patch("services.ayue_agent.proactive_scheduler.PROACTIVE_FOLLOWUPS.find_one", return_value=candidate), \
             patch.multiple("services.ayue_agent.proactive_scheduler",
                 authorize_pending_delivery_slot=Mock(return_value=True),
                 followup_delivery_claim_is_current=Mock(return_value=True),
                 find_system_message_by_event=Mock(side_effect=[None, persisted])), \
             patch("services.ayue_agent.proactive_scheduler.save_system_message_once", return_value={"message_id": "m1", "created": True}) as save, \
             patch("services.ayue_agent.proactive_scheduler.mark_followup_asked", return_value=True), \
             patch("services.ayue_agent.proactive_scheduler.commit_delivery_slot", return_value=True) as commit, \
             patch("services.ayue_agent.proactive_scheduler.release_delivery_slot"):
            stats = run_due_proactive_care_once(now=3600.0)
        self.assertEqual(stats["delivered"], 1)
        self.assertEqual(save.call_args.args[:2], ("room-1", "週末看展後感覺如何？"))
        self.assertEqual(commit.call_args.kwargs["origin_room_id"], "room-1")
        self.assertEqual(commit.call_args.kwargs["message_id"], "m1")
        self.assertEqual(commit.call_args.kwargs["now"], 3601.0)

    def test_provider_failure_retries_instead_of_consuming_activity(self):
        candidate = {
            "_id": "candidate-1", "user_id": "owner", "room_id": "room-1",
            "source_message_id": "64b64c8f0000000000000001",
            "available_at": 3600.0, "next_attempt_at": 3600.0,
            "expires_at": 10000.0, "revision": 1,
        }
        user = {"user_id": "owner", "proactive_care_enabled": True,
                "last_user_activity_at": 0.0, "mediator_calendar_access": False}
        with patch("services.ayue_agent.proactive_scheduler.followup_mode_for_user", return_value="on"), \
             patch("services.ayue_agent.proactive_scheduler.expire_stale_followups"), \
             patch("services.ayue_agent.proactive_scheduler.PROACTIVE_FOLLOWUPS.find", return_value=_Cursor([candidate])), \
             patch("services.ayue_agent.proactive_scheduler.is_owned_public_ai_room", return_value=True), \
             patch("services.ayue_agent.proactive_scheduler.profiles_coll.find_one", return_value=user), \
             patch("services.ayue_agent.proactive_scheduler.messages_coll.find_one", return_value={"metadata": {"message_use": {"version": "message-use-v1", "use": "ordinary"}}}), \
             patch("services.ayue_agent.proactive_scheduler.next_quiet_end", return_value=None), \
             patch("services.ayue_agent.proactive_scheduler.unanswered_followup_until", return_value=(None, True)), \
             patch("services.ayue_agent.proactive_scheduler.claim_delivery_slot", return_value="delivery"), \
             patch("services.ayue_agent.proactive_scheduler.claim_followup_candidate", return_value=("claim", candidate)), \
             patch("services.ayue_agent.proactive_scheduler.build_proactive_care_context", return_value=Mock()), \
             patch("services.ayue_agent.proactive_scheduler.generate_proactive_care_outcome", return_value=(None, "provider_error")), \
             patch("services.ayue_agent.proactive_scheduler.release_delivery_slot"), \
             patch("services.ayue_agent.proactive_scheduler.release_followup_candidate") as retry:
            stats = run_due_proactive_care_once(now=3600.0)
        self.assertEqual(stats["retried"], 1)
        self.assertTrue(retry.called)
        self.assertTrue(retry.call_args.kwargs["retry"])

    def test_shadow_mode_never_sends(self):
        with patch("services.ayue_agent.proactive_scheduler.followup_mode_for_user", return_value="shadow"), \
             patch("services.ayue_agent.proactive_scheduler.PROACTIVE_FOLLOWUPS.count_documents", return_value=2), \
             patch("services.ayue_agent.proactive_scheduler.PROACTIVE_FOLLOWUPS.find") as find, \
             patch("services.ayue_agent.proactive_scheduler.save_system_message_once") as save:
            stats = run_due_proactive_care_once(now=3600.0)
        self.assertEqual(stats["shadowed"], 2)
        find.assert_not_called()
        save.assert_not_called()

    def test_scheduler_scans_expired_processing_leases(self):
        with patch("services.ayue_agent.proactive_scheduler.followup_mode_for_user", return_value="on"), \
             patch("services.ayue_agent.proactive_scheduler.expire_stale_followups"), \
             patch("services.ayue_agent.proactive_scheduler.PROACTIVE_FOLLOWUPS.find", return_value=_Cursor()) as find:
            stats = run_due_proactive_care_once(now=3600.0)
        self.assertEqual(stats["scanned"], 0)
        recovery_branch = find.call_args.args[0]["$or"][1]
        self.assertEqual(recovery_branch["status"], "processing")
        self.assertEqual(recovery_branch["lease_until"], {"$lte": 3600.0})
        self.assertEqual(len(find.call_args.args[0]["$or"]), 2)

    def test_pause_during_generation_stops_before_message_write(self):
        candidate = {
            "_id": "candidate-1", "user_id": "owner", "room_id": "room-1",
            "source_message_id": "64b64c8f0000000000000001",
            "available_at": 3600.0, "next_attempt_at": 3600.0,
            "expires_at": 10000.0, "revision": 1,
        }
        user = {"user_id": "owner", "proactive_care_enabled": True,
                "last_user_activity_at": 0.0, "mediator_calendar_access": False}
        decision = ProactiveCareDecision(
            message="週末看展後感覺如何？", focus="follow_up",
            grounding_span="週末想去看展", confidence=.9,
        )
        with patch("services.ayue_agent.proactive_scheduler.followup_mode_for_user", return_value="on"), \
             patch("services.ayue_agent.proactive_scheduler.expire_stale_followups"), \
             patch("services.ayue_agent.proactive_scheduler.PROACTIVE_FOLLOWUPS.find", return_value=_Cursor([candidate])), \
             patch("services.ayue_agent.proactive_scheduler.is_owned_public_ai_room", return_value=True), \
             patch("services.ayue_agent.proactive_scheduler.profiles_coll.find_one", return_value=user), \
             patch("services.ayue_agent.proactive_scheduler.messages_coll.find_one", return_value={"metadata": {"message_use": {"version": "message-use-v1", "use": "ordinary"}}}), \
             patch("services.ayue_agent.proactive_scheduler.next_quiet_end", return_value=None), \
             patch("services.ayue_agent.proactive_scheduler.unanswered_followup_until", return_value=(None, True)), \
             patch("services.ayue_agent.proactive_scheduler.claim_delivery_slot", return_value="delivery"), \
             patch("services.ayue_agent.proactive_scheduler.claim_followup_candidate", return_value=("claim", candidate)), \
             patch("services.ayue_agent.proactive_scheduler.build_proactive_care_context", return_value=Mock()), \
             patch("services.ayue_agent.proactive_scheduler.generate_proactive_care_outcome", return_value=(decision, "generated")), \
             patch("services.ayue_agent.proactive_scheduler.followup_delivery_claim_is_current", return_value=False), \
             patch("services.ayue_agent.proactive_scheduler.save_system_message_once") as save, \
             patch("services.ayue_agent.proactive_scheduler.release_delivery_slot"), \
             patch("services.ayue_agent.proactive_scheduler.release_followup_candidate"):
            stats = run_due_proactive_care_once(now=3600.0)
        save.assert_not_called()
        self.assertEqual(stats["delivered"], 0)

    def test_non_followup_decision_is_never_persisted(self):
        candidate = {
            "_id": "candidate-1", "user_id": "owner", "room_id": "room-1",
            "source_message_id": "64b64c8f0000000000000001",
            "available_at": 3600.0, "next_attempt_at": 3600.0,
            "expires_at": 10000.0, "revision": 1,
        }
        user = {"user_id": "owner", "proactive_care_enabled": True,
                "last_user_activity_at": 0.0, "mediator_calendar_access": False}
        decision = ProactiveCareDecision(
            message="最近過得如何？", focus="latest_message",
            grounding_span="最近", confidence=.9,
        )
        with patch("services.ayue_agent.proactive_scheduler.followup_mode_for_user", return_value="on"), \
             patch("services.ayue_agent.proactive_scheduler.expire_stale_followups"), \
             patch("services.ayue_agent.proactive_scheduler.PROACTIVE_FOLLOWUPS.find", return_value=_Cursor([candidate])), \
             patch("services.ayue_agent.proactive_scheduler.is_owned_public_ai_room", return_value=True), \
             patch("services.ayue_agent.proactive_scheduler.profiles_coll.find_one", return_value=user), \
             patch("services.ayue_agent.proactive_scheduler.messages_coll.find_one", return_value={"metadata": {"message_use": {"version": "message-use-v1", "use": "ordinary"}}}), \
             patch("services.ayue_agent.proactive_scheduler.next_quiet_end", return_value=None), \
             patch("services.ayue_agent.proactive_scheduler.unanswered_followup_until", return_value=(None, True)), \
             patch("services.ayue_agent.proactive_scheduler.claim_delivery_slot", return_value="delivery"), \
             patch("services.ayue_agent.proactive_scheduler.claim_followup_candidate", return_value=("claim", candidate)), \
             patch("services.ayue_agent.proactive_scheduler.build_proactive_care_context", return_value=Mock()), \
             patch("services.ayue_agent.proactive_scheduler.generate_proactive_care_outcome", return_value=(decision, "generated")), \
             patch("services.ayue_agent.proactive_scheduler.followup_delivery_claim_is_current") as recheck, \
             patch("services.ayue_agent.proactive_scheduler.save_system_message_once") as save, \
             patch("services.ayue_agent.proactive_scheduler.release_delivery_slot"), \
             patch("services.ayue_agent.proactive_scheduler.release_followup_candidate") as retry:
            stats = run_due_proactive_care_once(now=3600.0)
        save.assert_not_called()
        recheck.assert_not_called()
        self.assertEqual(stats["delivered"], 0)
        self.assertEqual(stats["retried"], 1)
        self.assertTrue(retry.call_args.kwargs["retry"])

    def test_candidate_state_change_does_not_block_staged_delivery(self):
        candidate = {
            "_id": "candidate-1", "user_id": "owner", "room_id": "room-1",
            "source_message_id": "64b64c8f0000000000000001",
            "available_at": 3600.0, "next_attempt_at": 3600.0,
            "expires_at": 10000.0, "revision": 1,
        }
        user = {"user_id": "owner", "proactive_care_enabled": True,
                "last_user_activity_at": 0.0, "mediator_calendar_access": False}
        decision = ProactiveCareDecision(
            message="週末看展後感覺如何？", focus="follow_up",
            grounding_span="週末想去看展", confidence=.9,
        )
        pending = {
            "event_key": "proactive-care:candidate-1:r1",
            "candidate_id": "candidate-1", "candidate_token": "claim",
            "delivery_token": "delivery", "message": decision.message,
            "origin_room_id": "room-1", "asked_at": 3600.0,
        }
        user["proactive_care_pending_delivery"] = pending
        existing = {
            "_id": "stable-message", "timestamp": 3601.0,
            "content": "週末看展後感覺如何？",
            "metadata": {"message_use": {"version": "message-use-v1", "use": "ordinary"}},
        }
        with patch("services.ayue_agent.proactive_scheduler.followup_mode_for_user", return_value="on"), \
             patch("services.ayue_agent.proactive_scheduler.expire_stale_followups"), \
             patch("services.ayue_agent.proactive_scheduler.PROACTIVE_FOLLOWUPS.find", return_value=_Cursor([candidate])), \
             patch("services.ayue_agent.proactive_scheduler.is_owned_public_ai_room", return_value=True), \
             patch("services.ayue_agent.proactive_scheduler.profiles_coll.find_one", return_value=user), \
             patch("services.ayue_agent.proactive_scheduler.messages_coll.find_one", side_effect=[existing, existing, existing]), \
             patch("services.ayue_agent.proactive_scheduler.next_quiet_end", return_value=None), \
             patch("services.ayue_agent.proactive_scheduler.unanswered_followup_until", return_value=(None, True)), \
             patch("services.ayue_agent.proactive_scheduler.claim_delivery_slot", return_value="delivery"), \
             patch("services.ayue_agent.proactive_scheduler.claim_followup_candidate", return_value=("claim", candidate)), \
             patch("services.ayue_agent.proactive_scheduler.build_proactive_care_context", return_value=Mock()), \
             patch("services.ayue_agent.proactive_scheduler.generate_proactive_care_outcome", return_value=(decision, "generated")), \
             patch("services.ayue_agent.proactive_scheduler.followup_delivery_claim_is_current", return_value=True), \
             patch("services.ayue_agent.proactive_scheduler.stage_delivery_slot", return_value=pending), \
             patch("services.ayue_agent.proactive_scheduler.find_system_message_by_event", return_value=existing), \
             patch("services.ayue_agent.proactive_scheduler.save_system_message_once", return_value={
                 "message_id": "stable-message", "created": True,
             }) as save, \
             patch("services.ayue_agent.proactive_scheduler.mark_followup_asked", return_value=False), \
             patch("services.ayue_agent.proactive_scheduler.commit_delivery_slot", return_value=True) as commit, \
             patch("services.ayue_agent.proactive_scheduler.release_delivery_slot"), \
             patch("services.ayue_agent.proactive_scheduler.release_followup_candidate"):
            stats = run_due_proactive_care_once(now=3600.0)
        self.assertEqual(stats["delivered"], 1)
        save.assert_not_called()
        self.assertEqual(commit.call_args.kwargs["event_key"], pending["event_key"])

    def test_profile_commit_failure_recovers_without_republishing_notice(self):
        candidate = {
            "_id": "candidate-1", "user_id": "owner", "room_id": "room-1",
            "source_message_id": "64b64c8f0000000000000001",
            "available_at": 3600.0, "next_attempt_at": 3600.0,
            "expires_at": 10000.0, "revision": 1,
        }
        user = {"user_id": "owner", "proactive_care_enabled": True,
                "last_user_activity_at": 0.0, "mediator_calendar_access": False}
        decision = ProactiveCareDecision(
            message="週末看展後感覺如何？", focus="follow_up",
            grounding_span="週末想去看展", confidence=.9,
        )
        pending = {
            "event_key": "proactive-care:candidate-1:r1",
            "candidate_id": "candidate-1", "candidate_token": "claim",
            "delivery_token": "delivery", "message": decision.message,
            "origin_room_id": "room-1", "asked_at": 3600.0,
        }
        pending_profile = {
            "user_id": "owner", "proactive_care_enabled": True,
            "proactive_care_pending_delivery": pending,
        }
        user["proactive_care_pending_delivery"] = pending
        existing = {
            "_id": "stable-message", "timestamp": 3601.0,
            "content": decision.message,
            "metadata": {"message_use": {"version": "message-use-v1", "use": "ordinary"}},
        }
        with patch.multiple("services.ayue_agent.proactive_scheduler",
                 followup_mode_for_user=Mock(return_value="on"),
                 list_pending_delivery_slots=Mock(side_effect=[[], [pending_profile]])), \
             patch("services.ayue_agent.proactive_scheduler.expire_stale_followups"), \
             patch("services.ayue_agent.proactive_scheduler.PROACTIVE_FOLLOWUPS.find", side_effect=[
                 _Cursor([candidate]), _Cursor(),
             ]), \
             patch("services.ayue_agent.proactive_scheduler.is_owned_public_ai_room", return_value=True), \
             patch("services.ayue_agent.proactive_scheduler.profiles_coll.find_one", return_value=user), \
             patch("services.ayue_agent.proactive_scheduler.messages_coll.find_one", side_effect=[existing, existing]), \
             patch("services.ayue_agent.proactive_scheduler.next_quiet_end", return_value=None), \
             patch("services.ayue_agent.proactive_scheduler.unanswered_followup_until", return_value=(None, True)), \
             patch("services.ayue_agent.proactive_scheduler.claim_delivery_slot", return_value="delivery"), \
             patch("services.ayue_agent.proactive_scheduler.claim_followup_candidate", return_value=("claim", candidate)), \
             patch("services.ayue_agent.proactive_scheduler.build_proactive_care_context", return_value=Mock()), \
             patch("services.ayue_agent.proactive_scheduler.generate_proactive_care_outcome", return_value=(decision, "generated")), \
             patch("services.ayue_agent.proactive_scheduler.stage_delivery_slot", return_value=pending) as stage, \
             patch("services.ayue_agent.proactive_scheduler.PROACTIVE_FOLLOWUPS.find_one", return_value=candidate), \
             patch.multiple("services.ayue_agent.proactive_scheduler",
                 authorize_pending_delivery_slot=Mock(return_value=True),
                 followup_delivery_claim_is_current=Mock(return_value=True),
                 find_system_message_by_event=Mock(side_effect=[None, existing, existing])), \
             patch("services.ayue_agent.proactive_scheduler.save_system_message_once", side_effect=[
                 {"message_id": "stable-message", "created": True},
                 {"message_id": "stable-message", "created": False},
             ]) as save, \
             patch("services.ayue_agent.proactive_scheduler.mark_followup_asked", side_effect=[True, False]) as mark, \
             patch("services.ayue_agent.proactive_scheduler.commit_delivery_slot", side_effect=[False, True]) as commit, \
             patch("services.ayue_agent.proactive_scheduler.release_delivery_slot"), \
             patch("services.ayue_agent.proactive_scheduler.release_followup_candidate") as release:
            first = run_due_proactive_care_once(now=3600.0)
            second = run_due_proactive_care_once(now=3660.0)
        self.assertEqual(first["retried"], 1)
        self.assertEqual(second["delivered"], 1)
        self.assertEqual(mark.call_count, 2)
        self.assertEqual(commit.call_count, 2)
        self.assertEqual(stage.call_count, 1)
        self.assertEqual(save.call_count, 1)
        self.assertEqual(
            {call.kwargs["event_key"] for call in save.call_args_list},
            {pending["event_key"]},
        )
        release.assert_not_called()

    def test_existing_frequency_without_due_time_is_backfilled(self):
        user = {"user_id": "owner", "proactive_frequency": "60", "last_user_activity_at": 100.0, "last_followup_activity_at": 0.0}
        with patch("services.ayue_agent.proactive_scheduler.profiles_coll.find", return_value=_Cursor([user])), \
             patch("services.ayue_agent.proactive_scheduler.profiles_coll.update_one", return_value=Mock(modified_count=1)) as update:
            count = backfill_missing_proactive_due_times(now=170.0)
        self.assertEqual(count, 1)
        self.assertEqual(update.call_args.args[1]["$set"]["next_proactive_care_at"], 170.0)


if __name__ == "__main__":
    unittest.main()
