"""Hermetic R3.1 runner tests: fake provider, no credentials/Graph/network/artifacts."""
from copy import deepcopy
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_r31_reliability as runner


class R31RunnerTests(unittest.TestCase):
    def case(self, case_id="h01-f", expected="NO"):
        return {"id": case_id, "Q": "Watching Synthetic Shows", "C": "Performing Synthetic Shows",
                "expected": expected, "relation": "role_mismatch", "category": "role_mismatch"}

    def response(self, decision="NO", content=None, finish="stop"):
        relation = {"NO": "role_mismatch", "YES": "equivalent", "ABSTAIN": "unknown"}[decision]
        if content is None:
            content = json.dumps({"results": [{"id": "h01-f", "decision": decision, "relation": relation}]})
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
                               message=SimpleNamespace(content=content))],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15))

    def fake_run(self, outcomes):
        """Execute the actual run orchestration behind in-memory IO boundaries."""
        calls, snapshots, closed = [], [], []
        pending = list(outcomes)
        fake_key = "synthetic-local-test-credential-only-ABCD"
        case = self.case()
        jobs = [{"job_id": "matrix-r1-s1-b001", "repeat": 1, "batch_size": 1, "cases": [case]}]

        def fake_config(_path, names):
            values = {"LLM_API_KEY": fake_key, "LLM_BASE_URL": "https://ollama.com/v1",
                      "LLM_MODEL_ID": "deepseek-v4.1-flash:cloud"}
            return {name: values[name] for name in names}

        def fake_http_client(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            return SimpleNamespace(**kwargs)

        def fake_openai(**kwargs):
            self.assertEqual(kwargs["max_retries"], 0)
            self.assertEqual(kwargs["timeout"], 60)

            def create(**request):
                # Exercise the real runner's request allowlist/budget hook, but
                # do not create an actual HTTP client, socket or request object.
                event = SimpleNamespace(url=SimpleNamespace(
                    scheme="https", host="ollama.com", path="/v1/chat/completions"))
                for hook in kwargs["http_client"].event_hooks["request"]:
                    hook(event)
                calls.append(deepcopy(request))
                if not pending:
                    raise AssertionError("Unexpected extra model attempt")
                outcome = pending.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                                   close=lambda: closed.append(True))

        def capture(path, data, secrets=()):
            # Never persist the fake key or any provider output, even locally.
            text = json.dumps(data)
            self.assertNotIn(fake_key, text)
            snapshots.append((Path(path).name, deepcopy(data)))

        with tempfile.TemporaryDirectory(prefix="r31-runner-test-") as temporary, ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {
                "httpx": SimpleNamespace(Client=fake_http_client),
                "openai": SimpleNamespace(OpenAI=fake_openai),
            }))
            stack.enter_context(patch.object(runner, "frozen_cases", return_value=[case]))
            stack.enter_context(patch.object(runner, "schedule", return_value=jobs))
            stack.enter_context(patch.object(runner, "selected_config", side_effect=fake_config))
            stack.enter_context(patch.object(runner, "write_report", side_effect=capture))
            stack.enter_context(patch.object(runner, "OUT", Path(temporary)))
            stack.enter_context(patch.object(runner.time, "sleep"))
            stack.enter_context(redirect_stdout(io.StringIO()))
            runner.run(SimpleNamespace(action="matrix", model_config="not-read", matchmaker_config="not-read"))
        reports = [data for name, data in snapshots if name == "matrix.json"]
        self.assertEqual(reports[-1]["status"], "COMPLETED")
        self.assertEqual(closed, [True])
        self.assertEqual(reports[-1]["http_attempts"], len(calls))
        self.assertEqual(reports[-1]["hard_request_budget"], 2)
        self.assertEqual(reports[-1]["workers"], 3)
        for call in calls:
            payload = json.loads(call["messages"][1]["content"])
            self.assertEqual(set(payload), {"pairs"})
            self.assertEqual(set(payload["pairs"][0]), {"id", "Q", "C"})
        return calls, reports[-1], snapshots

    def test_schedule_has_two_complete_rounds_for_each_size(self):
        cases = [self.case(f"h{i:02}-{direction}") for i in range(1, 49) for direction in ("f", "r")]
        jobs = runner.schedule(cases, "matrix")
        self.assertEqual(len(jobs), 360)
        self.assertEqual(len({job["job_id"] for job in jobs}), 360)
        self.assertEqual(jobs, runner.schedule(cases, "matrix"))
        for repeat in (1, 2):
            for size in (1, 2, 4, 8):
                with self.subTest(repeat=repeat, size=size):
                    selected = [job for job in jobs if job["repeat"] == repeat and job["batch_size"] == size]
                    ids = [case["id"] for job in selected for case in job["cases"]]
                    self.assertEqual(len(selected), 96 // size)
                    self.assertEqual(len(ids), 96)
                    self.assertEqual(len(set(ids)), 96)
                    self.assertEqual(set(ids), {case["id"] for case in cases})

    def test_failure_text_rejects_secret_material_and_oversize(self):
        key = "synthetic-offline-fake-credential-Z9XZ"
        for text in (key, "output " + key[:6], "output " + key[-4:],
                     "Authorization: bearer examplecredentialvalue1234"):
            with self.subTest(text_kind=len(text)):
                saved, _ = runner.safe_failure_text(text, key)
                self.assertIsNone(saved)
        self.assertIsNone(runner.safe_failure_text("x" * 65537, key)[0])
        self.assertIsNone(runner.safe_failure_text(None, key)[0])
        self.assertEqual(runner.safe_failure_text('{"results":[]}', key),
                         ('{"results":[]}', "synthetic_only_secret_checked"))

    def test_valid_no_and_abstain_never_retry(self):
        for decision in ("NO", "ABSTAIN", "YES"):
            with self.subTest(decision=decision):
                calls, report, _ = self.fake_run([self.response(decision)])
                self.assertEqual(len(calls), 1)
                job = report["jobs"][0]
                self.assertTrue(job["first_pass_valid"])
                self.assertTrue(job["eventual_valid"])
                self.assertEqual(job["attempts"][0]["decisions"]["h01-f"]["decision"], decision)

    def test_schema_retry_is_identical_and_at_most_once(self):
        calls, report, saved = self.fake_run([self.response(content="invalid JSON"), self.response("NO")])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        job = report["jobs"][0]
        self.assertFalse(job["first_pass_valid"])
        self.assertTrue(job["eventual_valid"])
        self.assertEqual(job["attempts"][0]["decisions"], {})
        self.assertEqual(job["attempts"][0]["errors"], ["invalid_json"])
        self.assertTrue(any("model_output" in data for _, data in saved))
        calls, report, _ = self.fake_run([self.response(content="invalid"), self.response(content="invalid again")])
        self.assertEqual(len(calls), 2)
        self.assertFalse(report["jobs"][0]["eventual_valid"])
        self.assertEqual(report["jobs"][0]["attempts"][-1]["decisions"], {})

    def test_auth_failure_does_not_retry_or_save_exception_message(self):
        class AuthenticationFailure(Exception):
            status_code = 403
            def __str__(self): raise AssertionError("Exception text must never be inspected")
        calls, report, saved = self.fake_run([AuthenticationFailure()])
        self.assertEqual(len(calls), 1)
        record = report["jobs"][0]["attempts"][0]
        self.assertEqual(record["metadata"]["subtype"], "auth_failure")
        self.assertFalse(record["valid"])
        self.assertFalse(any("model_output" in data for _, data in saved))

    def test_timeout_retries_once_without_reasking_valid_semantic_no(self):
        calls, report, _ = self.fake_run([TimeoutError(), self.response("NO")])
        self.assertEqual(len(calls), 2)
        records = report["jobs"][0]["attempts"]
        self.assertEqual(records[0]["metadata"]["subtype"], "timeout")
        self.assertTrue(records[1]["valid"])

    def test_summary_error_is_not_a_valid_no_or_accuracy_denominator(self):
        case = self.case(expected="YES")
        case["relation"] = "equivalent"
        good = {"valid": True, "errors": [], "decisions": {
            "h01-f": {"id": "h01-f", "decision": "YES", "relation": "equivalent"}},
                "usage": None, "latency_seconds": 1.0}
        bad = {"valid": False, "errors": ["invalid_json"], "decisions": {},
               "usage": None, "latency_seconds": 2.0}
        jobs = []
        for size in (1, 2, 4, 8):
            for repeat, attempt in ((1, good), (2, bad)):
                jobs.append({"batch_size": size, "repeat": repeat,
                             "first_pass_valid": attempt["valid"], "eventual_valid": attempt["valid"],
                             "attempts": [deepcopy(attempt)], "elapsed_seconds": attempt["latency_seconds"]})
        raw = {"status": "COMPLETED", "jobs": jobs, "http_attempts": len(jobs)}
        captured = []
        with patch.object(runner, "frozen_cases", return_value=[case]), \
                patch.object(Path, "read_text", return_value=json.dumps(raw)), \
                patch.object(runner, "write_report", side_effect=lambda _path, data: captured.append(data)), \
                redirect_stdout(io.StringIO()):
            runner.summarize()
        report = captured[-1]
        for size in (1, 2, 4, 8):
            result = report["matrix"][str(size)]
            self.assertEqual(result["first_pass_rate"], 0.5)
            self.assertEqual(result["valid_first_case_outputs"], 1)
            self.assertEqual(result["primary_accuracy_valid_first"], 1.0)
            self.assertEqual(result["secondary_accuracy_valid_first"], 1.0)
            self.assertEqual(report["consistency"][str(size)]["both_runs_valid"], 0)
            self.assertEqual(report["consistency"][str(size)]["special_cases"]["h02-f"][1]["decision"], "ERROR")
        self.assertEqual(report["failure_taxonomy"]["first_attempts"]["invalid_json"], 4)


if __name__ == "__main__":
    unittest.main()
