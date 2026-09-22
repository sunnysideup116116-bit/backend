"""Frozen-v2, batch-two reasoning isolation; synthetic-only and no Graph access."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import threading
import time
import warnings

from run_r31_reliability import FROZEN, frozen_cases, safe_failure_text
from run_r3_offline import FLAGS, selected_config, write_report
from r31_diagnostics import classify_provider_error
from r32_primary import classify_primary_response

HERE = Path(__file__).resolve().parent
OUT = HERE / "artifacts/r32b"
V2 = {
    "r32_prompt_v2.txt": "db71bf2c677a6eef856484097141cd19a53c1a5a3a764a2587f501e031805666",
    "R32_CONTRACT.md": "d14c2e115fe99374e23db11bb9b384fd7903e40446e024d9ecee4a982a9cd359",
}
PARSER_HASHES = {
    "r32_primary.py": "65d41b1071e1f063a82e5aee920e916bd50b311663cb4d7cd2cc287fe73a2151",
    "r31_diagnostics.py": "91ae45842b3e6b90cf2c905b4fe3880f94015cd9d76a77f2c498412fa911190f",
}
MODEL = "deepseek-v4.1-flash:cloud"
SEEDS = (9401, 9402, 9403)


def frozen_v2_cases():
    for name, expected in {**V2, **PARSER_HASHES}.items():
        if hashlib.sha256((HERE / name).read_bytes()).hexdigest() != expected:
            raise ValueError("frozen_v2_changed")
    return frozen_cases()


def schedule_r32b(cases, controls):
    if not controls or len(set(controls)) != len(controls) or not set(controls) <= {"none", "low", "default"}:
        raise ValueError("invalid_controls")
    if len(cases) != 96 or len({c["id"] for c in cases}) != 96:
        raise ValueError("expected_frozen_96_cases")
    jobs = []
    for repeat, seed in enumerate(SEEDS, 1):
        ordered = list(cases)
        random.Random(seed).shuffle(ordered)
        for control in controls:
            for offset in range(0, 96, 2):
                jobs.append({"job_id": f"r{repeat}-{control}-b{offset // 2 + 1:03}",
                             "repeat": repeat, "seed": seed, "control": control,
                             "batch_size": 2, "cases": ordered[offset:offset + 2]})
    random.Random(932201).shuffle(jobs)
    return jobs


def manifest_r32b(jobs, controls):
    return {"experiment": "R3.2b", "dataset_use": "96-case development; not final validation",
            "frozen_v1_hashes": FROZEN, "frozen_v2_hashes": V2, "parser_hashes": PARSER_HASHES, "labels_changed": False,
            "controls": list(controls), "effective_effort": "unknown; requested wire setting only",
            "low_arm": "requested-low included after contract probe" if "low" in controls else "SKIPPED: low support not confirmed by probe",
            "seeds": SEEDS, "batch_size": 2, "rounds": 3, "temperature": 0,
            "max_tokens": 4096, "timeout_seconds": 60, "workers": 3, "api_auto_retries": 0,
            "scheduled_requests": len(jobs), "hard_attempt_budget": 2 * len(jobs),
            "parser": "frozen r32_primary.classify_primary_response",
            "retry": "one identical schema/transient retry; never valid NO/ABSTAIN",
            "runtime_flags": dict.fromkeys(FLAGS, "off"), "graph_access": False,
            "production_fingerprint": "unknown", "production_ready": False}


def request_kwargs(job, prompt):
    kwargs = {"model": MODEL, "temperature": 0, "max_tokens": 4096,
              "messages": [{"role": "system", "content": prompt}, {"role": "user", "content":
                json.dumps({"pairs": [{k: c[k] for k in ("id", "Q", "C")} for c in job["cases"]]}, ensure_ascii=False)}]}
    if job["control"] != "default":
        kwargs["extra_body"] = {"reasoning_effort": job["control"]}
    return kwargs


def evaluate_r32b(job, client, prompt, key):
    result = {k: v for k, v in job.items() if k != "cases"}
    result.update(ids=[c["id"] for c in job["cases"]], attempts=[])
    kwargs = request_kwargs(job, prompt)
    begin = time.perf_counter()
    for number in (1, 2):
        content = None
        start = time.perf_counter()
        record = {"attempt": number, "usage": None}
        try:
            response = client.chat.completions.create(**kwargs)
            if response.usage:
                record["usage"] = {n: int(getattr(response.usage, n, 0) or 0)
                                   for n in ("prompt_tokens", "completion_tokens", "total_tokens")}
                value = getattr(getattr(response.usage, "completion_tokens_details", None), "reasoning_tokens", None)
                record["usage"]["reasoning_tokens"] = value if type(value) is int else None
            try:
                choice = response.choices[0]
                content = choice.message.content
                record.update(classify_primary_response(content, choice.finish_reason, set(result["ids"])))
                reasoning = getattr(choice.message, "reasoning_content", None)
                if not isinstance(reasoning, str):
                    reasoning = getattr(choice.message, "reasoning", None)
                record["metadata"].update(reasoning_char_count=len(reasoning) if isinstance(reasoning, str) else 0,
                                          reasoning_present=bool(reasoning) if isinstance(reasoning, str) else False)
                # Provider model metadata is deliberately restricted to known non-secret values.
                record["metadata"]["model_matches_requested"] = getattr(response, "model", None) == MODEL
            except Exception:
                record.update(valid=False, errors=["parser_internal_failure"], decisions={}, metadata={})
            retryable = not record["valid"] and "parser_internal_failure" not in record["errors"]
        except Exception as exc:
            info = classify_provider_error(exc)
            record.update(valid=False, errors=["provider_api_failure"], decisions={}, metadata=info)
            retryable = info["retryable"]
        record["latency_seconds"] = time.perf_counter() - start
        if not record["valid"]:
            text, retention = safe_failure_text(content, key)
            record["failure_retention"] = retention
            if text is not None:
                write_report(OUT / "failed_outputs" / f'{job["job_id"]}-attempt{number}.json',
                             {"synthetic_only": True, "job_id": job["job_id"],
                              "errors": record["errors"], "model_output": text}, [key])
        result["attempts"].append(record)
        if record["valid"] or not retryable or number == 2:
            break
        time.sleep(.5)
    result.update(elapsed_seconds=time.perf_counter() - begin,
                  first_pass_valid=result["attempts"][0]["valid"], eventual_valid=result["attempts"][-1]["valid"])
    return result


def run_r32b(args):
    import httpx
    from openai import OpenAI
    from r32b_controls_probe import matrix_controls_from_probe

    cases = frozen_v2_cases()
    controls = matrix_controls_from_probe(json.loads((OUT / "control_probe.json").read_text()))
    jobs = schedule_r32b(cases, controls)
    manifest = manifest_r32b(jobs, controls)
    path = OUT / "matrix.json"
    if path.exists() or (OUT / "manifest.json").exists():
        raise ValueError("refusing_to_overwrite_experiment")
    settings = selected_config(args.matchmaker_config, {"LLM_API_KEY", "LLM_BASE_URL"})
    model = selected_config(args.model_config, {"LLM_MODEL_ID"}).get("LLM_MODEL_ID")
    key = settings.get("LLM_API_KEY")
    base = settings.get("LLM_BASE_URL", "")
    del settings
    if not key or base.rstrip("/") != "https://ollama.com/v1" or model != MODEL:
        raise ValueError("provider_unconfirmed")
    prompt = (HERE / "r32_prompt_v2.txt").read_text()
    report = {"status": "RUNNING", "manifest": manifest, "model": MODEL, "http_attempts": 0, "jobs": []}
    write_report(OUT / "manifest.json", manifest, [key])
    write_report(path, report, [key])
    lock = threading.Lock()
    local = threading.local()
    clients = []

    def request_gate(request):
        if (request.method != "POST" or request.url.scheme != "https" or request.url.host != "ollama.com"
                or request.url.path != "/v1/chat/completions" or request.url.port not in (None, 443)
                or request.url.query or request.url.userinfo):
            raise ValueError("unexpected_endpoint")
        with lock:
            if report["http_attempts"] >= manifest["hard_attempt_budget"]:
                raise ValueError("attempt_budget_exhausted")
            report["http_attempts"] += 1

    def evaluate(job):
        if not hasattr(local, "client"):
            local.client = OpenAI(api_key=key, base_url=base, max_retries=0, timeout=60,
                http_client=httpx.Client(trust_env=False, follow_redirects=False, event_hooks={"request": [request_gate]}))
            with lock:
                clients.append(local.client)
        return evaluate_r32b(job, local.client, prompt, key)

    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            for future in as_completed([executor.submit(evaluate, job) for job in jobs]):
                item = future.result()
                report["jobs"].append(item)
                write_report(path, report, [key])
                if len(report["jobs"]) % 12 == 0 or not item["first_pass_valid"]:
                    print(json.dumps({"completed": len(report["jobs"]), "total": len(jobs),
                                      "control": item["control"], "first_valid": item["first_pass_valid"],
                                      "eventual_valid": item["eventual_valid"],
                                      "errors": [a["errors"] for a in item["attempts"]]}), flush=True)
        report["status"] = "COMPLETED"
    finally:
        for client in clients:
            client.close()
        write_report(path, report, [key])


def summarize_r32b():
    from r32b_metrics import summarize_condition
    cases = frozen_v2_cases()
    data = json.loads((OUT / "matrix.json").read_text())
    manifest = data["manifest"]
    jobs = schedule_r32b(cases, manifest["controls"])
    if data["status"] != "COMPLETED" or manifest != json.loads(json.dumps(manifest_r32b(jobs, manifest["controls"]))):
        raise ValueError("incomplete_or_changed_manifest")
    if sorted(j["job_id"] for j in data["jobs"]) != sorted(j["job_id"] for j in jobs):
        raise ValueError("incomplete_matrix")
    result = {"manifest": manifest, "http_attempts": data["http_attempts"], "conditions": {}}
    for control in manifest["controls"]:
        result["conditions"][control] = summarize_condition([j for j in data["jobs"] if j["control"] == control], cases)
    write_report(OUT / "summary.json", result)
    print(json.dumps({c: {k: v[k] for k in ("first_pass_rate", "eventual_rate", "final_pooled_yes",
                  "primary_consistency_all_cases", "all_gates_pass")} for c, v in result["conditions"].items()}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "summarize"))
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--matchmaker-config", type=Path)
    parser.add_argument("--confirm-synthetic-provider-use", action="store_true")
    args = parser.parse_args()
    warnings.filterwarnings("ignore")
    logging.disable(logging.CRITICAL)
    if any(os.environ.get(f, "off") != "off" for f in FLAGS):
        parser.error("semantic flags must stay OFF")
    if args.action == "run" and (not args.confirm_synthetic_provider_use or not args.model_config or not args.matchmaker_config):
        parser.error("explicit provider permission/config required")
    try:
        run_r32b(args) if args.action == "run" else summarize_r32b()
    except Exception:
        print("R3.2b stopped; inspect secret-safe artifacts. No production changes.", flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
