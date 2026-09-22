"""Hermetic fixed-input relation-only runner tests; no credentials/provider/Graph."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_r32c_offline as runner


class R32cOfflineTests(unittest.TestCase):
    def job(self):
        cases = {c["id"]: c for c in runner.load_complete_cases()}
        return {"job_id": "r1-default-b001", "repeat": 1, "seed": 9401,
                "control": "default", "batch_size": 2,
                "cases": [cases["h11-r"], cases["h47-r"]]}

    def response(self, kind="candidate_more_broad", content=None, finish="stop", model=None):
        rows = [{"id": c["id"], "relation": kind} for c in self.job()["cases"]]
        return SimpleNamespace(model=runner.MODEL if model is None else model,
            choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(
                content=json.dumps({"results": rows}) if content is None else content,
                reasoning_content="synthetic hidden reasoning not for report"))],
            usage=SimpleNamespace(prompt_tokens=90, completion_tokens=40, total_tokens=130,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=7,
                    internal_secret="not-for-report"), internal_secret="not-for-report"))

    def evaluate(self, outputs, key="synthetic-fake-credential-only-r32c"):
        calls, retained = [], []
        scripted = iter(outputs)

        def create(**kwargs):
            calls.append(deepcopy(kwargs))
            item = next(scripted)
            if isinstance(item, Exception):
                raise item
            return item

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        # Replace this runner's clock binding, not the shared stdlib time module.
        # Other regression modules may import services with background threads;
        # their sleep calls must not contaminate this runner's retry observation.
        sleep = Mock()
        clock = SimpleNamespace(perf_counter=runner.time.perf_counter, sleep=sleep)
        with tempfile.TemporaryDirectory(prefix="r32c-runner-test-") as tmp, \
                patch.object(runner, "OUT", Path(tmp)), \
                patch.object(runner, "time", clock), \
                patch.object(runner, "write_report", side_effect=lambda path, data, secrets=():
                    retained.append((Path(path).name, deepcopy(data)))):
            result = runner.evaluate_relation_batch(self.job(), client, "frozen relation-only prompt", key)
            sleep_count = sleep.call_count
        return result, calls, retained, sleep_count

    def test_snapshot_freezes_three_round_default_schedule_and_bounds(self):
        snapshot, cases, jobs = runner.experiment_snapshot()
        self.assertEqual(len(cases), 96)
        self.assertEqual(len(jobs), 144)
        self.assertEqual(len({j["job_id"] for j in jobs}), 144)
        self.assertEqual({j["control"] for j in jobs}, {"default"})
        self.assertEqual({j["batch_size"] for j in jobs}, {2})
        for repeat, seed in enumerate((9401, 9402, 9403), 1):
            selected = [j for j in jobs if j["repeat"] == repeat]
            self.assertEqual(len(selected), 48)
            self.assertEqual({j["seed"] for j in selected}, {seed})
            self.assertEqual(sorted(c["id"] for j in selected for c in j["cases"]),
                             sorted(c["id"] for c in cases))
        self.assertEqual(snapshot["reasoning_effort"], "omitted/default")
        self.assertEqual(snapshot["temperature"], 0)
        self.assertEqual(snapshot["max_tokens"], 4096)
        self.assertEqual(snapshot["hard_attempt_budget"], 288)
        self.assertEqual(snapshot["sdk_retries"], 0)
        self.assertEqual(snapshot["timeout_seconds"], 60)
        self.assertEqual(snapshot["workers"], 3)
        self.assertEqual(snapshot["runtime_flags"], dict.fromkeys(runner.FLAGS, "off"))
        self.assertFalse(snapshot["graph_access"])
        self.assertFalse(snapshot["production_ready"])
        self.assertEqual(snapshot["production_fingerprint"], "unknown")
        self.assertFalse(snapshot["primary_gold_changed"])
        self.assertEqual(runner.experiment_snapshot(), (snapshot, cases, jobs))

    def test_wire_keeps_complete_qualifiers_and_only_id_q_c(self):
        request = runner.relation_request(self.job(), "fixed prompt")
        self.assertEqual(set(request), {"model", "temperature", "max_tokens", "messages"})
        self.assertEqual(request["model"], runner.MODEL)
        self.assertEqual(request["temperature"], 0)
        self.assertEqual(request["max_tokens"], 4096)
        self.assertEqual(request["messages"][0], {"role": "system", "content": "fixed prompt"})
        pairs = json.loads(request["messages"][1]["content"])["pairs"]
        self.assertEqual(len(pairs), 2)
        self.assertTrue(all(set(c) == {"id", "Q", "C"} for c in pairs))
        self.assertEqual(pairs[0]["Q"], "Watching Waterfalls on Short Accessible Trails")
        self.assertEqual(pairs[1]["Q"], "Visiting Quiet Castles without Guided Groups")
        self.assertNotIn("expected", request["messages"][1]["content"])
        self.assertNotIn("source_relation", request["messages"][1]["content"])

    def test_prepare_never_loads_secret_or_initializes_provider(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="r32c-prepare-test-") as tmp, \
                patch.object(runner, "OUT", Path(tmp)), \
                patch.object(runner, "selected_config", side_effect=AssertionError("must not load env")) as config, \
                patch.dict(sys.modules, {"openai": None, "httpx": None, "neo4j": None}), \
                redirect_stdout(output):
            runner.prepare_r32c()
            manifest = json.loads((Path(tmp) / "manifest.json").read_text())
            inputs = json.loads((Path(tmp) / "inputs.json").read_text())
            self.assertEqual(inputs, {"synthetic_only": True, "cases": runner.load_complete_cases()})
            self.assertEqual(manifest, runner.experiment_snapshot()[0])
            with self.assertRaisesRegex(ValueError, "refusing_to_overwrite"):
                runner.prepare_r32c()
            config.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["status"], "FROZEN_BEFORE_SCORING")

    def test_changed_source_or_complete_input_invalidates_prescoring_manifest(self):
        snapshot, cases, _ = runner.experiment_snapshot()
        changed_cases = deepcopy(cases)
        changed_cases[0]["Q"] += " qualifier"
        with patch.object(runner, "load_complete_cases", return_value=changed_cases):
            changed_snapshot = runner.experiment_snapshot()[0]
        self.assertNotEqual(changed_snapshot["expanded_cases_sha256"], snapshot["expanded_cases_sha256"])
        original_read = Path.read_bytes

        def changed_source(path):
            return b"changed offline prompt" if path.name == "r32c_prompt_v3.txt" else original_read(path)

        with patch.object(Path, "read_bytes", changed_source):
            changed_source_snapshot = runner.experiment_snapshot()[0]
        self.assertNotEqual(changed_source_snapshot["hashes"], snapshot["hashes"])
        args = SimpleNamespace(model_config="not-read", matchmaker_config="not-read")
        for changed in (changed_snapshot, changed_source_snapshot):
            with tempfile.TemporaryDirectory(prefix="r32c-freeze-test-") as tmp, \
                    patch.object(runner, "OUT", Path(tmp)), \
                    patch.object(runner, "selected_config") as config, \
                    patch.object(runner, "experiment_snapshot", return_value=(changed, cases, [])), \
                    patch.dict(sys.modules, {"httpx": SimpleNamespace(),
                        "openai": SimpleNamespace(OpenAI=lambda **kwargs: self.fail("no provider calls"))}):
                runner.write_report(Path(tmp) / "manifest.json", snapshot)
                with self.assertRaisesRegex(ValueError, "prescoring_freeze_changed"):
                    runner.run_r32c(args)
                config.assert_not_called()

    def test_existing_matrix_never_overwritten_before_config(self):
        snapshot, cases, jobs = runner.experiment_snapshot()
        with tempfile.TemporaryDirectory(prefix="r32c-existing-test-") as tmp, \
                patch.object(runner, "OUT", Path(tmp)), \
                patch.object(runner, "selected_config") as config, \
                patch.dict(sys.modules, {"httpx": SimpleNamespace(), "openai": SimpleNamespace(OpenAI=None)}):
            runner.write_report(Path(tmp) / "manifest.json", snapshot)
            runner.write_report(Path(tmp) / "matrix.json", {"frozen": True})
            with self.assertRaisesRegex(ValueError, "refusing_to_overwrite"):
                runner.run_r32c(SimpleNamespace(model_config="not-read", matchmaker_config="not-read"))
            config.assert_not_called()
            self.assertEqual(json.loads((Path(tmp) / "matrix.json").read_text()), {"frozen": True})

    def test_valid_mapped_no_and_abstain_never_retry(self):
        for kind, primary in (("candidate_more_broad", "NO"), ("role_mismatch", "NO"),
                              ("constraint_conflict", "NO"), ("unknown", "ABSTAIN"),
                              ("lexical_ambiguity", "ABSTAIN")):
            with self.subTest(relation=kind):
                result, calls, retained, sleeps = self.evaluate([self.response(kind)])
                self.assertTrue(result["first_pass_valid"])
                self.assertTrue(result["eventual_valid"])
                self.assertEqual(len(calls), 1)
                self.assertEqual(sleeps, 0)
                self.assertEqual(retained, [])
                self.assertEqual({row["decision"] for row in result["attempts"][0]["decisions"].values()}, {primary})

    def test_model_decision_override_is_schema_failure_not_acceptance(self):
        rows = [{"id": c["id"], "relation": "candidate_more_broad", "decision": "YES"}
                for c in self.job()["cases"]]
        bad = self.response(content=json.dumps({"results": rows}))
        result, calls, _, sleeps = self.evaluate([bad, self.response()])
        self.assertFalse(result["first_pass_valid"])
        self.assertEqual(result["attempts"][0]["decisions"], {})
        self.assertIn("extra_unexpected_fields", result["attempts"][0]["errors"])
        self.assertTrue(result["eventual_valid"])
        self.assertEqual({row["decision"] for row in result["attempts"][-1]["decisions"].values()}, {"NO"})
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(sleeps, 1)

    def test_only_one_identical_retry_for_malformed_empty_length_and_transient(self):
        quota = RuntimeError("must not log secret provider error")
        quota.status_code = 429
        failures = [self.response(content="{broken"), self.response(content=""),
                    self.response(finish="length"), TimeoutError("private error"),
                    ConnectionError("private error"), quota]
        for index, failure in enumerate(failures):
            with self.subTest(failure=index):
                result, calls, retained, sleeps = self.evaluate([failure, self.response()])
                self.assertFalse(result["first_pass_valid"])
                self.assertTrue(result["eventual_valid"])
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0], calls[1])
                self.assertEqual(sleeps, 1)
                self.assertNotIn("private error", json.dumps((result, retained)))
                self.assertNotIn("must not log secret provider error", json.dumps((result, retained)))

    def test_failed_retry_remains_error_and_no_partial_salvage(self):
        result, calls, _, sleeps = self.evaluate([self.response(finish="length"),
                                                self.response(finish="length")])
        self.assertFalse(result["eventual_valid"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, 1)
        for attempt in result["attempts"]:
            self.assertEqual(attempt["decisions"], {})
            self.assertIn("truncated_response", attempt["errors"])

    def test_permanent_provider_and_internal_parser_failures_do_not_retry(self):
        failures = [SimpleNamespace(usage=None, choices=[])]
        for status in (400, 401, 403, 404, 422):
            failure = RuntimeError("private provider failure")
            failure.status_code = status
            failures.append(failure)
        for index, failure in enumerate(failures):
            with self.subTest(failure=index):
                result, calls, retained, sleeps = self.evaluate([failure])
                self.assertFalse(result["eventual_valid"])
                self.assertEqual(len(calls), 1)
                self.assertEqual(sleeps, 0)
                self.assertNotIn("private provider failure", json.dumps((result, retained)))

    def test_unexpected_finish_never_retries_even_when_empty(self):
        for finish in ("content_filter", "tool_calls", "function_call", "unknown", None):
            for content in (None, ""):
                with self.subTest(finish=finish, empty=content == ""):
                    result, calls, _, sleeps = self.evaluate([
                        self.response(content=content, finish=finish)])
                    self.assertFalse(result["first_pass_valid"])
                    self.assertFalse(result["eventual_valid"])
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(sleeps, 0)
                    self.assertIn("unexpected_finish_reason", result["attempts"][0]["errors"])
                    self.assertEqual(result["attempts"][0]["decisions"], {})

    def test_allowlisted_usage_metadata_never_stores_reasoning_or_provider_metadata(self):
        result, _, retained, _ = self.evaluate([self.response()])
        serialized = json.dumps((result, retained))
        self.assertNotIn("synthetic hidden reasoning", serialized)
        self.assertNotIn("not-for-report", serialized)
        metadata = result["attempts"][0]["metadata"]
        self.assertTrue(metadata["reasoning_present"])
        self.assertTrue(metadata["model_matches_requested"])
        self.assertEqual(set(result["attempts"][0]["usage"]),
                         {"prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"})

    def test_failed_output_containing_secret_not_retained(self):
        key = "synthetic-fake-credential-only-r32c"
        result, calls, retained, _ = self.evaluate([self.response(content=key), self.response()], key=key)
        self.assertEqual(len(calls), 2)
        self.assertEqual(retained, [])
        self.assertNotIn(key, json.dumps(result))

    def test_cli_permission_and_off_flags_gate_before_provider(self):
        for arguments, flags in ((["run"], dict.fromkeys(runner.FLAGS, "off")),
                                 (["prepare"], {runner.FLAGS[0]: "active"})):
            with self.subTest(arguments=arguments, flags=flags), \
                    patch.object(sys, "argv", ["run_r32c_offline.py", *arguments]), \
                    patch.dict(runner.os.environ, flags), \
                    patch.object(runner, "run_r32c") as run, \
                    patch.object(runner, "prepare_r32c") as prepare, \
                    patch.object(runner.logging, "disable"), \
                    patch.object(runner.warnings, "filterwarnings"), \
                    redirect_stdout(io.StringIO()), \
                    patch.object(sys, "stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    runner.main()
                run.assert_not_called()
                prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
