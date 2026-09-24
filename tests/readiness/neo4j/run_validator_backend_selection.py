"""Same-task native backend comparison; no production flow or final scoring."""
import argparse,hashlib,json,logging,os,random,subprocess
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from validator_native_providers import HERE,NativeProviders,PROFILES,native_evaluate
from validator_backend_metrics import backend_metrics
from r32c_relation import load_complete_cases
from run_r3_offline import FLAGS,write_report
ROOT=HERE.parents[2]
OUT=HERE/"artifacts/backend_selection"


def backend_snapshot():
    ready=json.loads((OUT/"capability_ready.json").read_text())
    assert ready["status"]=="COMPLETED"
    names=[n for n in ("gemini35","gemini31","gemini25","qwen_local") if ready["eligible"].get(n)]
    if not names:raise ValueError("no_capable_backend")
    cases=load_complete_cases()
    files=("VALIDATOR_BACKEND_SELECTION_PROTOCOL.md","INDEPENDENT_LABEL_REVIEW_PROCESS.md",
           "validator_native_providers.py","validator_backend_probe.py","validator_backend_ready.py",
           "validator_backend_metrics.py","run_validator_backend_selection.py","test_validator_backend_selection.py",
           "r32c_prompt_v3.txt","r32c_relation.py","r3_holdout.json")
    paths=["tests/readiness/neo4j/"+f for f in files]
    if subprocess.check_output(["git","status","--porcelain","--untracked-files=all","--",*paths],cwd=ROOT,text=True):
        raise ValueError("commit_protocol_before_scoring")
    jobs=[]
    for repeat,seed in enumerate((350101,350102,350103),1):
        ordered=list(cases);random.Random(seed).shuffle(ordered)
        for backend in names:
            for offset in range(0,96,2):
                jobs.append({"job_id":f"{backend}-r{repeat}-b{offset//2+1:03}","backend":backend,"repeat":repeat,
                    "batch_size":2,"cases":ordered[offset:offset+2]})
    random.Random(350100).shuffle(jobs)
    digest=lambda v:hashlib.sha256(json.dumps(v,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    snapshot={"experiment":"native-relation-backend-selection-development",
        "source_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
        "hashes":{f:hashlib.sha256((HERE/f).read_bytes()).hexdigest() for f in files},
        "capability_sha256":hashlib.sha256((OUT/"capability_ready.json").read_bytes()).hexdigest(),
        "cases_sha256":digest(cases),"schedule_sha256":digest(jobs),"profiles":{n:PROFILES[n] for n in names},
        "first_requests":len(jobs),"max_attempts":2*len(jobs),"thresholds":{"first_validity":.995,"eventual_validity":1,
        "precision":.99,"recall":.95,"acceptance_consistency":.99,"safety_false_accept":0,"ERROR_fail_open":0},
        "temperature":0,"batch_size":2,"rounds":3,"max_output_tokens":4096,"max_retries":1,
        "runtime_flags":dict.fromkeys(FLAGS,"off"),"production_ready":False,"human_final_review_required":True}
    return snapshot,cases,jobs


def backend_prepare():
    s,c,_=backend_snapshot()
    if (OUT/"development_manifest.json").exists():raise ValueError("no_overwrite")
    write_report(OUT/"development_manifest.json",s)
    write_report(OUT/"development_inputs.json",{"cases":c,"synthetic_only":True})
    print(json.dumps({"status":"FROZEN","profiles":list(s["profiles"]),"first_requests":s["first_requests"]}),flush=True)


def backend_run():
    s,_,jobs=backend_snapshot()
    if json.loads((OUT/"development_manifest.json").read_text())!=s or (OUT/"development_matrix.json").exists():
        raise ValueError("changed_or_existing_experiment")
    p=NativeProviders();report={"status":"SETUP","manifest":s,"jobs":[],"bindings":{},"not_run":{}}
    prompt=(HERE/"r32c_prompt_v3.txt").read_text()
    try:
        for name in s["profiles"]:
            if PROFILES[name]["provider"]=="google":
                binding=p.bind_pool(name);report["bindings"][name]=binding
                if not binding["usable"]:report["not_run"][name]="model_credential_unavailable"
                print(json.dumps({"backend":name,"binding":binding}),flush=True)
        selected=[j for j in jobs if j["backend"] not in report["not_run"]]
        report["status"]="RUNNING";write_report(OUT/"development_matrix.json",report,p.keys)
        with ThreadPoolExecutor(max_workers=3) as pool:
            for future in as_completed([pool.submit(native_evaluate,j,p,prompt) for j in selected]):
                item=future.result();report["jobs"].append(item)
                write_report(OUT/"development_matrix.json",report,p.keys)
                if len(report["jobs"])%24==0 or not item["first_pass_valid"]:
                    print(json.dumps({"completed":len(report["jobs"]),"total":len(selected),"backend":item["backend"],
                        "first_valid":item["first_pass_valid"],"eventual_valid":item["eventual_valid"],
                        "errors":[a["errors"] for a in item["attempts"]]}),flush=True)
        report["status"]="COMPLETED"
    finally:
        p.close();write_report(OUT/"development_matrix.json",report,p.keys)


def backend_report():
    s,c,jobs=backend_snapshot();d=json.loads((OUT/"development_matrix.json").read_text())
    if d["status"]!="COMPLETED" or d["manifest"]!=s:raise ValueError("incomplete_or_changed")
    planned=[j for j in jobs if j["backend"] not in d["not_run"]]
    if sorted(j["job_id"] for j in planned)!=sorted(j["job_id"] for j in d["jobs"]):raise ValueError("schedule_mismatch")
    results={}
    for name in s["profiles"]:
        if name in d["not_run"]:results[name]={"status":"NOT_RUN_UNAVAILABLE","PASS":False};continue
        results[name]=backend_metrics([j for j in d["jobs"] if j["backend"]==name],c)
    selected=next((name for name in ("gemini35","gemini31","gemini25","qwen_local") if results.get(name,{}).get("PASS")),None)
    write_report(OUT/"development_summary.json",{"manifest":s,"results":results,"selected":selected,
                 "final_scoring_blocked_pending_human_review":True,"production_ready":False})
    print(json.dumps({"selected":selected,"gates":{k:v.get("blocking_gates",v.get("status")) for k,v in results.items()}}),flush=True)


if __name__=="__main__":
    logging.disable(logging.CRITICAL)
    p=argparse.ArgumentParser();p.add_argument("action",choices=("prepare","run","summarize"));a=p.parse_args()
    if any(os.environ.get(f)!="off" for f in FLAGS):p.error("semantic flags must remain OFF")
    try:{"prepare":backend_prepare,"run":backend_run,"summarize":backend_report}[a.action]()
    except Exception:
        print("Native backend selection STOPPED; inspect secret-safe artifacts; no automatic rerun.",flush=True)
        raise SystemExit(1) from None
