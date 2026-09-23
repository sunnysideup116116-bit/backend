"""R3.3 offline holdout runner. No production imports, Graph, or embedding IO."""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
from importlib import metadata
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
import warnings

import run_r32c_offline as frozen
from run_r3_offline import FLAGS, selected_config, write_report
from r33_data import HERE, ROOT, r33_snapshot
from r33_metrics import r33_summarize
OUT = HERE / "artifacts/r33"


def r33_current():
    snapshot, cases, jobs, audit = r33_snapshot()
    paths = list(snapshot["hashes"])
    changed = subprocess.check_output(["git","status","--porcelain","--untracked-files=all","--",*paths],cwd=ROOT,text=True)
    if changed:
        raise ValueError("commit_frozen_inputs_before_prepare")
    snapshot["source_commit"] = subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()
    subprocess.run(["git","merge-base","--is-ancestor",snapshot["gate_commit"],snapshot["source_commit"]],cwd=ROOT,check=True,capture_output=True)
    snapshot["sdk_versions"] = {name:metadata.version(name) for name in snapshot["configuration"]["sdk_versions"]}
    if snapshot["sdk_versions"] != snapshot["configuration"]["sdk_versions"]:
        raise ValueError("sdk_version_changed")
    return snapshot,cases,jobs,audit


def r33_prepare():
    snapshot,cases,_jobs,audit = r33_current()
    if (OUT/"manifest.json").exists() or (OUT/"matrix.json").exists():
        raise ValueError("refusing_to_overwrite_freeze")
    write_report(OUT/"manifest.json",snapshot)
    write_report(OUT/"inputs.json",{"synthetic_only":True,"cases":cases,"input_fidelity":audit})
    print(json.dumps({"status":"FROZEN_BEFORE_SCORING","source_commit":snapshot["source_commit"],
                      "case_count":len(cases),"coverage":snapshot["coverage"]}),flush=True)


def r33_run(args):
    import httpx
    from openai import OpenAI
    snapshot,_cases,jobs,_audit = r33_current()
    if json.loads((OUT/"manifest.json").read_text()) != snapshot:
        raise ValueError("prescoring_freeze_changed")
    if (OUT/"matrix.json").exists():
        raise ValueError("refusing_to_overwrite_experiment")
    settings = selected_config(args.matchmaker_config,{"LLM_API_KEY","LLM_BASE_URL"})
    model = selected_config(args.model_config,{"LLM_MODEL_ID"}).get("LLM_MODEL_ID")
    key,base = settings.get("LLM_API_KEY"),settings.get("LLM_BASE_URL","")
    del settings
    if not key or base.rstrip("/")!="https://ollama.com/v1" or model!=snapshot["configuration"]["model"]:
        raise ValueError("provider_unconfirmed")
    prompt = (HERE/"r32c_prompt_v3.txt").read_text()
    # Only the ignored diagnostic destination changes. Frozen request/parser/
    # retry code and configuration remain byte-for-byte identical.
    frozen.OUT = OUT
    allowed = Counter(hashlib.sha256(json.dumps(frozen.relation_request(job,prompt),ensure_ascii=False,sort_keys=True).encode()).hexdigest() for job in jobs)
    consumed = Counter()
    report = {"status":"RUNNING","manifest":snapshot,"http_attempts":0,"jobs":[]}
    write_report(OUT/"matrix.json",report,[key])
    lock,local,clients = threading.Lock(),threading.local(),[]

    def request_gate(request):
        url = request.url
        if (request.method!="POST" or url.scheme!="https" or url.host!="ollama.com"
                or url.path!="/v1/chat/completions" or url.port not in (None,443)
                or url.query or url.userinfo):
            raise ValueError("unexpected_endpoint")
        digest = hashlib.sha256(json.dumps(json.loads(request.content),ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        with lock:
            if digest not in allowed or consumed[digest]>=allowed[digest]*2:
                raise ValueError("unplanned_or_excess_request")
            if report["http_attempts"]>=snapshot["configuration"]["hard_http_attempt_limit"]:
                raise ValueError("attempt_budget_exhausted")
            consumed[digest]+=1
            report["http_attempts"]+=1

    def evaluate(job):
        if not hasattr(local,"client"):
            local.client=OpenAI(api_key=key,base_url=base,max_retries=0,timeout=60,
                http_client=httpx.Client(trust_env=False,follow_redirects=False,event_hooks={"request":[request_gate]}))
            with lock:
                clients.append(local.client)
        return frozen.evaluate_relation_batch(job,local.client,prompt,key)

    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            for future in as_completed([executor.submit(evaluate,job) for job in jobs]):
                item=future.result()
                report["jobs"].append(item)
                write_report(OUT/"matrix.json",report,[key])
                if len(report["jobs"])%24==0 or not item["first_pass_valid"]:
                    print(json.dumps({"completed":len(report["jobs"]),"total":len(jobs),
                        "first_valid":item["first_pass_valid"],"eventual_valid":item["eventual_valid"],
                        "errors":[a["errors"] for a in item["attempts"]]}),flush=True)
        if any(a["metadata"].get("model_matches_requested") is False for j in report["jobs"] for a in j["attempts"]):
            raise ValueError("provider_model_changed")
        report["status"]="COMPLETED"
    finally:
        for client in clients:
            client.close()
        write_report(OUT/"matrix.json",report,[key])


def r33_report():
    snapshot,cases,_jobs,_audit = r33_current()
    data=json.loads((OUT/"matrix.json").read_text())
    if data["status"]!="COMPLETED" or data["manifest"]!=snapshot or json.loads((OUT/"manifest.json").read_text())!=snapshot:
        raise ValueError("incomplete_or_changed_experiment")
    summary={"manifest":snapshot,"http_attempts":data["http_attempts"],"results":r33_summarize(data["jobs"],cases)}
    write_report(OUT/"summary.json",summary)
    print(json.dumps({k:summary["results"][k] for k in ("PASS","blocking_gates","acceptance_precision",
        "acceptance_recall","acceptance_consistency","first_pass_validity","eventual_validity","ERROR_observations")}),flush=True)


def r33_main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("prepare","run","summarize"))
    parser.add_argument("--model-config",type=Path)
    parser.add_argument("--matchmaker-config",type=Path)
    parser.add_argument("--confirm-synthetic-provider-use",action="store_true")
    args=parser.parse_args()
    warnings.filterwarnings("ignore")
    logging.disable(logging.CRITICAL)
    if any(os.environ.get(f)!="off" for f in FLAGS):
        parser.error("semantic flags must explicitly remain OFF")
    if args.action=="run" and (not args.confirm_synthetic_provider_use or not args.model_config or not args.matchmaker_config):
        parser.error("explicit synthetic provider permission/config required")
    try:
        {"prepare":r33_prepare,"run":lambda:r33_run(args),"summarize":r33_report}[args.action]()
    except Exception:
        print("R3.3 stopped. Inspect secret-safe artifacts; no automatic rerun or production change.",flush=True)
        raise SystemExit(1) from None


if __name__=="__main__":
    r33_main()
