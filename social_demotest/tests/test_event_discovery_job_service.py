import os
import unittest
from unittest.mock import MagicMock, patch

from services import event_discovery_job_service as jobs
from services import interactive_priority_service as priority


class EventDiscoveryJobServiceTests(unittest.TestCase):
    def test_public_snapshot_exposes_bounded_category_funnel(self):
        result = jobs._public_snapshot({
            "state": "finished", "category_counts": {"音樂": 2},
            "validation_counts": {"音樂": {"outside_window": 3}},
            "reconciliation": {"deduplicated_count": 1, "pruned_count": 0},
            "supplemental": {"triggered_categories": ["音樂"]},
            "job_token": "private",
        })

        self.assertEqual(result["category_counts"], {"音樂": 2})
        self.assertEqual(result["validation_counts"]["音樂"]["outside_window"], 3)
        self.assertEqual(result["reconciliation"]["deduplicated_count"], 1)
        self.assertNotIn("job_token", result)

    @patch.object(jobs, "_jobs")
    def test_enqueue_persists_bounded_worker_job(self, collection):
        collection.find_one_and_update.return_value = {
            "_id": "event-discovery-singleton", "state": "queued",
            "run_number": 3, "started_at": 0.0, "finished_at": 0.0,
            "job_token": "private-token", "source": "demo",
        }

        result = jobs.enqueue_event_discovery_job(
            region="高雄", window_days=999,
            categories=["市集", "bad", "音樂"], source="demo",
        )

        self.assertEqual(result["status"], "queued")
        self.assertNotIn("job_token", result)
        update = collection.find_one_and_update.call_args.args[1]
        self.assertEqual(update["$set"]["window_days"], 60)
        self.assertEqual(update["$set"]["categories"], ["市集", "音樂"])

    @patch.object(jobs, "_jobs")
    def test_claim_uses_atomic_lease(self, collection):
        collection.find_one_and_update.return_value = {"state": "running"}

        jobs.claim_event_discovery_job("worker-1", lease_seconds=300)

        query = collection.find_one_and_update.call_args.args[0]
        update = collection.find_one_and_update.call_args.args[1]
        self.assertEqual(query["_id"], "event-discovery-singleton")
        self.assertEqual(update["$set"]["state"], "running")
        self.assertEqual(update["$set"]["lease_owner"], "worker-1")

    @patch.object(jobs, "enqueue_event_discovery_job")
    def test_monday_schedule_enqueues_full_weekly_cycle(self, enqueue):
        enqueue.return_value = {"status": "queued"}
        from datetime import datetime

        with patch.dict(os.environ, {
            "EVENT_DISCOVERY_WEEKDAY": "0", "EVENT_DISCOVERY_HOUR": "8",
        }):
            result = jobs.enqueue_weekly_event_discovery_if_due(
                datetime(2026, 8, 17, 8, 0, tzinfo=jobs.TAIPEI),
            )

        self.assertEqual(result, {"status": "queued"})
        self.assertEqual(enqueue.call_args.kwargs["job_kind"], "weekly_cycle")
        self.assertEqual(enqueue.call_args.kwargs["source"], "scheduled")

    @patch.object(jobs, "_jobs")
    def test_change_stream_watches_only_queued_singleton_job(self, collection):
        stream = MagicMock()
        collection.watch.return_value = stream

        result = jobs.open_event_discovery_job_change_stream(
            max_await_time_ms=90_000,
        )

        self.assertIs(result, stream)
        pipeline = collection.watch.call_args.args[0]
        match = pipeline[0]["$match"]
        self.assertEqual(match["documentKey._id"], "event-discovery-singleton")
        self.assertEqual(match["fullDocument.state"], "queued")
        self.assertEqual(
            collection.watch.call_args.kwargs["full_document"], "updateLookup",
        )
        self.assertEqual(
            collection.watch.call_args.kwargs["max_await_time_ms"], 90_000,
        )

    @patch.object(jobs, "_jobs")
    def test_renew_lease_requires_same_job_and_worker(self, collection):
        collection.update_one.return_value = MagicMock(modified_count=1)

        renewed = jobs.renew_event_discovery_job_lease(
            {"job_token": "job-7"}, "worker-1", lease_seconds=300,
        )

        self.assertTrue(renewed)
        query = collection.update_one.call_args.args[0]
        self.assertEqual(query["job_token"], "job-7")
        self.assertEqual(query["lease_owner"], "worker-1")
        self.assertEqual(query["state"], "running")

    def test_interactive_lease_is_noop(self):
        with priority.interactive_chat_lease():
            pass
        self.assertFalse(priority.interactive_chat_active())


if __name__ == "__main__":
    unittest.main()
