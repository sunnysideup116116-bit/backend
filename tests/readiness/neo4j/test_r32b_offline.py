"""Hermetic fixed-v2 reasoning-isolation scheduling and failure-boundary checks."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_r32b_offline as runner


class R32BOfflineTests(unittest.TestCase):
    def cases(self):
        return [{"id": f"h{number:02}-{direction}", "Q": "Synthetic query",
                 "C": "Synthetic saved preference", "expected": "NO",
                 "relation": "role_mismatch", "category": "role_mismatch",
                 "language": "EN-EN"}
                for number in range(1, 49) for direction in ("f", "r")]

    def job(self, control="none"):
        return {"job_id": "r1-none-b001", "repeat": 1, "seed": 9401,
                "control": control, "batch_size": 2, "cases": self.cases()[:2]}

    def response(self, decision="NO", relation="role_mismatch", content=None,
                 finish="stop", model=None, reasoning="synthetic private reasoning"):
        rows = [{"id": case["id"], "decision": decision, "relation": relation}
                for case in self.job()["cases"]]
        return SimpleNamespace(model=runner.MODEL if model is None else model,
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=7,
                    unknown_provider_field="not-for-report"), provider_secret="not-for-report"),
            choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(
                content=json.dumps({"results": rows}) if content is None else content,
                reasoning_content=reasoning))], provider_secret="not-for-report")

    def evaluate(self, outputs, control="none", key="synthetic-only-test-credential-R32B"):
        calls, retained = [], []
        scripted = iter(outputs)

        def create(**kwargs):
            calls.append(deepcopy(kwargs))
            item = next(scripted)
            if isinstance(item, Exception):
                raise item
            return item

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with tempfile.TemporaryDirectory(prefix="r32b-hermetic-") as tmp, \
                patch.object(runner, "OUT", Path(tmp)), \
                patch.object(runner.time, "sleep") as sleep, \
                patch.object(runner, "write_report", side_effect=lambda path, data, secrets=():
                             retained.append((Path(path).name, deepcopy(data)))):
            result = runner.evaluate_r32b(self.job(control), client, "frozen synthetic prompt", key)
            sleeps = sleep.call_count
        return result, calls, retained, sleeps

    def test_frozen_96_v2_and_parser_hashes_are_verified(self):
        cases = runner.frozen_v2_cases()
        self.assertEqual(len(cases), 96)
        self.assertEqual(len({case["id"] for case in cases}), 96)
        self.assertEqual(runner.V2["r32_prompt_v2.txt"],
                         "db71bf2c677a6eef856484097141cd19a53c1a5a3a764a2587f501e031805666")
        self.assertEqual(runner.V2["R32_CONTRACT.md"],
                         "d14c2e115fe99374e23db11bb9b384fd7903e40446e024d9ecee4a982a9cd359")
        read_bytes = Path.read_bytes
        for name in (*runner.V2, *runner.PARSER_HASHES):
            def tampered(path):
                return b"changed" if path.name == name else read_bytes(path)

            with self.subTest(name=name), patch.object(Path, "read_bytes", tampered):
                with self.assertRaisesRegex(ValueError, "frozen"):
                    runner.frozen_v2_cases()

    def test_identical_three_seed_shuffle_for_every_reasoning_control(self):
        cases = self.cases()
        unchanged = deepcopy(cases)
        controls = ["none", "low", "default"]
        jobs = runner.schedule_r32b(cases, controls)
        self.assertEqual(jobs, runner.schedule_r32b(cases, controls))
        self.assertEqual(len(jobs), 432)
        self.assertEqual(len({job["job_id"] for job in jobs}), 432)
        self.assertEqual({job["batch_size"] for job in jobs}, {2})
        self.assertEqual(runner.SEEDS, (9401, 9402, 9403))
        per_seed = []
        for repeat, seed in enumerate(runner.SEEDS, 1):
            orderings = []
            for control in controls:
                selected = sorted((job for job in jobs if job["repeat"] == repeat
                                   and job["control"] == control), key=lambda job: job["job_id"])
                self.assertEqual(len(selected), 48)
                self.assertEqual({job["seed"] for job in selected}, {seed})
                ids = [case["id"] for job in selected for case in job["cases"]]
                self.assertEqual(len(set(ids)), 96)
                self.assertEqual(set(ids), {case["id"] for case in cases})
                orderings.append(ids)
            self.assertEqual(orderings[0], orderings[1])
            self.assertEqual(orderings[0], orderings[2])
            per_seed.append(tuple(orderings[0]))
        self.assertEqual(len(set(per_seed)), 3)
        self.assertEqual(cases, unchanged)

    def test_invalid_schedule_and_duplicate_cases_are_rejected(self):
        for controls in ([], ["none", "none"], ["medium"], ["default", "production"]):
            with self.subTest(controls=controls), self.assertRaises(ValueError):
                runner.schedule_r32b(self.cases(), controls)
        for cases in (self.cases()[:-1], self.cases() + self.cases()[:1],
                      self.cases()[:-1] + self.cases()[:1]):
            with self.subTest(count=len(cases)), self.assertRaises(ValueError):
                runner.schedule_r32b(cases, ["none"])

    def test_manifest_freezes_budget_and_production_stop(self):
        controls = ["none", "low", "default"]
        manifest = runner.manifest_r32b(runner.schedule_r32b(self.cases(), controls), controls)
        self.assertEqual(manifest["temperature"], 0)
        self.assertEqual(manifest["max_tokens"], 4096)
        self.assertEqual(manifest["batch_size"], 2)
        self.assertEqual(manifest["rounds"], 3)
        self.assertEqual(manifest["scheduled_requests"], 432)
        self.assertEqual(manifest["hard_attempt_budget"], 864)
        self.assertEqual(manifest["api_auto_retries"], 0)
        self.assertEqual(manifest["runtime_flags"], dict.fromkeys(runner.FLAGS, "off"))
        self.assertFalse(manifest["graph_access"])
        self.assertFalse(manifest["production_ready"])
        self.assertEqual(manifest["production_fingerprint"], "unknown")
        self.assertFalse(manifest["labels_changed"])

    def test_wire_controls_and_only_id_query_candidate_payload(self):
        for control in ("none", "low", "default"):
            with self.subTest(control=control):
                kwargs = runner.request_kwargs(self.job(control), "frozen v2 prompt")
                self.assertEqual(kwargs["model"], runner.MODEL)
                self.assertEqual(kwargs["temperature"], 0)
                self.assertEqual(kwargs["max_tokens"], 4096)
                self.assertEqual(kwargs["messages"][0], {"role": "system", "content": "frozen v2 prompt"})
                payload = json.loads(kwargs["messages"][1]["content"])
                self.assertEqual(set(payload), {"pairs"})
                self.assertEqual(len(payload["pairs"]), 2)
                self.assertTrue(all(set(row) == {"id", "Q", "C"} for row in payload["pairs"]))
                self.assertNotIn("reasoning_effort", kwargs)
                if control == "default":
                    self.assertNotIn("extra_body", kwargs)
                else:
                    self.assertEqual(kwargs["extra_body"], {"reasoning_effort": control})

    def test_valid_no_and_abstain_never_retry(self):
        for decision, relation in (("NO", "role_mismatch"), ("ABSTAIN", "unknown")):
            with self.subTest(decision=decision):
                result, calls, retained, sleeps = self.evaluate([self.response(decision, relation)])
                self.assertTrue(result["first_pass_valid"])
                self.assertTrue(result["eventual_valid"])
                self.assertEqual(len(calls), 1)
                self.assertEqual(sleeps, 0)
                self.assertEqual(retained, [])
                self.assertEqual({row["decision"] for row in result["attempts"][0]["decisions"].values()}, {decision})

    def test_legal_secondary_disagreement_does_not_retry_or_override_primary(self):
        result, calls, _, sleeps = self.evaluate([self.response("NO", "equivalent")])
        self.assertTrue(result["eventual_valid"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, 0)
        self.assertTrue(result["attempts"][0]["metadata"]["secondary_primary_disagreement"])
        self.assertEqual({row["decision"] for row in result["attempts"][0]["decisions"].values()}, {"NO"})

    def test_one_retry_for_length_empty_malformed_and_transient_failures(self):
        quota = RuntimeError("secret provider response must not be retained")
        quota.status_code = 429
        failures = [self.response(content="", finish="length"), self.response(content=""),
                    self.response(content="{bad json"), TimeoutError("private exception"),
                    ConnectionError("private exception"), quota]
        for index, failure in enumerate(failures):
            with self.subTest(failure=index):
                result, calls, _, sleeps = self.evaluate([failure, self.response()])
                self.assertFalse(result["first_pass_valid"])
                self.assertTrue(result["eventual_valid"])
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0], calls[1])
                self.assertEqual(sleeps, 1)
                self.assertEqual(result["attempts"][0]["decisions"], {})

    def test_double_length_remains_error_no_third_attempt_or_partial_salvage(self):
        result, calls, _, sleeps = self.evaluate([self.response(finish="length"),
                                                 self.response(finish="length")])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertFalse(result["eventual_valid"])
        self.assertEqual(sleeps, 1)
        for attempt in result["attempts"]:
            self.assertIn("truncated_response", attempt["errors"])
            self.assertEqual(attempt["decisions"], {})

    def test_permanent_provider_failures_and_internal_parser_errors_do_not_retry(self):
        failures = []
        for code in (400, 401, 403, 404, 422):
            error = RuntimeError("sensitive-provider-text")
            error.status_code = code
            failures.append(error)
        failures.append(SimpleNamespace(usage=None, choices=[]))
        for index, failure in enumerate(failures):
            with self.subTest(failure=index):
                result, calls, _, sleeps = self.evaluate([failure])
                self.assertFalse(result["eventual_valid"])
                self.assertEqual(len(calls), 1)
                self.assertEqual(sleeps, 0)
                self.assertNotIn("sensitive-provider-text", json.dumps(result))

    def test_report_keeps_allowlisted_metadata_not_model_identifier_or_reasoning(self):
        private = "provider-secret-like-metadata-not-for-retention"
        result, _, retained, _ = self.evaluate([self.response(model=private, reasoning=private)])
        self.assertNotIn(private, json.dumps(result))
        self.assertNotIn("not-for-report", json.dumps(result))
        self.assertEqual(retained, [])
        metadata = result["attempts"][0]["metadata"]
        self.assertFalse(metadata["model_matches_requested"])
        self.assertTrue(metadata["reasoning_present"])
        self.assertEqual(metadata["reasoning_char_count"], len(private))
        self.assertEqual(set(result["attempts"][0]["usage"]),
                         {"prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"})

    def test_secret_in_failed_output_is_not_retained(self):
        fake_key = "synthetic-only-test-credential-R32B"
        result, _, retained, _ = self.evaluate([self.response(content=fake_key),
                                                self.response()], key=fake_key)
        self.assertNotIn(fake_key, json.dumps(result))
        self.assertNotIn(fake_key, json.dumps(retained))
        self.assertEqual(retained, [])
        self.assertTrue(result["eventual_valid"])


if __name__ == "__main__":
    unittest.main()
