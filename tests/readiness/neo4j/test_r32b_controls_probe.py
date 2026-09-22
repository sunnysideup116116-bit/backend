"""Hermetic R3.2b probe tests; no real keys, network or Graph access."""
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
import r32b_controls_probe as probe


class R32bControlsProbeTests(unittest.TestCase):
    def cases(self):
        return [{"id": case_id, "Q": "Synthetic Query", "C": "Synthetic Candidate",
                 "expected": "NO", "relation": "role_mismatch"}
                for ids in probe.BATCH_IDS for case_id in ids]

    def response(self, ids, reasoning="", content=None, model=probe.MODEL):
        if content is None:
            content = json.dumps({"results": [
                {"id": case_id, "decision": "NO", "relation": "role_mismatch"} for case_id in ids]})
        return SimpleNamespace(model=model, choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content=content, reasoning_content=reasoning))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=40, total_tokens=140,
                                  completion_tokens_details=None))

    def fake_run(self, outcome=None, invalid=False):
        calls, closed, config_calls = [], [], []
        fake_key = "offline-only-fake-credential-Z9XZ"

        def config(_path, names):
            config_calls.append(names)
            values = {"LLM_API_KEY": fake_key, "LLM_BASE_URL": probe.BASE_URL,
                      "LLM_MODEL_ID": probe.MODEL}
            return {name: values[name] for name in names}

        def http_client(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            return SimpleNamespace(**kwargs)

        def openai_client(**kwargs):
            self.assertEqual(kwargs["max_retries"], 0)
            self.assertEqual(kwargs["timeout"], 60)

            # Old SDK signature: a direct reasoning_effort keyword is rejected.
            def create(*, model, temperature, max_tokens, messages, extra_body=None):
                request = {"model": model, "temperature": temperature,
                           "max_tokens": max_tokens, "messages": messages}
                if extra_body is not None:
                    request["extra_body"] = extra_body
                event = SimpleNamespace(method="POST", url=SimpleNamespace(scheme="https",
                    host="ollama.com", path="/v1/chat/completions", port=None, query=b"", userinfo=b""))
                for hook in kwargs["http_client"].event_hooks["request"]:
                    hook(event)
                calls.append(deepcopy(request))
                ids = [row["id"] for row in json.loads(messages[1]["content"])["pairs"]]
                if outcome is not None:
                    return outcome(request, ids)
                return self.response(ids, reasoning="" if extra_body == {"reasoning_effort": "none"}
                                     else "not persisted synthetic reasoning")
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                                   close=lambda: closed.append(True))

        def capture(_path, data, secrets=()):
            serialized = json.dumps(data)
            for forbidden in (fake_key, fake_key[:6], fake_key[-4:], "Synthetic Query",
                              "Synthetic Candidate", "not persisted synthetic reasoning"):
                self.assertNotIn(forbidden, serialized)

        output = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="r32b-probe-test-") as temporary, ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {
                "httpx": SimpleNamespace(Client=http_client),
                "openai": SimpleNamespace(OpenAI=openai_client)}))
            stack.enter_context(patch.object(probe, "frozen_cases", return_value=self.cases()))
            stack.enter_context(patch.object(probe, "selected_config", side_effect=config))
            stack.enter_context(patch.object(probe, "write_report", side_effect=capture))
            stack.enter_context(patch.object(probe, "OUT", Path(temporary) / "probe.json"))
            stack.enter_context(patch.dict(probe.os.environ, dict.fromkeys(probe.FLAGS, "off")))
            stack.enter_context(redirect_stdout(output))
            report = probe.run_r32b_control_probe(SimpleNamespace(model_config="not-read",
                matchmaker_config="not-read", confirm_synthetic_provider_use=True,
                include_invalid_effort=invalid))
        self.assertEqual(closed, [True])
        self.assertEqual(config_calls, [{"LLM_API_KEY", "LLM_BASE_URL"}, {"LLM_MODEL_ID"}])
        self.assertEqual(len(calls), 7 if invalid else 6)
        self.assertEqual(report["http_attempts"], len(calls))
        self.assertEqual(report["status"], "COMPLETED")
        for line in output.getvalue().splitlines():
            self.assertEqual(set(json.loads(line)), {"probe_id", "status"})
        return calls, report

    def test_schedule_is_two_fixed_two_case_batches(self):
        jobs = probe.create_r32b_probe_jobs(self.cases())
        self.assertEqual(len(jobs), 6)
        self.assertEqual(len({j["probe_id"] for j in jobs}), 6)
        for j in jobs:
            self.assertEqual(j["batch_size"], 2)
            self.assertEqual(j["payload"], probe.create_r32b_probe_jobs(self.cases())[jobs.index(j)]["payload"])
            self.assertTrue(all(set(row) == {"id", "Q", "C"} for row in j["payload"]))

    def test_only_effort_changes_wire_and_effect_is_unknown(self):
        calls, report = self.fake_run()
        for offset in (0, 3):
            comparable = []
            for request in calls[offset:offset + 3]:
                value = deepcopy(request)
                value.pop("extra_body", None)
                comparable.append(value)
                self.assertEqual(request["temperature"], 0)
                self.assertEqual(request["max_tokens"], 4096)
                self.assertNotIn("reasoning_effort", request)
                self.assertNotIn("response_format", request)
            self.assertEqual(comparable[0], comparable[1])
            self.assertEqual(comparable[1], comparable[2])
        self.assertEqual(probe.matrix_controls_from_probe(report), ("none", "low", "default"))
        self.assertEqual(report["control_support"]["effective_effort"], "unknown")
        self.assertFalse(report["control_support"]["hidden_compute_enforcement_confirmed"])

    def test_no_observable_reasoning_does_not_claim_low_enforcement(self):
        _, report = self.fake_run(lambda _request, ids: self.response(ids))
        self.assertEqual(report["control_support"]["status"], "WIRE_ACCEPTED_EFFECT_UNKNOWN")
        self.assertEqual(report["control_support"]["observations"]["low"]["reasoning_present_count"], 0)

    def test_http_response_with_malformed_json_is_accepted_not_protocol_valid(self):
        _, report = self.fake_run(lambda _request, ids: self.response(ids, content="not-json"))
        self.assertEqual(report["control_support"]["status"], "WIRE_ACCEPTED_EFFECT_UNKNOWN")
        self.assertEqual(report["control_support"]["low_protocol_valid_count"], 0)
        self.assertTrue(all(j["errors"] == ["invalid_json"] for j in report["jobs"]))

    def test_low_contract_rejected_skips_low_and_never_reads_error_text(self):
        class ProviderFailure(Exception):
            status_code = 400
            def __str__(self):
                raise AssertionError("provider error text must not be read")
        def outcome(request, ids):
            if request.get("extra_body") == {"reasoning_effort": "low"}:
                raise ProviderFailure()
            return self.response(ids)
        _, report = self.fake_run(outcome)
        self.assertEqual(probe.matrix_controls_from_probe(report), ("none", "default"))

    def test_api_auth_or_transient_failure_is_inconclusive_not_unsupported(self):
        for code in (401, 429, 503):
            with self.subTest(code=code):
                class ProviderFailure(Exception):
                    status_code = code
                def outcome(request, ids):
                    if request.get("extra_body") == {"reasoning_effort": "low"}:
                        raise ProviderFailure()
                    return self.response(ids)
                _, report = self.fake_run(outcome)
                with self.assertRaisesRegex(ValueError, "inconclusive"):
                    probe.matrix_controls_from_probe(report)

    def test_optional_invalid_effort_is_seventh_no_retry(self):
        calls, report = self.fake_run(invalid=True)
        self.assertEqual(calls[-1]["extra_body"], {"reasoning_effort": probe.INVALID_EFFORT})
        self.assertEqual(report["hard_request_budget"], 7)
        self.assertEqual(probe.matrix_controls_from_probe(report), ("none", "default"))
        self.assertEqual(report["control_support"]["status"], "INCONCLUSIVE")
        self.assertFalse(report["control_support"]["requested_low_matrix_allowed"])
        self.assertEqual(report["control_support"]["invalid_effort_negative_control"],
                         "ACCEPTED_MAPPING_ENFORCEMENT_UNVERIFIED")

    def test_low_skip_does_not_salvage_unverified_none_default_controls(self):
        _, report = self.fake_run(lambda _request, ids: self.response(ids), invalid=True)
        with self.assertRaisesRegex(ValueError, "inconclusive"):
            probe.matrix_controls_from_probe(report)
        _, report = self.fake_run(invalid=True)
        report["jobs"] = [j for j in report["jobs"] if j["probe_id"] != "r32b-b2-default"]
        report["control_support"] = probe.summarize_r32b_probe_support(report["jobs"])
        with self.assertRaisesRegex(ValueError, "inconclusive"):
            probe.matrix_controls_from_probe(report)

    def test_invalid_rejection_and_low_acceptance_allow_requested_low_only(self):
        class ProviderFailure(Exception):
            status_code = 400
        def outcome(request, ids):
            if request.get("extra_body") == {"reasoning_effort": probe.INVALID_EFFORT}:
                raise ProviderFailure()
            return self.response(ids)
        _, report = self.fake_run(outcome, invalid=True)
        self.assertEqual(probe.matrix_controls_from_probe(report), probe.CONTROLS)
        self.assertEqual(report["control_support"]["effective_effort"], "unknown")
        self.assertEqual(report["control_support"]["invalid_effort_negative_control"],
                         "REJECTED_PARAMETER_VALIDATION_OBSERVED")

    def test_v2_secondary_cannot_override_primary_or_validity(self):
        def outcome(_request, ids):
            text = json.dumps({"results": [
                {"id": case_id, "decision": "NO", "relation": "equivalent"} for case_id in ids]})
            return self.response(ids, content=text)
        _, report = self.fake_run(outcome)
        self.assertTrue(all(j["valid"] for j in report["jobs"]))

    def test_report_mutation_or_unexpected_model_fails_closed(self):
        _, report = self.fake_run()
        for changes in ({"frozen_hashes": {}}, {"temperature": 1}, {"max_tokens": 8192},
                        {"status": "RUNNING"}, {"runtime_flags": {}}, {"control_support": {}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                probe.matrix_controls_from_probe({**report, **changes})
        _, wrong = self.fake_run(lambda _request, ids: self.response(ids, model="wrong-model"))
        with self.assertRaises(ValueError):
            probe.matrix_controls_from_probe(wrong)

    def test_endpoint_and_hard_attempt_budget(self):
        good = dict(scheme="https", host="ollama.com", path="/v1/chat/completions",
                    port=None, query=b"", userinfo=b"")
        for changes in ({"host": "evil.test"}, {"scheme": "http"}, {"port": 8443},
                        {"path": "/api/chat"}, {"query": b"x=y"}, {"userinfo": b"u"}):
            with self.subTest(changes=changes):
                counter = {"http_attempts": 0, "hard_request_budget": 7}
                with self.assertRaises(ValueError):
                    probe.r32b_probe_request_guard(counter, SimpleNamespace(method="POST",
                        url=SimpleNamespace(**{**good, **changes})))
                self.assertEqual(counter["http_attempts"], 0)
        counter = {"http_attempts": 0, "hard_request_budget": 6}
        event = SimpleNamespace(method="POST", url=SimpleNamespace(**good))
        for _ in range(6):
            probe.r32b_probe_request_guard(counter, event)
        with self.assertRaises(ValueError):
            probe.r32b_probe_request_guard(counter, event)

    def test_permission_flags_and_frozen_hash_block_before_config(self):
        args = SimpleNamespace(model_config="not-read", matchmaker_config="not-read",
            confirm_synthetic_provider_use=False, include_invalid_effort=False)
        with patch.object(probe, "selected_config") as config:
            with self.assertRaises(ValueError):
                probe.run_r32b_control_probe(args)
            args.confirm_synthetic_provider_use = True
            with patch.dict(probe.os.environ, {probe.FLAGS[0]: "active"}):
                with self.assertRaises(ValueError):
                    probe.run_r32b_control_probe(args)
            with patch.dict(probe.os.environ, dict.fromkeys(probe.FLAGS, "off")), \
                 patch.object(probe, "FROZEN_R32B", {"r32_prompt_v2.txt": "wrong"}), \
                 tempfile.TemporaryDirectory(prefix="r32b-frozen-test-") as temporary, \
                 patch.object(probe, "OUT", Path(temporary) / "not-created.json"):
                with self.assertRaisesRegex(ValueError, "frozen_input_changed"):
                    probe.run_r32b_control_probe(args)
            config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
