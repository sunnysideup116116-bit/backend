import unittest
from unittest.mock import MagicMock, patch

from pymongo.errors import ConnectionFailure

import event_worker


class EventWorkerTests(unittest.TestCase):
    @patch.object(event_worker.time, "sleep")
    def test_wait_for_work_uses_change_stream_without_poll_sleep(self, sleep):
        stream = MagicMock()
        stream.try_next.return_value = None

        result = event_worker._wait_for_work(stream, 60.0)

        self.assertIs(result, stream)
        stream.try_next.assert_called_once_with()
        sleep.assert_not_called()

    @patch.object(event_worker.time, "sleep")
    def test_change_stream_failure_falls_back_to_bounded_reconciliation(self, sleep):
        stream = MagicMock()
        stream.try_next.side_effect = ConnectionFailure("offline")

        result = event_worker._wait_for_work(stream, 60.0)

        self.assertIsNone(result)
        stream.close.assert_called_once_with()
        sleep.assert_called_once_with(60.0)

    @patch.dict("os.environ", {"EVENT_WORKER_RECONCILE_SECONDS": "2"})
    def test_reconciliation_interval_has_safe_lower_bound(self):
        self.assertEqual(event_worker._reconcile_seconds(), 10.0)

    @patch.object(event_worker, "update_event_discovery_job_stage")
    @patch.object(event_worker, "run_weekly_event_cycle")
    def test_weekly_job_runs_the_owned_cycle(self, run_cycle, update_stage):
        run_cycle.return_value = {"status": "success", "job_kind": "weekly_cycle"}
        job = {
            "job_kind": "weekly_cycle", "region": "高雄",
            "window_days": 30, "categories": ["市集", "音樂"],
        }

        result = event_worker._execute_job(job, "worker-1")

        self.assertEqual(result["job_kind"], "weekly_cycle")
        callback = run_cycle.call_args.kwargs["stage_callback"]
        callback("resetting")
        update_stage.assert_called_once_with(job, "worker-1", "resetting")

    @patch.object(event_worker, "update_event_discovery_job_stage")
    @patch.object(event_worker, "discover_and_ingest_events")
    def test_manual_job_remains_discovery_only(self, discover, update_stage):
        discover.return_value = {"status": "success"}

        result = event_worker._execute_job(
            {"job_kind": "discovery", "categories": ["市集"]}, "worker-1",
        )

        self.assertEqual(result["job_kind"], "discovery")
        update_stage.assert_called_once_with(
            {"job_kind": "discovery", "categories": ["市集"]},
            "worker-1", "discovering",
        )


if __name__ == "__main__":
    unittest.main()
