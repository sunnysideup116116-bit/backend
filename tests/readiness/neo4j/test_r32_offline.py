"""Hermetic R3.2 scheduling, IO boundaries and engineering-gate tests."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_r32_offline as runner


class R32OfflineTests(unittest.TestCase):
    def case(self, case_id="h01-f", expected="YES", relation="equivalent", category="equivalent"):
        return {"id": case_id, "Q": "Synthetic Query", "C": "Synthetic Candidate Concept",
                "expected": expected, "relation": relation, "category": category,
                "language": "EN-EN"}

    def probe(self):
        return {"status": "COMPLETED", "requested_model": "deepseek-v4.1-flash:cloud",
                "frozen_hashes": deepcopy(runner.FROZEN),
                "control_support": {"status": "OBSERVED_REASONING_OUTPUT_SUPPRESSION"},
                "jobs": [{"control": "none", "valid": True, "reasoning_char_count": 0}
                         for _ in range(4)]}

    def job(self, case, repeat, decision=None, relation=None, valid=True):
        row = {"id": case["id"], "decision": decision or case["expected"],
               "relation": relation or case["relation"]}
        attempt = {"valid": valid, "errors": [] if valid else ["empty_response"],
                   "decisions": {case["id"]: row} if valid else {},
                   "metadata": {"finish_reason": "stop"}, "latency_seconds": 1.0,
                   "usage": {"completion_tokens": 10}}
        return {"repeat": repeat, "attempts": [attempt], "first_pass_valid": valid,
                "eventual_valid": valid, "elapsed_seconds": 1.0}

    def test_schedule_expected_request_counts_and_frozen_shuffles(self):
        cases = [self.case(f"h{i:02}-{direction}") for i in range(1, 49) for direction in ("f", "r")]
        untouched = deepcopy(cases)
        for phase, count, repeats, controls in (
                ("generation", 288, 2, {"baseline", "none"}),
                ("development", 216, 3, {"none"})):
            with self.subTest(phase=phase):
                jobs = runner.schedule_r32(cases, phase)
                self.assertEqual(jobs, runner.schedule_r32(cases, phase))
                self.assertEqual(len(jobs), count)
                self.assertEqual(len({j["job_id"] for j in jobs}), count)
                self.assertEqual({j["control"] for j in jobs}, controls)
                self.assertEqual({j["batch_size"] for j in jobs}, {2, 4})
                for repeat in range(1, repeats + 1):
                    for control in controls:
                        for size in (2, 4):
                            selected = [j for j in jobs if j["repeat"] == repeat
                                        and j["control"] == control and j["batch_size"] == size]
                            ids = [c["id"] for j in selected for c in j["cases"]]
                            self.assertEqual(len(selected), 96 // size)
                            self.assertEqual(len(ids), 96)
                            self.assertEqual(len(set(ids)), 96)
                            self.assertEqual(set(ids), {c["id"] for c in cases})
        self.assertEqual(cases, untouched)
        with self.assertRaises(ValueError):
            runner.schedule_r32(cases, "production")

    def test_probe_rejects_http_success_only_or_inconclusive(self):
        runner.check_probe(self.probe())
        for status in (None, "HTTP_200", "SUPPORTED", "INCONCLUSIVE"):
            with self.subTest(status=status):
                probe = self.probe()
                probe["control_support"] = {"status": status, "http_status": 200}
                with self.assertRaises(ValueError):
                    runner.check_probe(probe)
        probe = self.probe()
        probe["control_support"] = {}
        with self.assertRaises(ValueError):
            runner.check_probe(probe)

    def test_probe_requires_exact_model_hashes_and_four_valid_suppressed_outputs(self):
        bad_reports = []
        for name, value in (("status", "RUNNING"), ("requested_model", "unverified-model"),
                            ("frozen_hashes", {})):
            p = self.probe(); p[name] = value; bad_reports.append(p)
        for count in (0, 3, 5):
            p = self.probe()
            p["jobs"] = [{"control": "none", "valid": True, "reasoning_char_count": 0}
                         for _ in range(count)]
            bad_reports.append(p)
        p = self.probe(); p["jobs"][0]["valid"] = False; bad_reports.append(p)
        p = self.probe(); p["jobs"][0]["reasoning_char_count"] = 2; bad_reports.append(p)
        for probe in bad_reports:
            with self.subTest(probe_case=bad_reports.index(probe)):
                with self.assertRaises(ValueError):
                    runner.check_probe(probe)

    def test_error_stays_error_and_positive_error_stays_in_recall_denominator(self):
        cases = [self.case(), self.case("h02-f", expected="NO", relation="role_mismatch")]
        jobs = [self.job(c, repeat, valid=repeat == 1) for repeat in (1, 2) for c in cases]
        result = runner.condition_summary(jobs, cases, "development")
        self.assertEqual(result["final_case_coverage"], 2)
        self.assertEqual(result["expected_case_outputs"], 4)
        self.assertEqual(result["final_per_run"][1]["primary_confusion"]["YES"]["ERROR"], 1)
        self.assertEqual(result["final_per_run"][1]["primary_confusion"]["NO"]["ERROR"], 1)
        self.assertEqual(result["final_per_run"][1]["primary_confusion"]["NO"]["NO"], 0)
        self.assertEqual(result["final_pooled_yes"]["TP"], 1)
        self.assertEqual(result["final_pooled_yes"]["FN"], 1)
        self.assertEqual(result["final_pooled_yes"]["recall"], 0.5)
        self.assertEqual(result["final_pooled_yes"]["TN"], 1,
                         "An ERROR is fail-closed non-admission, not a true semantic negative")
        self.assertEqual(result["primary_consistency_all_cases"], 0)
        self.assertFalse(result["all_gates_pass"])

    def test_primary_metrics_are_not_overridden_by_legal_secondary_disagreement(self):
        cases = [self.case(), self.case("h02-f", "NO", "role_mismatch"),
                 self.case("h03-f", "ABSTAIN", "unknown", "ambiguity")]
        jobs = []
        for repeat in (1, 2, 3):
            jobs.extend([self.job(cases[0], repeat, relation="role_mismatch"),
                         self.job(cases[1], repeat, relation="equivalent"),
                         self.job(cases[2], repeat, relation="equivalent")])
        result = runner.condition_summary(jobs, cases, "development")
        self.assertEqual(result["final_pooled_yes"]["TP"], 3)
        self.assertEqual(result["final_pooled_yes"]["FP"], 0)
        self.assertEqual(result["unknown_handling"], {"ABSTAIN": 3})
        self.assertEqual(result["primary_accuracy_valid_final"], 1)
        self.assertEqual(result["role_false_yes"], 0)

    def test_95_of_96_primary_consistency_does_not_round_up_to_99_percent(self):
        cases = [self.case(f"h{i:02}-f") for i in range(1, 97)]
        jobs = [self.job(c, r) for r in (1, 2, 3) for c in cases]
        jobs[-1] = self.job(cases[-1], 3, decision="NO", relation="candidate_broader_insufficient")
        result = runner.condition_summary(jobs, cases, "development")
        self.assertEqual(result["primary_consistent_cases"], 95)
        self.assertEqual(result["primary_consistency_all_cases"], 95 / 96)
        self.assertFalse(result["proposed_engineering_gates"]["primary_consistency_ge_99"])
        self.assertFalse(result["all_gates_pass"])

    def test_undefined_yes_precision_never_passes_gate(self):
        case = self.case(expected="NO", relation="role_mismatch")
        result = runner.condition_summary([self.job(case, r) for r in (1, 2, 3)], [case], "development")
        self.assertIsNone(result["final_pooled_yes"]["precision"])
        self.assertFalse(result["proposed_engineering_gates"]["yes_precision_ge_99"])
        self.assertFalse(result["all_gates_pass"])

    def test_role_and_constraint_false_yes_block_gate(self):
        cases = [self.case(), self.case("h02-f", "NO", "role_mismatch", "role_mismatch"),
                 self.case("h03-f", "NO", "constraint_conflict", "constraint_conflict")]
        jobs = [self.job(c, r, decision="YES", relation="equivalent") for r in (1, 2) for c in cases]
        result = runner.condition_summary(jobs, cases, "development")
        self.assertEqual(result["role_false_yes"], 2)
        self.assertEqual(result["constraint_false_yes"], 2)
        self.assertFalse(result["proposed_engineering_gates"]["role_constraint_zero_false_yes"])
        self.assertFalse(result["all_gates_pass"])

    def test_first_pass_and_one_retry_metrics_remain_separate(self):
        case = self.case()
        jobs = [self.job(case, r) for r in (1, 2, 3)]
        failed = self.job(case, 1, valid=False)["attempts"][0]
        failed["errors"] = ["empty_response", "truncated_response"]
        failed["metadata"]["finish_reason"] = "length"
        jobs[0]["attempts"].insert(0, failed)
        jobs[0]["first_pass_valid"] = False
        result = runner.condition_summary(jobs, [case], "development")
        self.assertEqual(result["first_pass_rate"], 2 / 3)
        self.assertEqual(result["eventual_rate"], 1)
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(result["first_truncation_rate"], 1 / 3)
        self.assertEqual(result["finish_reasons"], {"length": 1, "stop": 3})
        self.assertFalse(result["proposed_engineering_gates"]["first_pass_ge_99"])
        self.assertTrue(result["proposed_engineering_gates"]["eventual_ge_999_or_sample_100"])

    def test_perfect_sample_can_pass_engineering_gate_not_production_gate(self):
        cases = [self.case(), self.case("h02-f", "NO", "role_mismatch")]
        jobs = [self.job(c, r) for r in (1, 2, 3) for c in cases]
        result = runner.condition_summary(jobs, cases, "development")
        self.assertTrue(result["all_gates_pass"])
        self.assertIn("development only", result["scope"])

    def test_stats_p95_uses_nearest_rank_and_empty_is_unavailable(self):
        stats = runner.sample_stats(list(range(1, 21)))
        self.assertEqual(stats["p95_nearest_rank"], 19)
        self.assertEqual(stats["median"], 10.5)
        self.assertIsNone(runner.sample_stats([]))

    def test_actual_runner_sends_only_ids_and_concepts_never_human_labels(self):
        case = self.case(expected="NO", relation="role_mismatch")
        calls, snapshots, closed = [], [], []
        fake_key = "synthetic-test-only-credential-K33P"
        fake_probe = self.probe()

        def fake_config(_path, names):
            values = {"LLM_API_KEY": fake_key, "LLM_BASE_URL": "https://ollama.com/v1",
                      "LLM_MODEL_ID": "deepseek-v4.1-flash:cloud"}
            return {name: values[name] for name in names}

        def fake_http(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            return SimpleNamespace(**kwargs)

        def fake_openai(**kwargs):
            self.assertEqual(kwargs["max_retries"], 0)
            self.assertEqual(kwargs["timeout"], 60)

            def create(**request):
                event = SimpleNamespace(method="POST", url=SimpleNamespace(
                    scheme="https", host="ollama.com", path="/v1/chat/completions",
                    port=None, query=b"", userinfo=b""))
                for hook in kwargs["http_client"].event_hooks["request"]:
                    hook(event)
                calls.append(deepcopy(request))
                row = {"id": case["id"], "decision": "NO", "relation": "role_mismatch"}
                return SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason="stop",
                    message=SimpleNamespace(content=json.dumps({"results": [row]})))])

            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                                   close=lambda: closed.append(True))

        def capture(path, report, secrets=()):
            self.assertNotIn(fake_key, json.dumps(report))
            snapshots.append((Path(path).name, deepcopy(report)))

        for phase, control in (("generation", "baseline"), ("development", "none")):
            calls.clear(); snapshots.clear(); closed.clear()
            job = {"job_id": "synthetic-job", "repeat": 1, "control": control,
                   "batch_size": 2, "cases": [case]}
            manifest = {"prompt_file": "synthetic-prompt.txt", "hard_attempt_budget": 2}
            with tempfile.TemporaryDirectory(prefix="r32-hermetic-test-") as tmp, ExitStack() as stack:
                stack.enter_context(patch.dict(sys.modules, {
                    "httpx": SimpleNamespace(Client=fake_http), "openai": SimpleNamespace(OpenAI=fake_openai)}))
                stack.enter_context(patch.object(runner, "frozen_cases", return_value=[case]))
                stack.enter_context(patch.object(runner, "schedule_r32", return_value=[job]))
                stack.enter_context(patch.object(runner, "experiment_manifest", return_value=manifest))
                stack.enter_context(patch.object(runner, "selected_config", side_effect=fake_config))
                stack.enter_context(patch.object(runner, "write_report", side_effect=capture))
                stack.enter_context(patch.object(runner, "OUT", Path(tmp)))
                stack.enter_context(patch.object(Path, "read_text", side_effect=lambda *a, **k:
                    json.dumps(fake_probe)))
                stack.enter_context(redirect_stdout(io.StringIO()))
                runner.run(SimpleNamespace(action=phase, matchmaker_config="not-read", model_config="not-read"))
            self.assertEqual(len(calls), 1, "A valid NO must not trigger retry")
            self.assertEqual(closed, [True])
            request = calls[0]
            payload = json.loads(request["messages"][1]["content"])
            self.assertEqual(set(payload), {"pairs"})
            self.assertEqual(set(payload["pairs"][0]), {"id", "Q", "C"})
            self.assertEqual(request["max_tokens"], 4096)
            self.assertEqual(request["temperature"], 0)
            if control == "none":
                self.assertEqual(request["extra_body"], {"reasoning_effort": "none"})
                self.assertNotIn("reasoning_effort", request)
            else:
                self.assertNotIn("reasoning_effort", request)
                self.assertNotIn("extra_body", request)
            final = [r for name, r in snapshots if name == phase + ".json"][-1]
            self.assertEqual(final["status"], "COMPLETED")
            self.assertFalse(final["graph_access"])
            self.assertEqual(final["http_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
