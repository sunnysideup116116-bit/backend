"""One frozen full-input relation-only experiment; never a production service."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
import warnings

from r31_diagnostics import classify_provider_error
from r32c_relation import classify_relation_response, load_complete_cases, RELATION_TO_PRIMARY
from run_r31_reliability import safe_failure_text
from run_r3_offline import FLAGS, write_report, selected_config
from run_r32b_offline import schedule_r32b, MODEL

HERE = Path(__file__).resolve().parent
OUT = HERE / "artifacts/r32c"
FROZEN_FILES = ("r3_holdout.json", "r32c_prompt_v3.txt", "R32C_CONTRACT.md",
                "r32c_relation.py", "r32c_metrics.py", "run_r32c_offline.py")


def experiment_snapshot():
    cases = load_complete_cases()
    jobs = schedule_r32b(cases, ("default",))
    cases_bytes = json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()
    snapshot = {
        "experiment": "R3.2c", "dataset_use": "development_only_not_final_holdout",
        "hashes": {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in FROZEN_FILES},
        "expanded_cases_sha256": hashlib.sha256(cases_bytes).hexdigest(),
        "input_mode": "full original synthetic Q/C; validation-only; no canonicalization or truncation",
        "input_character_bound": 120, "case_count": len(cases), "primary_gold_changed": False,
        "relation_gold": "explicit v3 projection preserving source_relation and original primary",
        "relation_to_primary": RELATION_TO_PRIMARY,
        "provider": "Ollama Cloud", "model": MODEL, "reasoning_effort": "omitted/default",
        "temperature": 0, "max_tokens": 4096, "batch_size": 2, "rounds": 3,
        "seeds": [9401, 9402, 9403], "timeout_seconds": 60, "sdk_retries": 0, "workers": 3,
        "scheduled_requests": len(jobs), "hard_attempt_budget": 2 * len(jobs),
        "retry": "one identical malformed/empty/truncated/transient retry; never valid mapped NO/ABSTAIN",
        "runtime_flags": dict.fromkeys(FLAGS, "off"), "graph_access": False,
        "production_ready": False, "production_fingerprint": "unknown",
    }
    return snapshot, cases, jobs


def prepare_r32c():
    snapshot, cases, _jobs = experiment_snapshot()
    path = OUT / "manifest.json"
    if path.exists():
        raise ValueError("refusing_to_overwrite_prescoring_freeze")
    write_report(path, snapshot)
    # Synthetic full strings/annotations only; never a raw user message.
    write_report(OUT / "inputs.json", {"synthetic_only": True, "cases": cases})
    print(json.dumps({"status": "FROZEN_BEFORE_SCORING", "case_count": len(cases),
                      "scheduled_requests": snapshot["scheduled_requests"], "hashes": snapshot["hashes"]}))


def relation_request(job, prompt):
    return {"model": MODEL, "temperature": 0, "max_tokens": 4096,
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(
                {"pairs": [{k: case[k] for k in ("id", "Q", "C")} for case in job["cases"]]}, ensure_ascii=False)}]}


def evaluate_relation_batch(job, client, prompt, key):
    result = {k: v for k, v in job.items() if k != "cases"}
    result.update(ids=[case["id"] for case in job["cases"]], attempts=[])
    request = relation_request(job, prompt)
    started = time.perf_counter()
    for number in (1, 2):
        content = None
        begin = time.perf_counter()
        record = {"attempt": number, "usage": None}
        try:
            response = client.chat.completions.create(**request)
            if response.usage:
                record["usage"] = {name: int(getattr(response.usage, name, 0) or 0)
                                   for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
                reasoning_tokens = getattr(getattr(response.usage, "completion_tokens_details", None), "reasoning_tokens", None)
                record["usage"]["reasoning_tokens"] = reasoning_tokens if type(reasoning_tokens) is int else None
            try:
                choice = response.choices[0]
                content = choice.message.content
                record.update(classify_relation_response(content, choice.finish_reason, set(result["ids"])))
                reasoning = getattr(choice.message, "reasoning_content", None)
                if not isinstance(reasoning, str):
                    reasoning = getattr(choice.message, "reasoning", None)
                record["metadata"].update(reasoning_present=bool(reasoning) if isinstance(reasoning, str) else False,
                    reasoning_char_count=len(reasoning) if isinstance(reasoning, str) else 0,
                    model_matches_requested=getattr(response, "model", None) == MODEL)
            except Exception:
                record.update(valid=False, errors=["parser_internal_failure"], decisions={}, metadata={})
            retryable = not record["valid"] and not set(record["errors"]).intersection(
                {"parser_internal_failure", "unexpected_finish_reason"})
        except Exception as exc:
            info = classify_provider_error(exc)
            record.update(valid=False, errors=["provider_api_failure"], decisions={}, metadata=info)
            retryable = info["retryable"]
        record["latency_seconds"] = time.perf_counter() - begin
        if not record["valid"]:
            text, retention = safe_failure_text(content, key)
            record["failure_retention"] = retention
            if text is not None:
                write_report(OUT / "failed_outputs" / f'{job["job_id"]}-attempt{number}.json',
                    {"synthetic_only": True, "job_id": job["job_id"], "errors": record["errors"], "model_output": text}, [key])
        result["attempts"].append(record)
        if record["valid"] or not retryable or number == 2:
            break
        time.sleep(.5)
    result.update(elapsed_seconds=time.perf_counter() - started,
        first_pass_valid=result["attempts"][0]["valid"], eventual_valid=result["attempts"][-1]["valid"])
    return result


def run_r32c(args):
    import httpx
    from openai import OpenAI
    snapshot, _cases, jobs = experiment_snapshot()
    if json.loads((OUT / "manifest.json").read_text()) != snapshot:
        raise ValueError("prescoring_freeze_changed")
    path = OUT / "matrix.json"
    if path.exists():
        raise ValueError("refusing_to_overwrite_experiment")
    selected = selected_config(args.matchmaker_config, {"LLM_API_KEY", "LLM_BASE_URL"})
    model = selected_config(args.model_config, {"LLM_MODEL_ID"}).get("LLM_MODEL_ID")
    key, base = selected.get("LLM_API_KEY"), selected.get("LLM_BASE_URL", "")
    del selected
    if not key or base.rstrip("/") != "https://ollama.com/v1" or model != MODEL:
        raise ValueError("provider_unconfirmed")
    prompt = (HERE / "r32c_prompt_v3.txt").read_text()
    report = {"status": "RUNNING", "manifest": snapshot, "http_attempts": 0, "jobs": []}
    write_report(path, report, [key])
    lock, local, clients = threading.Lock(), threading.local(), []

    def request_gate(request):
        url = request.url
        if (request.method != "POST" or url.scheme != "https" or url.host != "ollama.com"
                or url.path != "/v1/chat/completions" or url.port not in (None, 443)
                or url.query or url.userinfo):
            raise ValueError("unexpected_endpoint")
        with lock:
            if report["http_attempts"] >= snapshot["hard_attempt_budget"]:
                raise ValueError("attempt_budget_exhausted")
            report["http_attempts"] += 1

    def evaluate(job):
        if not hasattr(local, "client"):
            local.client = OpenAI(api_key=key, base_url=base, max_retries=0, timeout=60,
                http_client=httpx.Client(trust_env=False, follow_redirects=False, event_hooks={"request": [request_gate]}))
            with lock:
                clients.append(local.client)
        return evaluate_relation_batch(job, local.client, prompt, key)

    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            for future in as_completed([executor.submit(evaluate, job) for job in jobs]):
                item = future.result()
                report["jobs"].append(item)
                write_report(path, report, [key])
                if len(report["jobs"]) % 12 == 0 or not item["first_pass_valid"]:
                    print(json.dumps({"completed": len(report["jobs"]), "total": len(jobs),
                        "first_valid": item["first_pass_valid"], "eventual_valid": item["eventual_valid"],
                        "errors": [attempt["errors"] for attempt in item["attempts"]]}), flush=True)
        report["status"] = "COMPLETED"
    finally:
        for client in clients:
            client.close()
        write_report(path, report, [key])


def summarize_r32c():
    from r32c_metrics import summarize_relation_condition
    snapshot, cases, jobs = experiment_snapshot()
    data = json.loads((OUT / "matrix.json").read_text())
    if (data["status"] != "COMPLETED" or data["manifest"] != snapshot
            or json.loads((OUT / "manifest.json").read_text()) != snapshot
            or sorted(j["job_id"] for j in data["jobs"]) != sorted(j["job_id"] for j in jobs)):
        raise ValueError("incomplete_or_changed_experiment")
    result = {"manifest": snapshot, "http_attempts": data["http_attempts"],
              "results": summarize_relation_condition(data["jobs"], cases)}
    write_report(OUT / "summary.json", result)
    print(json.dumps({k: result["results"][k] for k in ("first_pass_rate", "eventual_rate",
        "relation_accuracy_valid_final", "final_pooled_yes", "primary_consistency_all_cases",
        "relation_consistency_all_cases", "all_gates_pass")}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "summarize"))
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--matchmaker-config", type=Path)
    parser.add_argument("--confirm-synthetic-provider-use", action="store_true")
    args = parser.parse_args()
    warnings.filterwarnings("ignore")
    logging.disable(logging.CRITICAL)
    if any(os.environ.get(flag, "off") != "off" for flag in FLAGS):
        parser.error("semantic flags must stay OFF")
    if args.action == "run" and (not args.confirm_synthetic_provider_use or not args.model_config or not args.matchmaker_config):
        parser.error("explicit synthetic provider permission/config required")
    try:
        if args.action == "prepare":
            prepare_r32c()
        elif args.action == "run":
            run_r32c(args)
        else:
            summarize_r32c()
    except Exception:
        print("R3.2c stopped; inspect secret-safe artifacts. No production changes.", flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
