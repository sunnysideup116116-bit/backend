"""Hermetic control-probe tests: no real secret reads, sockets or Graph calls."""
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
import r32_controls_probe as probe


class R32ControlsProbeTests(unittest.TestCase):
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

    def fake_run(self, outcome=None):
        calls, reports, closed = [], [], []
        fake_key = "offline-only-fake-credential-Z9XZ"
        config_calls = []

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

            # Match the older installed SDK's explicit signature: an unsupported
            # direct reasoning_effort keyword fails before any fake HTTP hook.
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
                ids = [row["id"] for row in json.loads(request["messages"][1]["content"])["pairs"]]
                if outcome is not None:
                    return outcome(request, ids)
                return self.response(ids, reasoning="" if request.get("extra_body", {}).get("reasoning_effort") == "none"
                                     else "not persisted synthetic reasoning")
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                                   close=lambda: closed.append(True))

        def capture(_path, data, secrets=()):
            serialized = json.dumps(data)
            for forbidden in (fake_key, fake_key[:6], fake_key[-4:], "Synthetic Query",
                              "Synthetic Candidate", "not persisted synthetic reasoning"):
                self.assertNotIn(forbidden, serialized)
            reports.append(deepcopy(data))

        output = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="r32-probe-test-") as temporary, ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {
                "httpx": SimpleNamespace(Client=http_client),
                "openai": SimpleNamespace(OpenAI=openai_client)}))
            stack.enter_context(patch.object(probe, "frozen_cases", return_value=self.cases()))
            stack.enter_context(patch.object(probe, "selected_config", side_effect=config))
            stack.enter_context(patch.object(probe, "write_report", side_effect=capture))
            stack.enter_context(patch.object(probe, "OUT", Path(temporary) / "control_probe.json"))
            stack.enter_context(patch.dict(probe.os.environ, dict.fromkeys(probe.FLAGS, "off")))
            stack.enter_context(redirect_stdout(output))
            report = probe.run_control_probe(SimpleNamespace(model_config="not-read",
                matchmaker_config="not-read", confirm_synthetic_provider_use=True))
        self.assertEqual(closed, [True])
        self.assertEqual(config_calls, [{"LLM_API_KEY", "LLM_BASE_URL"}, {"LLM_MODEL_ID"}])
        self.assertEqual(len(calls), 8)
        self.assertEqual(report["http_attempts"], 8)
        self.assertEqual(report["status"], "COMPLETED")
        for line in output.getvalue().splitlines():
            self.assertEqual(set(json.loads(line)), {"probe_id", "status"})
        return calls, report

    def test_schedule_fixed_two_sizes_two_controls_two_repeats(self):
        jobs = probe.create_probe_jobs(self.cases())
        self.assertEqual(len(jobs), 8)
        self.assertEqual(len({job["probe_id"] for job in jobs}), 8)
        self.assertEqual(jobs, probe.create_probe_jobs(self.cases()))
        for job in jobs:
            self.assertIn(job["batch_size"], {2, 4})
            for row in job["payload"]:
                self.assertEqual(set(row), {"id", "Q", "C"})

    def test_none_is_only_request_variable_and_no_valid_no_retry(self):
        calls, report = self.fake_run()
        self.assertEqual(sum(call.get("extra_body") == {"reasoning_effort": "none"} for call in calls), 4)
        for call in calls:
            self.assertEqual(call["max_tokens"], 4096)
            self.assertEqual(call["temperature"], 0)
            self.assertNotIn("response_format", call)
            self.assertNotIn("reasoning_effort", call)
            self.assertNotIn("think", call.get("extra_body", {}))
            if "extra_body" in call:
                self.assertEqual(call["extra_body"], {"reasoning_effort": "none"})
        for offset in (0, 2, 4, 6):
            one, two = deepcopy(calls[offset]), deepcopy(calls[offset + 1])
            one.pop("extra_body", None)
            two.pop("extra_body", None)
            self.assertEqual(one, two)
        self.assertEqual(report["control_support"]["status"], "OBSERVED_REASONING_OUTPUT_SUPPRESSION")
        self.assertFalse(report["control_support"]["hidden_compute_enforcement_confirmed"])

    def test_local_sdk_keyword_error_is_not_provider_rejection(self):
        failure = probe.classify_provider_error(TypeError("unexpected keyword argument"))
        self.assertEqual(failure["subtype"], "unexpected_provider_failure")
        self.assertFalse(failure["retryable"])
        jobs = []
        for job in probe.create_probe_jobs(self.cases()):
            baseline = job["control"] == "baseline"
            jobs.append({"control": job["control"], "valid": baseline,
                         "reasoning_present": baseline,
                         "reasoning_char_count": 100 if baseline else None,
                         "provider_error": None if baseline else failure["subtype"]})
        result = probe.summarize_probe_support(jobs)
        self.assertEqual(result["status"], "INCONCLUSIVE")
        self.assertNotEqual(result["status"], "CONTROL_REJECTED")

    def test_http_success_without_valid_json_never_confirms_control(self):
        _, report = self.fake_run(lambda _request, ids: self.response(ids, content="bad-json"))
        self.assertEqual(report["control_support"]["status"], "INCONCLUSIVE")
        self.assertTrue(all(not job["valid"] for job in report["jobs"]))
        self.assertTrue(all(job["errors"] == ["invalid_json"] for job in report["jobs"]))

    def test_none_with_reasoning_is_not_enforced(self):
        _, report = self.fake_run(lambda _request, ids: self.response(ids, reasoning="unexpected reasoning"))
        self.assertEqual(report["control_support"]["status"], "NO_OBSERVABLE_THINKING_SUPPRESSION")

    def test_both_conditions_without_reasoning_are_inconclusive(self):
        _, report = self.fake_run(lambda _request, ids: self.response(ids))
        self.assertEqual(report["control_support"]["status"], "INCONCLUSIVE")

    def test_no_raw_exception_retained_and_no_retry(self):
        class ProviderFailure(Exception):
            status_code = 400
            def __str__(self):
                raise AssertionError("do not inspect provider message")
        def fail(_request, _ids):
            raise ProviderFailure()
        _, report = self.fake_run(fail)
        self.assertEqual(report["control_support"]["status"], "CONTROL_REJECTED")
        self.assertTrue(all(job["provider_error"] == "provider_contract_failure" for job in report["jobs"]))

    def test_metadata_blocks_model_secrets_and_never_keeps_text(self):
        key = "test-credential-secret-Z9XZ"
        for model in (key, key[:6], key[-4:], "https://untrusted.test/?secret=x"):
            with self.subTest(model_length=len(model)):
                response = self.response(["h02-f"], model=model, reasoning=key,
                                         content=json.dumps({"unexpected": key}))
                metadata = probe.extract_probe_metadata(response, {"h02-f"}, key)
                self.assertIsNone(metadata["actual_model"])
                self.assertNotIn(key, json.dumps(metadata))
                self.assertNotIn("content", metadata)
                self.assertNotIn("decisions", metadata)
                self.assertEqual(metadata["reasoning_char_count"], len(key))

    def test_usage_values_are_bounded_numbers_only(self):
        response = self.response(["h02-f"])
        response.usage = SimpleNamespace(prompt_tokens=True, completion_tokens="credential",
            total_tokens=10**9, completion_tokens_details=SimpleNamespace(reasoning_tokens=5))
        result = probe.extract_probe_metadata(response, {"h02-f"}, "fake-credential")
        self.assertEqual(result["usage"], {"prompt_tokens": None, "completion_tokens": None,
                                         "total_tokens": None, "reasoning_tokens": 5})

    def test_endpoint_and_budget_guard(self):
        good = dict(scheme="https", host="ollama.com", path="/v1/chat/completions",
                    port=None, query=b"", userinfo=b"")
        for changes in ({"host": "evil.test"}, {"scheme": "http"}, {"port": 8443},
                        {"path": "/api/chat"}, {"query": b"x=y"}, {"userinfo": b"user"}):
            with self.subTest(changes=changes):
                counter = {"http_attempts": 0}
                with self.assertRaises(ValueError):
                    probe.probe_request_guard(counter, SimpleNamespace(method="POST",
                        url=SimpleNamespace(**{**good, **changes})))
                self.assertEqual(counter["http_attempts"], 0)
        event = SimpleNamespace(method="POST", url=SimpleNamespace(**good))
        counter = {"http_attempts": 0}
        for _ in range(8):
            probe.probe_request_guard(counter, event)
        with self.assertRaises(ValueError):
            probe.probe_request_guard(counter, event)
        self.assertEqual(counter["http_attempts"], 8)

    def test_permission_and_flags_block_before_config_reads(self):
        args = SimpleNamespace(model_config="not-read", matchmaker_config="not-read",
                               confirm_synthetic_provider_use=False)
        with patch.object(probe, "selected_config") as config:
            with self.assertRaises(ValueError):
                probe.run_control_probe(args)
            args.confirm_synthetic_provider_use = True
            with patch.dict(probe.os.environ, {probe.FLAGS[0]: "active"}):
                with self.assertRaises(ValueError):
                    probe.run_control_probe(args)
            config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
