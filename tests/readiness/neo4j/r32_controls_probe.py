"""Eight-request synthetic R3.2 control probe, never production runtime.

This does not prove hidden-compute enforcement. It compares observable final JSON,
reasoning presence/count and usage for omitted effort versus effort=none. No raw
provider text, exception, header, config path, credential or decision is persisted.
"""
from __future__ import annotations

import argparse
from functools import partial
import json
import logging
import os
from pathlib import Path
import re
import time
import warnings

from r31_diagnostics import classify_provider_error, classify_response
from run_r31_reliability import FROZEN, frozen_cases, safe_failure_text
from run_r3_offline import FLAGS, selected_config, write_report

HERE = Path(__file__).resolve().parent
OUT = HERE / "artifacts/r32/control_probe_wire.json"
MODEL = "deepseek-v4.1-flash:cloud"
BASE_URL = "https://ollama.com/v1"
MAX_REQUESTS = 8
BATCH_IDS = (("h02-f", "h12-r"), ("h03-f", "h03-r", "h30-f", "h32-f"))


def create_probe_jobs(cases):
    """Use fixed existing synthetic cases; labels never enter provider input."""
    by_id = {case["id"]: case for case in cases}
    jobs = []
    for repeat in (1, 2):
        controls = ("baseline", "none") if repeat == 1 else ("none", "baseline")
        for ids in BATCH_IDS:
            payload = [{name: by_id[case_id][name] for name in ("id", "Q", "C")}
                       for case_id in ids]
            for control in controls:
                jobs.append({"probe_id": f"probe-r{repeat}-s{len(ids)}-{control}",
                             "repeat": repeat, "batch_size": len(ids),
                             "control": control, "payload": payload})
    if len(jobs) != MAX_REQUESTS:
        raise ValueError("unexpected_probe_schedule")
    return jobs


def extract_probe_metadata(response, expected_ids, key):
    """Project only bounded safe metadata; never retain text or decisions."""
    record = {"valid": False, "errors": ["parser_internal_failure"],
              "actual_model": None, "finish_reason": "unknown", "char_count": None,
              "reasoning_present": False, "reasoning_char_count": None, "usage": None}
    try:
        actual = response.model
        if (isinstance(actual, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", actual)
                and safe_failure_text(actual, key)[0] is not None):
            record["actual_model"] = actual
        choice = response.choices[0]
        classified = classify_response(choice.message.content, choice.finish_reason, expected_ids)
        record.update(valid=classified["valid"], errors=classified["errors"],
                      finish_reason=classified["metadata"]["finish_reason"],
                      char_count=classified["metadata"]["char_count"])
        reasoning = getattr(choice.message, "reasoning_content", None)
        if not isinstance(reasoning, str):
            reasoning = getattr(choice.message, "reasoning", None)
        record["reasoning_present"] = isinstance(reasoning, str) and bool(reasoning)
        record["reasoning_char_count"] = len(reasoning) if isinstance(reasoning, str) else 0
        usage = getattr(response, "usage", None)
        if usage is not None:
            record["usage"] = {}
            for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = getattr(usage, name, None)
                record["usage"][name] = value if type(value) is int and 0 <= value <= 10**8 else None
            details = getattr(usage, "completion_tokens_details", None)
            value = getattr(details, "reasoning_tokens", None)
            record["usage"]["reasoning_tokens"] = value if type(value) is int and 0 <= value <= 10**8 else None
    except Exception:
        record.update(valid=False, errors=["parser_internal_failure"])
    return record


def summarize_probe_support(jobs):
    """HTTP success alone is not evidence of a functioning thinking control."""
    none = [job for job in jobs if job["control"] == "none"]
    baseline = [job for job in jobs if job["control"] == "baseline"]
    complete = len(none) == len(baseline) == 4
    valid_none = sum(job["valid"] for job in none)
    zero_none = sum(job["valid"] and job["reasoning_char_count"] == 0 for job in none)
    observed_baseline = sum(job["valid"] and job["reasoning_present"] for job in baseline)
    if not complete:
        status = "INCOMPLETE"
    elif any(job.get("provider_error") == "provider_contract_failure" for job in none):
        status = "CONTROL_REJECTED"
    elif any(job["reasoning_present"] for job in none):
        status = "NO_OBSERVABLE_THINKING_SUPPRESSION"
    elif valid_none == zero_none == 4 and observed_baseline:
        status = "OBSERVED_REASONING_OUTPUT_SUPPRESSION"
    else:
        status = "INCONCLUSIVE"
    return {"status": status, "none_valid": valid_none,
            "none_valid_without_reasoning": zero_none,
            "baseline_valid_with_reasoning": observed_baseline,
            "http_200_alone_is_support": False,
            "hidden_compute_enforcement_confirmed": False}


def probe_request_guard(counter, request):
    """Deny redirects, alternate endpoints and attempts beyond the hard budget."""
    url = request.url
    if (url.scheme != "https" or url.host != "ollama.com"
            or url.path != "/v1/chat/completions" or url.port not in (None, 443)
            or url.query or url.userinfo or request.method != "POST"):
        raise ValueError("unexpected_endpoint")
    if counter["http_attempts"] >= MAX_REQUESTS:
        raise ValueError("request_budget_exhausted")
    counter["http_attempts"] += 1


def run_control_probe(args):
    """Read only allowlisted credential/config names; issue at most eight calls."""
    if (not args.confirm_synthetic_provider_use or not args.model_config
            or not args.matchmaker_config):
        raise ValueError("explicit_provider_permission_required")
    if any(os.environ.get(flag, "off") != "off" for flag in FLAGS):
        raise ValueError("semantic_flags_must_remain_off")
    if OUT.exists():
        raise ValueError("refusing_to_overwrite_experiment")
    jobs = create_probe_jobs(frozen_cases())
    prompt = (HERE / "r3_prompt.txt").read_text()
    settings = selected_config(args.matchmaker_config, {"LLM_API_KEY", "LLM_BASE_URL"})
    key = settings.get("LLM_API_KEY")
    base = settings.get("LLM_BASE_URL", "")
    del settings
    model = selected_config(args.model_config, {"LLM_MODEL_ID"}).get("LLM_MODEL_ID")
    if not key or base.rstrip("/") != BASE_URL or model != MODEL:
        raise ValueError("provider_config_unconfirmed")

    import httpx
    from openai import OpenAI
    report = {"status": "RUNNING", "experiment": "R3.2-control-compatibility",
              "dataset_use": "development_not_final_holdout", "frozen_hashes": FROZEN,
              "requested_model": MODEL, "temperature": 0, "max_tokens": 4096,
              "timeout_seconds": 60, "max_retries": 0, "hard_request_budget": MAX_REQUESTS,
              "http_attempts": 0, "jobs": [], "graph_access": False,
              "runtime_flags": dict.fromkeys(FLAGS, "off"), "production_ready": False}
    write_report(OUT, report, [key])
    client = OpenAI(api_key=key, base_url=BASE_URL, max_retries=0, timeout=60,
                    http_client=httpx.Client(trust_env=False, follow_redirects=False,
                        event_hooks={"request": [partial(probe_request_guard, report)]}))
    try:
        for job in jobs:
            request = {"model": MODEL, "temperature": 0, "max_tokens": 4096,
                       "messages": [{"role": "system", "content": prompt},
                                    {"role": "user", "content": json.dumps(
                                        {"pairs": job["payload"]}, ensure_ascii=False)}]}
            if job["control"] == "none":
                # SDK 1.30.1 lacks the direct keyword; use its supported JSON passthrough.
                request["extra_body"] = {"reasoning_effort": "none"}
            started = time.perf_counter()
            try:
                response = client.chat.completions.create(**request)
                result = extract_probe_metadata(response, {p["id"] for p in job["payload"]}, key)
            except Exception as exc:
                failure = classify_provider_error(exc)
                result = {"valid": False, "errors": ["provider_api_failure"],
                          "provider_error": failure["subtype"], "actual_model": None,
                          "finish_reason": "unknown", "char_count": None,
                          "reasoning_present": False, "reasoning_char_count": None, "usage": None}
            result.update({name: job[name] for name in ("probe_id", "repeat", "batch_size", "control")})
            result["latency_seconds"] = time.perf_counter() - started
            report["jobs"].append(result)
            write_report(OUT, report, [key])
            print(json.dumps({"probe_id": job["probe_id"],
                              "status": "VALID" if result["valid"] else "ERROR"}), flush=True)
        report["status"] = "COMPLETED"
        report["control_support"] = summarize_probe_support(report["jobs"])
    finally:
        client.close()
        write_report(OUT, report, [key])
    return report


def probe_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--matchmaker-config", type=Path, required=True)
    parser.add_argument("--confirm-synthetic-provider-use", action="store_true")
    args = parser.parse_args()
    warnings.filterwarnings("ignore")
    logging.disable(logging.CRITICAL)
    try:
        run_control_probe(args)
    except Exception:
        print(json.dumps({"probe_id": "control-probe", "status": "STOPPED"}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    probe_main()
