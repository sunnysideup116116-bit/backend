import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from pymongo.errors import ConnectionFailure

import event_worker

SOCIAL_ROOT = Path(__file__).resolve().parents[1]


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

    def test_supervisor_recovers_worker_outer_exception(self):
        stop_event = threading.Event()
        attempts = []
        fatal = []

        def runner(stop):
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                raise RuntimeError("isolated outer-loop failure")
            stop.set()

        event_worker._supervise_worker(
            stop_event,
            worker_runner=runner,
            fatal_handler=lambda count, error: fatal.append((count, error)),
            max_restarts=2,
            restart_base_seconds=0,
            restart_max_seconds=0,
            poll_seconds=0.01,
            stable_seconds=30,
        )

        self.assertEqual(attempts, [1, 2])
        self.assertEqual(fatal, [])

    def test_unexpected_worker_return_has_a_bounded_recovery_limit(self):
        stop_event = threading.Event()
        attempts = []
        fatal = []

        def runner(_stop):
            attempts.append(1)

        def fail_process(count, error):
            fatal.append((count, error))
            stop_event.set()

        event_worker._supervise_worker(
            stop_event,
            worker_runner=runner,
            fatal_handler=fail_process,
            max_restarts=2,
            restart_base_seconds=0,
            restart_max_seconds=0,
            poll_seconds=0.01,
            stable_seconds=30,
        )

        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(fatal), 1)
        self.assertEqual(fatal[0][0], 3)
        self.assertIsNone(fatal[0][1])

    def test_long_running_worker_is_not_mistaken_for_dead(self):
        stop_event = threading.Event()
        started = threading.Event()
        release = threading.Event()
        attempts = []
        fatal = []

        def runner(_stop):
            attempts.append(1)
            started.set()
            release.wait(timeout=2)

        supervisor = threading.Thread(
            target=event_worker._supervise_worker,
            kwargs={
                "stop_event": stop_event,
                "worker_runner": runner,
                "fatal_handler": lambda count, error: fatal.append((count, error)),
                "max_restarts": 1,
                "restart_base_seconds": 0,
                "restart_max_seconds": 0,
                "poll_seconds": 0.01,
                "stable_seconds": 0.01,
            },
            daemon=True,
        )
        supervisor.start()
        self.assertTrue(started.wait(timeout=1))
        time.sleep(0.08)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(fatal, [])

        stop_event.set()
        release.set()
        supervisor.join(timeout=1)
        self.assertFalse(supervisor.is_alive())
        self.assertEqual(len(attempts), 1)

    def test_repeated_start_and_slow_shutdown_never_create_a_second_worker(self):
        started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        attempts = []

        def runner(stop):
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                started.set()
                release_first.wait(timeout=2)
            else:
                second_started.set()
                stop.wait(timeout=2)

        with patch.object(event_worker, "run_worker", side_effect=runner), patch.object(
            event_worker, "_SUPERVISOR_SHUTDOWN_JOIN_SECONDS", 0.01,
        ):
            event_worker.start_event_discovery_worker()
            self.assertTrue(started.wait(timeout=1))
            event_worker.start_event_discovery_worker()
            self.assertEqual(len(attempts), 1)

            event_worker.stop_event_discovery_worker()
            self.assertTrue(event_worker._supervisor_thread.is_alive())
            event_worker.start_event_discovery_worker()
            self.assertEqual(len(attempts), 1)

            release_first.set()
            deadline = time.monotonic() + 1
            while event_worker._supervisor_thread and event_worker._supervisor_thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(event_worker._supervisor_thread and event_worker._supervisor_thread.is_alive())

            event_worker.start_event_discovery_worker()
            self.assertTrue(second_started.wait(timeout=1))
            self.assertEqual(len(attempts), 2)
            event_worker.stop_event_discovery_worker()
            self.assertFalse(event_worker._supervisor_thread and event_worker._supervisor_thread.is_alive())

    def test_worker_failure_exits_after_mock_health_request_is_served(self):
        # Socket binding is restricted in this runner, so exercise the health
        # handler through an isolated same-process request fixture instead.
        script = r"""
import event_worker
import threading
import sys

attempts = []
def broken_worker(_stop):
    attempts.append(1)
    raise RuntimeError('isolated worker fault')
event_worker.run_worker = broken_worker
event_worker._SUPERVISOR_MAX_RESTARTS = 1
event_worker._SUPERVISOR_RESTART_BASE_SECONDS = 0.5
event_worker._SUPERVISOR_RESTART_MAX_SECONDS = 0.5
event_worker._SUPERVISOR_POLL_SECONDS = 0.01
def mock_social_health_handler():
    return 200, 'ok'

print('HTTP_READY', flush=True)
event_worker.start_event_discovery_worker()
command = sys.stdin.readline()
if command.strip() == 'GET':
    status, body = mock_social_health_handler()
    print(f'HTTP_RESPONSE {status} {body}', flush=True)
threading.Event().wait()
"""
        env = dict(os.environ)
        env["MONGO_URI"] = "mongodb://127.0.0.1:1/isolated"
        env["MONGO_DB_NAME"] = "event_worker_isolated_test"
        env["AYUE_SKIP_DOTENV"] = "1"
        server_root = SOCIAL_ROOT.parent
        python_paths = [str(SOCIAL_ROOT), str(server_root)]
        if env.get("PYTHONPATH"):
            python_paths.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(python_paths)
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", script],
            cwd=SOCIAL_ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            ready_line = process.stdout.readline().strip()
            if ready_line != "HTTP_READY":
                output, _ = process.communicate(timeout=3)
                self.fail(f"isolated process failed before HTTP startup: {ready_line}\n{output}")
            process.stdin.write("GET\n")
            process.stdin.flush()
            http_response = ""
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                readable, _, _ = select.select(
                    [process.stdout], [], [], max(0, deadline - time.monotonic()),
                )
                if not readable:
                    break
                line = process.stdout.readline().strip()
                if line.startswith("HTTP_RESPONSE "):
                    http_response = line
                    break
            self.assertEqual(http_response, "HTTP_RESPONSE 200 ok")

            output, _ = process.communicate(timeout=5)
            self.assertEqual(process.returncode, event_worker._SUPERVISOR_EXIT_CODE, output)
            self.assertIn("bounded recovery exhausted", output)
            self.assertIn("last_error=RuntimeError", output)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=2)


if __name__ == "__main__":
    unittest.main()
