"""Bounded synthetic R3.2b wire probe, not proof of effective reasoning effort.

Two frozen v2 batches are sent with none, low, and omitted effort. This can
confirm the documented field is accepted/rejected for this endpoint/model, not
its internal enforcement. No raw response, reasoning, exception or secret is
persisted. There are no retries, Graph calls or production runtime mutations.
"""
from __future__ import annotations

import argparse
from functools import partial
import hashlib
import json
import logging
import os
from pathlib import Path
import time
import warnings

from r31_diagnostics import classify_provider_error
from r32_controls_probe import BASE_URL, MODEL, extract_probe_metadata
from r32_primary import classify_primary_response
from run_r31_reliability import FROZEN, frozen_cases
from run_r3_offline import FLAGS, selected_config, write_report

HERE = Path(__file__).resolve().parent
OUT = HERE / "artifacts/r32b/control_probe.json"
FROZEN_R32B = {
    **FROZEN,
    "r32_prompt_v2.txt": "db71bf2c677a6eef856484097141cd19a53c1a5a3a764a2587f501e031805666",
    "R32_CONTRACT.md": "d14c2e115fe99374e23db11bb9b384fd7903e40446e024d9ecee4a982a9cd359",
    "r32_primary.py": "65d41b1071e1f063a82e5aee920e916bd50b311663cb4d7cd2cc287fe73a2151",
    "r31_diagnostics.py": "91ae45842b3e6b90cf2c905b4fe3880f94015cd9d76a77f2c498412fa911190f",
}
BATCH_IDS = (("h02-f", "h12-r"), ("h03-f", "h03-r"))
CONTROLS = ("none", "low", "default")
INVALID_EFFORT = "invalid_r32b_probe_effort"
MAX_REQUESTS = 7
DOCUMENTATION = [
    "https://docs.ollama.com/api/openai-compatibility",
    "https://ollama.com/library/deepseek-v4.1-flash",
]


def create_r32b_probe_jobs(cases, include_invalid=False):
    """Only frozen Q/C/opaque IDs leave the process; labels are excluded."""
    by_id = {case["id"]: case for case in cases}
    jobs = []
    for batch, ids in enumerate(BATCH_IDS, 1):
        payload = [{name: by_id[case_id][name] for name in ("id", "Q", "C")}
                   for case_id in ids]
        # Alternate the order without changing the selected two-case batches.
        controls = CONTROLS if batch == 1 else tuple(reversed(CONTROLS))
        for control in controls:
            jobs.append({"probe_id": f"r32b-b{batch}-{control}", "batch": batch,
                         "batch_size": 2, "control": control, "payload": payload})
    if include_invalid:
        jobs.append({"probe_id": "r32b-invalid-effort", "batch": 1, "batch_size": 2,
                     "control": "invalid", "payload": jobs[0]["payload"]})
    return jobs


def summarize_r32b_probe_support(jobs):
    """Distinguish documented wire acceptance from unknowable internal effort."""
    grouped = {c: [j for j in jobs if j.get("control") == c] for c in CONTROLS}
    complete = all(len(rows) == 2 and {j.get("batch") for j in rows} == {1, 2}
                   for rows in grouped.values())
    default_ok = complete and all(j.get("response_received") is True
        and j.get("actual_model") == MODEL for j in grouped["default"])
    low = grouped["low"]
    accepted = default_ok and all(j.get("response_received") is True
        and j.get("actual_model") == MODEL for j in low)
    rejected = default_ok and all(j.get("response_received") is False
        and j.get("provider_error") == "provider_contract_failure" for j in low)
    status = ("WIRE_ACCEPTED_EFFECT_UNKNOWN" if accepted else
              "LOW_REQUEST_REJECTED" if rejected else "INCONCLUSIVE")
    negative = [j for j in jobs if j.get("control") == "invalid"]
    negative_status = "NOT_RUN"
    if negative:
        negative_status = "INCONCLUSIVE"
        if len(negative) == 1 and negative[0].get("response_received") is True:
            negative_status = "ACCEPTED_MAPPING_ENFORCEMENT_UNVERIFIED"
            status = "INCONCLUSIVE"
            accepted = False
        elif (len(negative) == 1 and negative[0].get("response_received") is False
              and negative[0].get("provider_error") == "provider_contract_failure"):
            negative_status = "REJECTED_PARAMETER_VALIDATION_OBSERVED"
        else:
            status = "INCONCLUSIVE"
            accepted = False
    return {
        "status": status,
        "documented_field": "reasoning_effort",
        "requested_low_matrix_allowed": accepted,
        "effective_effort": "unknown",
        "hidden_compute_enforcement_confirmed": False,
        "invalid_effort_negative_control": negative_status,
        "low_protocol_valid_count": sum(j.get("valid") is True for j in low),
        "observations": {c: {
            "response_count": sum(j.get("response_received") is True for j in rows),
            "reasoning_present_count": sum(j.get("reasoning_present") is True for j in rows),
            "valid_count": sum(j.get("valid") is True for j in rows),
        } for c, rows in grouped.items()},
        "interpretation": "Only requested wire settings are compared; no numeric effort or token cap is certified.",
    }


def matrix_controls_from_probe(report):
    """Require complete frozen evidence; never turn an inconclusive probe into support."""
    if (report.get("status") != "COMPLETED" or report.get("experiment") != "R3.2b-control-probe"
            or report.get("frozen_hashes") != FROZEN_R32B
            or report.get("requested_model") != MODEL
            or report.get("temperature") != 0 or report.get("max_tokens") != 4096
            or report.get("graph_access") is not False
            or report.get("runtime_flags") != dict.fromkeys(FLAGS, "off")):
        raise ValueError("probe_evidence_unconfirmed")
    support = summarize_r32b_probe_support(report.get("jobs", []))
    if support != report.get("control_support"):
        raise ValueError("probe_summary_inconsistent")
    if support["status"] == "WIRE_ACCEPTED_EFFECT_UNKNOWN":
        return CONTROLS
    if support["status"] == "LOW_REQUEST_REJECTED":
        return ("none", "default")
    if (support["status"] == "INCONCLUSIVE"
            and support["invalid_effort_negative_control"] == "ACCEPTED_MAPPING_ENFORCEMENT_UNVERIFIED"):
        # A permissive enum parser does not establish low support. Keep low skipped;
        # only independently demonstrated none/default controls may still proceed.
        safe_controls = True
        for control in ("none", "default"):
            rows = [j for j in report["jobs"] if j.get("control") == control]
            safe_controls = safe_controls and len(rows) == 2 and {j.get("batch") for j in rows} == {1, 2}
            safe_controls = safe_controls and all(j.get("valid") is True
                and j.get("response_received") is True and j.get("actual_model") == MODEL
                and (j.get("reasoning_char_count") == 0 if control == "none"
                     else j.get("reasoning_present") is True and type(j.get("reasoning_char_count")) is int
                     and j["reasoning_char_count"] > 0) for j in rows)
        if safe_controls:
            return ("none", "default")
    raise ValueError("low_support_inconclusive")


def r32b_probe_request_guard(counter, request):
    """Count actual attempts, disallow alternate endpoints and redirects."""
    url = request.url
    if (url.scheme != "https" or url.host != "ollama.com"
            or url.path != "/v1/chat/completions" or url.port not in (None, 443)
            or url.query or url.userinfo or request.method != "POST"):
        raise ValueError("unexpected_endpoint")
    if counter["http_attempts"] >= counter["hard_request_budget"]:
        raise ValueError("request_budget_exhausted")
    counter["http_attempts"] += 1


def run_r32b_control_probe(args):
    """Issue six (optionally seven) synthetic calls with allowlisted config only."""
    if (not args.confirm_synthetic_provider_use or not args.model_config
            or not args.matchmaker_config):
        raise ValueError("explicit_provider_permission_required")
    if any(os.environ.get(flag, "off") != "off" for flag in FLAGS):
        raise ValueError("semantic_flags_must_remain_off")
    if OUT.exists():
        raise ValueError("refusing_to_overwrite_experiment")
    for name, sha in FROZEN_R32B.items():
        if hashlib.sha256((HERE / name).read_bytes()).hexdigest() != sha:
            raise ValueError("frozen_input_changed")
    jobs = create_r32b_probe_jobs(frozen_cases(), bool(args.include_invalid_effort))
    prompt = (HERE / "r32_prompt_v2.txt").read_text()
    settings = selected_config(args.matchmaker_config, {"LLM_API_KEY", "LLM_BASE_URL"})
    key, base = settings.get("LLM_API_KEY"), settings.get("LLM_BASE_URL", "")
    del settings
    model = selected_config(args.model_config, {"LLM_MODEL_ID"}).get("LLM_MODEL_ID")
    if not key or base.rstrip("/") != BASE_URL or model != MODEL:
        raise ValueError("provider_config_unconfirmed")

    import httpx
    from openai import OpenAI
    report = {"status": "RUNNING", "experiment": "R3.2b-control-probe",
              "dataset_use": "development_not_final_holdout", "frozen_hashes": FROZEN_R32B,
              "requested_model": MODEL, "temperature": 0, "max_tokens": 4096,
              "timeout_seconds": 60, "max_retries": 0, "hard_request_budget": len(jobs),
              "http_attempts": 0, "jobs": [], "graph_access": False,
              "runtime_flags": dict.fromkeys(FLAGS, "off"), "production_ready": False,
              "documentation": DOCUMENTATION,
              "effective_effort": "unknown", "no_raw_model_or_reasoning_text_saved": True}
    write_report(OUT, report, [key])
    client = OpenAI(api_key=key, base_url=BASE_URL, max_retries=0, timeout=60,
                    http_client=httpx.Client(trust_env=False, follow_redirects=False,
                        event_hooks={"request": [partial(r32b_probe_request_guard, report)]}))
    try:
        for job in jobs:
            request = {"model": MODEL, "temperature": 0, "max_tokens": 4096,
                       "messages": [{"role": "system", "content": prompt},
                                    {"role": "user", "content": json.dumps(
                                        {"pairs": job["payload"]}, ensure_ascii=False)}]}
            effort = {"none": "none", "low": "low", "invalid": INVALID_EFFORT}.get(job["control"])
            if effort is not None:
                request["extra_body"] = {"reasoning_effort": effort}
            started = time.perf_counter()
            try:
                response = client.chat.completions.create(**request)
                ids = {p["id"] for p in job["payload"]}
                result = extract_probe_metadata(response, ids, key)
                # Keep exactly the frozen v2 parser semantics; discard its decisions.
                classified = classify_primary_response(response.choices[0].message.content,
                                                       response.choices[0].finish_reason, ids)
                result.update(valid=classified["valid"], errors=classified["errors"],
                              response_received=True)
            except Exception as exc:
                failure = classify_provider_error(exc)
                result = {"valid": False, "errors": ["provider_api_failure"],
                          "provider_error": failure["subtype"], "response_received": False,
                          "actual_model": None, "finish_reason": "unknown", "char_count": None,
                          "reasoning_present": False, "reasoning_char_count": None, "usage": None}
            result.update({name: job[name] for name in ("probe_id", "batch", "batch_size", "control")})
            result["requested_effort"] = effort if effort is not None else "omitted"
            result["effective_effort"] = "unknown"
            result["latency_seconds"] = time.perf_counter() - started
            report["jobs"].append(result)
            write_report(OUT, report, [key])
            print(json.dumps({"probe_id": job["probe_id"],
                              "status": "VALID" if result["valid"] else "ERROR"}), flush=True)
        report["status"] = "COMPLETED"
        report["control_support"] = summarize_r32b_probe_support(report["jobs"])
    finally:
        client.close()
        write_report(OUT, report, [key])
    return report


def r32b_probe_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--matchmaker-config", type=Path, required=True)
    parser.add_argument("--confirm-synthetic-provider-use", action="store_true")
    parser.add_argument("--include-invalid-effort", action="store_true")
    args = parser.parse_args()
    warnings.filterwarnings("ignore")
    logging.disable(logging.CRITICAL)
    try:
        run_r32b_control_probe(args)
    except Exception:
        print(json.dumps({"probe_id": "r32b-control-probe", "status": "STOPPED"}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    r32b_probe_main()
