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
    def test_schedule_persists_server_side_due_time(self):
        with patch("services.ayue_agent.proactive_care.profiles_coll.update_one") as update:
            schedule_proactive_care("owner", "60", last_activity=100.0, now=100.0)
        payload = update.call_args.args[1]
        self.assertEqual(payload["$set"]["next_proactive_care_at"], 160.0)
        self.assertEqual(payload["$set"]["proactive_frequency"], "60")

    def test_due_care_is_persisted_without_a_browser_poll(self):
        user = {"user_id": "owner", "proactive_frequency": "60", "last_user_activity_at": 100.0, "next_proactive_care_at": 160.0}
        decision = ProactiveCareDecision(message="你上次提到想去旅行，最近準備得如何？", focus="latest_message", grounding_span="想去旅行", confidence=.9)
        with patch("services.ayue_agent.proactive_scheduler.profiles_coll.find", return_value=_Cursor([user])), \
             patch("services.ayue_agent.proactive_scheduler.claim_proactive_care", return_value="claim"), \
             patch("services.ayue_agent.proactive_scheduler.build_proactive_care_context", return_value=Mock()), \
             patch("services.ayue_agent.proactive_scheduler.generate_proactive_care_outcome", return_value=(decision, "generated")), \
             patch("services.ayue_agent.proactive_scheduler.proactive_care_claim_is_current", return_value=True), \
             patch("services.ayue_agent.proactive_scheduler.save_message", return_value={"message_id": "m1"}) as save, \
             patch("services.ayue_agent.proactive_scheduler.finalize_proactive_care_claim", return_value=True) as finalize:
            stats = run_due_proactive_care_once(now=160.0)
        self.assertEqual(stats["delivered"], 1)
        self.assertEqual(save.call_args.args[1], "ai_assistant")
        self.assertEqual(finalize.call_args.kwargs["delivery_marker"]["message_id"], "m1")

    def test_provider_failure_retries_instead_of_consuming_activity(self):
        user = {"user_id": "owner", "proactive_frequency": "60", "last_user_activity_at": 100.0, "next_proactive_care_at": 160.0, "proactive_care_retry_count": 0}
        with patch("services.ayue_agent.proactive_scheduler.profiles_coll.find", return_value=_Cursor([user])), \
             patch("services.ayue_agent.proactive_scheduler.claim_proactive_care", return_value="claim"), \
             patch("services.ayue_agent.proactive_scheduler.build_proactive_care_context", return_value=Mock()), \
             patch("services.ayue_agent.proactive_scheduler.generate_proactive_care_outcome", return_value=(None, "provider_error")), \
             patch("services.ayue_agent.proactive_scheduler.reschedule_proactive_care_claim", return_value=True) as retry, \
             patch("services.ayue_agent.proactive_scheduler.finalize_proactive_care_claim") as finalize:
            stats = run_due_proactive_care_once(now=160.0)
        self.assertEqual(stats["retried"], 1)
        self.assertFalse(finalize.called)
        self.assertEqual(retry.call_args.kwargs["retry_after"], 220.0)

    def test_existing_frequency_without_due_time_is_backfilled(self):
        user = {"user_id": "owner", "proactive_frequency": "60", "last_user_activity_at": 100.0, "last_followup_activity_at": 0.0}
        with patch("services.ayue_agent.proactive_scheduler.profiles_coll.find", return_value=_Cursor([user])), \
             patch("services.ayue_agent.proactive_scheduler.profiles_coll.update_one", return_value=Mock(modified_count=1)) as update:
            count = backfill_missing_proactive_due_times(now=170.0)
        self.assertEqual(count, 1)
        self.assertEqual(update.call_args.args[1]["$set"]["next_proactive_care_at"], 170.0)


if __name__ == "__main__":
    unittest.main()
