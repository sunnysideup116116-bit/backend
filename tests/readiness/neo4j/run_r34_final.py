"""Bounded fresh final evaluation, only after candidate and labels are committed."""
import argparse,hashlib,json,logging,os,subprocess,threading,warnings
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
import run_r32c_offline as frozen
from run_r3_offline import FLAGS,selected_config,write_report
from r34_final_contract import HERE,ROOT,CANDIDATE_COMMIT,r34_final_plan,r34_final_metrics
OUT=HERE/"artifacts/r34_final"


def final_current():
    cfg,cases,jobs,audit=r34_final_plan()
    files=("r34_selected_config.json","R34_CANDIDATE_FREEZE.md","r34_final_holdout.json",
           "r34_final_contract.py","run_r34_final.py","test_r34_final.py")
    paths=["tests/readiness/neo4j/"+n for n in files]
    if subprocess.check_output(["git","status","--porcelain","--untracked-files=all","--",*paths],cwd=ROOT,text=True):
        raise ValueError("commit_freeze_before_scoring")
    h=lambda obj:hashlib.sha256(json.dumps(obj,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    snap={"experiment":"R3.4-fresh-final","candidate_commit":CANDIDATE_COMMIT,
        "source_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
        "source_hashes":{n:hashlib.sha256((HERE/n).read_bytes()).hexdigest() for n in files},
        "configuration":cfg,"inputs_sha256":h(cases),"schedule_sha256":h(jobs),"coverage":audit,
        "no_production_graph":True,"production_ready":False}
    subprocess.run(["git","merge-base","--is-ancestor",CANDIDATE_COMMIT,snap["source_commit"]],cwd=ROOT,check=True,capture_output=True)
    return snap,cases,jobs


def final_prepare():
    s,c,j=final_current()
    if (OUT/"manifest.json").exists():raise ValueError("no_overwrite")
    write_report(OUT/"manifest.json",s);write_report(OUT/"inputs.json",{"synthetic_only":True,"cases":c})
    print(json.dumps({"status":"FROZEN","requests":len(j),"coverage":s["coverage"]}),flush=True)


def final_run(args):
    import httpx
    from openai import OpenAI
    s,_,jobs=final_current()
    if json.loads((OUT/"manifest.json").read_text())!=s or (OUT/"matrix.json").exists():raise ValueError("freeze_or_overwrite")
    selected=selected_config(args.matchmaker_config,{"LLM_API_KEY","LLM_BASE_URL"})
    model=selected_config(args.model_config,{"LLM_MODEL_ID"}).get("LLM_MODEL_ID")
    key,base=selected.get("LLM_API_KEY"),selected.get("LLM_BASE_URL","");del selected
    if not key or base.rstrip("/")!="https://ollama.com/v1" or model!=frozen.MODEL:raise ValueError("provider_unconfirmed")
    assert s["configuration"]["configuration"]["max_tokens"]==4096
    prompt=(HERE/"r32c_prompt_v3.txt").read_text();frozen.OUT=OUT
    allowed=Counter(hashlib.sha256(json.dumps(frozen.relation_request(j,prompt),ensure_ascii=False,sort_keys=True).encode()).hexdigest() for j in jobs)
    consumed=Counter();lock=threading.Lock();local=threading.local();clients=[]
    report={"status":"RUNNING","manifest":s,"http_attempts":0,"jobs":[]}
    write_report(OUT/"matrix.json",report,[key])
    def guard(request):
        u=request.url
        if request.method!="POST" or u.scheme!="https" or u.host!="ollama.com" or u.path!="/v1/chat/completions" or u.query or u.userinfo or u.port not in (None,443):raise ValueError("endpoint")
        digest=hashlib.sha256(json.dumps(json.loads(request.content),ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        with lock:
            if digest not in allowed or consumed[digest]>=allowed[digest]*2 or report["http_attempts"]>=480:raise ValueError("wire_budget")
            consumed[digest]+=1;report["http_attempts"]+=1
    def evaluate(job):
        if not hasattr(local,"client"):
            local.client=OpenAI(api_key=key,base_url=base,max_retries=0,timeout=60,http_client=httpx.Client(
                trust_env=False,follow_redirects=False,event_hooks={"request":[guard]}))
            with lock:clients.append(local.client)
        return frozen.evaluate_relation_batch(job,local.client,prompt,key)
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            for future in as_completed([pool.submit(evaluate,j) for j in jobs]):
                item=future.result();report["jobs"].append(item);write_report(OUT/"matrix.json",report,[key])
                if len(report["jobs"])%24==0 or not item["first_pass_valid"]:
                    print(json.dumps({"completed":len(report["jobs"]),"total":len(jobs),"first_valid":item["first_pass_valid"],
                        "eventual_valid":item["eventual_valid"],"errors":[a["errors"] for a in item["attempts"]]}),flush=True)
        if any(a["metadata"].get("model_matches_requested") is False for j in report["jobs"] for a in j["attempts"]):raise ValueError("model_changed")
        report["status"]="COMPLETED"
    finally:
        for c in clients:c.close()
        write_report(OUT/"matrix.json",report,[key])


def final_report():
    s,c,j=final_current();d=json.loads((OUT/"matrix.json").read_text())
    assert d["status"]=="COMPLETED" and d["manifest"]==s and json.loads((OUT/"manifest.json").read_text())==s
    assert sorted(v["job_id"] for v in d["jobs"])==sorted(v["job_id"] for v in j)
    r={"manifest":s,"results":r34_final_metrics(d["jobs"],c)}
    write_report(OUT/"summary.json",r)
    print(json.dumps({k:r["results"][k] for k in ("PASS","blocking_gates","final_pooled_yes","acceptance_consistency","first_pass_rate","eventual_rate")}),flush=True)


if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("action",choices=("prepare","run","summarize"))
    p.add_argument("--model-config",type=Path);p.add_argument("--matchmaker-config",type=Path)
    p.add_argument("--confirm-synthetic-provider-use",action="store_true");a=p.parse_args()
    logging.disable(logging.CRITICAL);warnings.filterwarnings("ignore")
    if any(os.environ.get(f)!="off" for f in FLAGS):p.error("flags must remain OFF")
    if a.action=="run" and (not a.confirm_synthetic_provider_use or not a.model_config or not a.matchmaker_config):p.error("synthetic authorization/config required")
    try:{"prepare":final_prepare,"run":lambda:final_run(a),"summarize":final_report}[a.action]()
    except Exception:
        print("R3.4 final STOPPED; no automatic rerun. Inspect secret-safe artifacts.",flush=True)
        raise SystemExit(1) from None
