import unittest
from unittest.mock import patch

from fastapi import HTTPException, Request

from routers.system import local_ayue_debug_run
from services.ayue_agent.v3.debug_trace import append_event, begin_run, finish_run, get_run


def _request(host: str) -> Request:
    return Request({
        "type": "http", "method": "GET", "scheme": "http",
        "path": "/api/debug/ayue-runs/run", "raw_path": b"/api/debug/ayue-runs/run",
        "query_string": b"", "headers": [(b"host", host.encode("ascii"))],
        "client": (host, 12345), "server": (host, 80),
    })


class V3DebugTraceTests(unittest.TestCase):
    def test_ephemeral_trace_is_bounded_and_redacts_secret_keys(self):
        run_id = "a" * 32
        with patch.dict("os.environ", {"AYUE_LOCAL_DEBUG_TRACE": "on"}):
            begin_run(run_id, "owner")
            append_event(run_id, "planner_completed", prompt_raw="嗨", api_key="secret")
            finish_run(run_id, status="completed", response={"reply": "你好"})
            run = get_run(run_id, "owner")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["events"][0]["api_key"], "[REDACTED]")
        self.assertEqual(run["events"][-1]["response"]["reply"], "你好")

    def test_trace_is_owner_scoped(self):
        run_id = "b" * 32
        with patch.dict("os.environ", {"AYUE_LOCAL_DEBUG_TRACE": "on"}):
            begin_run(run_id, "owner")
            self.assertIsNone(get_run(run_id, "other"))

    def test_http_adapter_requires_true_loopback(self):
        run_id = "c" * 32
        with patch.dict("os.environ", {"AYUE_LOCAL_DEBUG_TRACE": "on"}):
            begin_run(run_id, "owner")
            with self.assertRaises(HTTPException) as raised:
                local_ayue_debug_run(run_id, "owner", _request("testclient"))
            self.assertEqual(raised.exception.status_code, 404)
            result = local_ayue_debug_run(run_id, "owner", _request("127.0.0.1"))
        self.assertEqual(result["run_id"], run_id)


if __name__ == "__main__":
    unittest.main()
