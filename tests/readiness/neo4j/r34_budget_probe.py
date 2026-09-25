"""Eight predeclared development-only budget probes; no semantic selection."""
import argparse,json,logging,os,time,warnings,hashlib
from pathlib import Path
from functools import partial
from r32c_relation import load_complete_cases,classify_relation_response
from r31_diagnostics import classify_provider_error
from run_r3_offline import selected_config,write_report,FLAGS
from run_r32c_offline import relation_request,MODEL

HERE=Path(__file__).resolve().parent
OUT=HERE/"artifacts/r34/budget_probe.json"
PAIRS=(("h17-f","h48-r"),("h17-f","h48-r"),("h33-f","h33-r"),("h12-r","h36-f"),
       ("h17-f","h48-r"),("h33-f","h36-f"),("h11-r","h47-r"),("h17-f","h48-r"))


def r34_probe(args):
    import httpx
    from openai import OpenAI
    if OUT.exists():
        raise ValueError("refusing_to_overwrite_probe")
    assert all(os.environ.get(f)=="off" for f in FLAGS)
    cases={c["id"]:c for c in load_complete_cases()}
    prompt=(HERE/"r32c_prompt_v3.txt").read_text()
    settings=selected_config(args.matchmaker_config,{"LLM_API_KEY","LLM_BASE_URL"})
    model=selected_config(args.model_config,{"LLM_MODEL_ID"}).get("LLM_MODEL_ID")
    key,base=settings.get("LLM_API_KEY"),settings.get("LLM_BASE_URL","")
    del settings
    if not key or base.rstrip("/")!="https://ollama.com/v1" or model!=MODEL:
        raise ValueError("provider_unconfirmed")
    report={"status":"RUNNING","probe_model":MODEL,"high_budget":8192,"rows":[],"http_attempts":0,
        "prompt_sha256":hashlib.sha256(prompt.encode()).hexdigest(),"source_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "protocol_sha256":hashlib.sha256((HERE/"R34_PROTOCOL.md").read_bytes()).hexdigest(),
        "dataset":"frozen_96_development_only","no_R33_input":True,"graph_access":False}

    def guard(request):
        u=request.url
        if request.method!="POST" or u.scheme!="https" or u.host!="ollama.com" or u.path!="/v1/chat/completions" or u.query or u.userinfo or u.port not in (None,443):
            raise ValueError("unexpected_endpoint")
        if report["http_attempts"]>=8:
            raise ValueError("probe_budget_exhausted")
        report["http_attempts"]+=1

    client=OpenAI(api_key=key,base_url=base,max_retries=0,timeout=60,http_client=httpx.Client(
        trust_env=False,follow_redirects=False,event_hooks={"request":[guard]}))
    write_report(OUT,report,[key])
    try:
        for index,ids in enumerate(PAIRS):
            budget=64 if index==0 else 8192
            req=relation_request({"cases":[cases[i] for i in ids]},prompt)
            req["max_tokens"]=budget
            start=time.perf_counter()
            row={"probe":index+1,"max_tokens":budget,"ids":list(ids)}
            try:
                response=client.chat.completions.create(**req)
                choice=response.choices[0]
                parsed=classify_relation_response(choice.message.content,choice.finish_reason,set(ids))
                row.update(api_success=True,model_matches=response.model==MODEL,
                    finish_reason=parsed["metadata"]["finish_reason"],schema_valid=parsed["valid"],
                    completion_tokens=int(response.usage.completion_tokens) if response.usage else None)
            except Exception as exc:
                row.update(api_success=False,provider_error=classify_provider_error(exc))
            row["latency_seconds"]=time.perf_counter()-start
            report["rows"].append(row)
            write_report(OUT,report,[key])
            print(json.dumps(row),flush=True)
        high=report["rows"][1:]
        accepted=all(r.get("api_success") and r.get("model_matches") and type(r.get("completion_tokens")) is int and r["completion_tokens"]<=8192 for r in high)
        over4096=any(4096<r.get("completion_tokens",0)<=8192 for r in high)
        report.update(status="COMPLETED",high_api_accepted=accepted,observed_above_4096=over4096,
            higher_budget_confirmed=accepted and over4096,
            model_revision="unknown",global_max_output_limit="unknown")
    finally:
        client.close()
        write_report(OUT,report,[key])


if __name__=="__main__":
    logging.disable(logging.CRITICAL);warnings.filterwarnings("ignore")
    p=argparse.ArgumentParser()
    p.add_argument("--model-config",type=Path,required=True)
    p.add_argument("--matchmaker-config",type=Path,required=True)
    p.add_argument("--confirm-synthetic-provider-use",action="store_true")
    args=p.parse_args()
    if not args.confirm_synthetic_provider_use:
        p.error("synthetic provider confirmation required")
    try:
        r34_probe(args)
    except Exception:
        print("R3.4 budget probe STOPPED; inspect secret-safe metadata.",flush=True)
        raise SystemExit(1) from None
