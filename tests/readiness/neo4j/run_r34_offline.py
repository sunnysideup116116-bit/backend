"""R3.4 four-arm reliability matrix; immutable semantic task/retry evaluator."""
from __future__ import annotations
import argparse,hashlib,json,logging,os,random,subprocess,threading,warnings
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
from types import SimpleNamespace
from importlib import metadata
import run_r32c_offline as frozen
from r32c_relation import load_complete_cases
from run_r3_offline import FLAGS,selected_config,write_report
from r34_metrics import r34_metrics,r34_select

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
OUT=HERE/"artifacts/r34"
ARMS=(("b2_t4096",2,4096),("b1_t4096",1,4096),("b2_t8192",2,8192),("b1_t8192",1,8192))
SEEDS=(340101,340102,340103)


def r34_schedule(cases):
    jobs=[]
    for repeat,seed in enumerate(SEEDS,1):
        ordered=list(cases);random.Random(seed).shuffle(ordered)
        for arm,size,budget in ARMS:
            for offset in range(0,len(cases),size):
                jobs.append({"job_id":f"r34-{arm}-r{repeat}-b{offset//size+1:03}","arm":arm,
                    "repeat":repeat,"seed":seed,"batch_size":size,"max_tokens":budget,
                    "cases":ordered[offset:offset+size]})
    random.Random(340100).shuffle(jobs)
    return jobs


def r34_current():
    baseline,cases,_=frozen.experiment_snapshot()
    paths=("R34_PROTOCOL.md","r34_budget_probe.py","r34_metrics.py","run_r34_offline.py","test_r34_offline.py")
    probe=json.loads((OUT/"budget_probe.json").read_text())
    if probe.get("status")!="COMPLETED" or probe.get("higher_budget_confirmed") is not True or probe.get("high_budget")!=8192:
        raise ValueError("higher_budget_not_confirmed")
    for name in ("r34_budget_probe.py","R34_PROTOCOL.md"):
        key="source_sha256" if name.endswith(".py") else "protocol_sha256"
        if hashlib.sha256((HERE/name).read_bytes()).hexdigest()!=probe[key]:
            raise ValueError("probe_freeze_changed")
    current_sources={name:hashlib.sha256((HERE/name).read_bytes()).hexdigest() for name in paths}
    checkpaths=["tests/readiness/neo4j/"+n for n in (*paths,*baseline["hashes"])]
    if subprocess.check_output(["git","status","--porcelain","--untracked-files=all","--",*checkpaths],cwd=ROOT,text=True):
        raise ValueError("commit_experiment_before_scoring")
    versions={k:metadata.version(k) for k in ("openai","httpx")}
    if versions!={"openai":"1.30.1","httpx":"0.28.1"}:
        raise ValueError("sdk_changed")
    jobs=r34_schedule(cases)
    snapshot={"experiment":"R3.4-development","source_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
        "sources":current_sources,"frozen_R32C":baseline,"probe_sha256":hashlib.sha256((OUT/"budget_probe.json").read_bytes()).hexdigest(),
        "case_sha256":hashlib.sha256(json.dumps(cases,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
        "schedule_sha256":hashlib.sha256(json.dumps(jobs,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
        "arms":ARMS,"seeds":SEEDS,"first_requests":864,"hard_attempt_budget":1728,"sdk":versions,
        "retired_R33_used":False,"flags":dict.fromkeys(FLAGS,"off"),"production_ready":False}
    # JSON-normalize tuples for immutable saved-manifest equality.
    return json.loads(json.dumps(snapshot)),cases,jobs


def r34_prepare():
    snapshot,cases,_=r34_current()
    if (OUT/"manifest.json").exists() or (OUT/"matrix.json").exists():
        raise ValueError("refusing_overwrite")
    write_report(OUT/"manifest.json",snapshot)
    write_report(OUT/"inputs.json",{"synthetic_only":True,"cases":cases})
    print(json.dumps({"status":"FROZEN","first_requests":864,"arms":snapshot["arms"]}),flush=True)


def r34_budget_client(client,budget):
    def create(**kwargs):
        if kwargs["max_tokens"]!=4096 or kwargs["temperature"]!=0 or "reasoning_effort" in kwargs or "extra_body" in kwargs:
            raise ValueError("frozen_request_changed")
        return client.chat.completions.create(**{**kwargs,"max_tokens":budget})
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def r34_run(args):
    import httpx
    from openai import OpenAI
    snapshot,_cases,jobs=r34_current()
    if json.loads((OUT/"manifest.json").read_text())!=snapshot or (OUT/"matrix.json").exists():
        raise ValueError("changed_or_existing_experiment")
    settings=selected_config(args.matchmaker_config,{"LLM_API_KEY","LLM_BASE_URL"})
    model=selected_config(args.model_config,{"LLM_MODEL_ID"}).get("LLM_MODEL_ID")
    key,base=settings.get("LLM_API_KEY"),settings.get("LLM_BASE_URL","");del settings
    if not key or base.rstrip("/")!="https://ollama.com/v1" or model!=frozen.MODEL:
        raise ValueError("provider_unconfirmed")
    prompt=(HERE/"r32c_prompt_v3.txt").read_text()
    allowed=Counter()
    for j in jobs:
        request=frozen.relation_request(j,prompt);request["max_tokens"]=j["max_tokens"]
        allowed[hashlib.sha256(json.dumps(request,ensure_ascii=False,sort_keys=True).encode()).hexdigest()]+=1
    used=Counter();lock=threading.Lock();local=threading.local();clients=[]
    report={"status":"RUNNING","manifest":snapshot,"http_attempts":0,"jobs":[]}
    frozen.OUT=OUT
    write_report(OUT/"matrix.json",report,[key])

    def guard(request):
        u=request.url
        if request.method!="POST" or u.scheme!="https" or u.host!="ollama.com" or u.path!="/v1/chat/completions" or u.query or u.userinfo or u.port not in (None,443):
            raise ValueError("unexpected_endpoint")
        digest=hashlib.sha256(json.dumps(json.loads(request.content),ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        with lock:
            if digest not in allowed or used[digest]>=allowed[digest]*2 or report["http_attempts"]>=1728:
                raise ValueError("wire_or_attempt_budget")
            used[digest]+=1;report["http_attempts"]+=1

    def evaluate(job):
        if not hasattr(local,"client"):
            local.client=OpenAI(api_key=key,base_url=base,max_retries=0,timeout=60,http_client=httpx.Client(
                trust_env=False,follow_redirects=False,event_hooks={"request":[guard]}))
            with lock:clients.append(local.client)
        return frozen.evaluate_relation_batch(job,r34_budget_client(local.client,job["max_tokens"]),prompt,key)

    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            for future in as_completed([executor.submit(evaluate,j) for j in jobs]):
                item=future.result();report["jobs"].append(item)
                write_report(OUT/"matrix.json",report,[key])
                if len(report["jobs"])%48==0 or not item["first_pass_valid"]:
                    print(json.dumps({"completed":len(report["jobs"]),"total":len(jobs),"arm":item["arm"],
                        "first_valid":item["first_pass_valid"],"eventual_valid":item["eventual_valid"],
                        "errors":[a["errors"] for a in item["attempts"]]}),flush=True)
        if any(a["metadata"].get("model_matches_requested") is False for j in report["jobs"] for a in j["attempts"]):
            raise ValueError("model_changed")
        report["status"]="COMPLETED"
    finally:
        for c in clients:c.close()
        write_report(OUT/"matrix.json",report,[key])


def r34_report():
    snapshot,cases,jobs=r34_current()
    raw=json.loads((OUT/"matrix.json").read_text())
    if raw["status"]!="COMPLETED" or raw["manifest"]!=snapshot or json.loads((OUT/"manifest.json").read_text())!=snapshot:
        raise ValueError("incomplete_or_changed_experiment")
    if sorted(j["job_id"] for j in jobs)!=sorted(j["job_id"] for j in raw["jobs"]):
        raise ValueError("schedule_mismatch")
    results={arm:r34_metrics([j for j in raw["jobs"] if j["arm"]==arm],cases) for arm,_,_ in ARMS}
    selected=r34_select(results)
    report={"manifest":snapshot,"results":results,"selected":selected,"production_ready":False}
    write_report(OUT/"summary.json",report)
    print(json.dumps({"selected":selected,"gates":{k:v["blocking_gates"] for k,v in results.items()}}),flush=True)


if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("action",choices=("prepare","run","summarize"))
    p.add_argument("--model-config",type=Path);p.add_argument("--matchmaker-config",type=Path)
    p.add_argument("--confirm-synthetic-provider-use",action="store_true");args=p.parse_args()
    logging.disable(logging.CRITICAL);warnings.filterwarnings("ignore")
    if any(os.environ.get(f)!="off" for f in FLAGS):p.error("semantic flags must explicitly stay OFF")
    if args.action=="run" and (not args.confirm_synthetic_provider_use or not args.model_config or not args.matchmaker_config):
        p.error("synthetic provider confirmation/config required")
    try:
        {"prepare":r34_prepare,"run":lambda:r34_run(args),"summarize":r34_report}[args.action]()
    except Exception:
        print("R3.4 STOPPED; inspect secret-safe artifacts. No automatic rerun.",flush=True)
        raise SystemExit(1) from None
