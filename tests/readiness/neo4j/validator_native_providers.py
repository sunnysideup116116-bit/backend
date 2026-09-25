"""Secret-safe native schema adapters for OFFLINE validator comparison only."""
from __future__ import annotations
import json,logging,threading,time,re
from pathlib import Path
import httpx
from run_r3_offline import selected_config
from r32c_relation import RELATION_TO_PRIMARY,classify_relation_response
HERE=Path(__file__).resolve().parent
PROFILES={
 "gemini35":{"provider":"google","model":"gemini-3.5-flash-lite","thinking":{"thinkingLevel":"minimal"}},
 "gemini31":{"provider":"google","model":"gemini-3.1-flash-lite","thinking":{"thinkingLevel":"minimal"}},
 "gemini25":{"provider":"google","model":"gemini-2.5-flash","thinking":{"thinkingBudget":1024}},
 "qwen_local":{"provider":"ollama_local","model":"qwen3.5:0.8b","thinking":False},
}


def relation_schema(ids):
    return {"type":"object","properties":{"results":{"type":"array","minItems":len(ids),"maxItems":len(ids),
        "items":{"type":"object","properties":{"id":{"type":"string","enum":list(ids)},
        "relation":{"type":"string","enum":list(RELATION_TO_PRIMARY)}},"required":["id","relation"],"additionalProperties":False}}},
        "required":["results"],"additionalProperties":False}


class NativeProviders:
    def __init__(self):
        selected=selected_config("/home/sunny/桌面/Graduate_Project/Server/.env",
                                 {f"GOOGLE_API_KEYS{i}" for i in range(1,7)})
        self.keys=list(dict.fromkeys(v for v in selected.values() if v))
        del selected
        if not self.keys:raise ValueError("google_pool_unavailable")
        self.cursor=0;self.lock=threading.Lock();self.next_google=0.;self.calls=0
        self.pools={};self.model_cursor={}
        self.client=httpx.Client(timeout=60,trust_env=False,follow_redirects=False)
        self.local_lock=threading.Lock()

    def close(self):
        self.client.close()

    def bind_pool(self,name):
        """One schema-only availability check/key, before development; no key identity saved."""
        schema={"type":"object","properties":{"value":{"type":"string","enum":["binding_ok"]}},"required":["value"],"additionalProperties":False}
        eligible=[];statuses={}
        for key in self.keys:
            result=self.one(name,"Return a schema-compliant object.","binding test",schema,_credential=key)
            status=str(result["status"]);statuses[status]=statuses.get(status,0)+1
            try:good=result["api_success"] and result["finish_reason"]=="stop" and json.loads(result.get("text",""))=={"value":"binding_ok"}
            except Exception:good=False
            if good:eligible.append(key)
        self.pools[name]=eligible;self.model_cursor[name]=0
        return {"checked":len(self.keys),"usable":len(eligible),"status_counts":statuses}

    def one(self,name,system,user,schema,_credential=None):
        profile=PROFILES[name];start=time.perf_counter()
        with self.lock:
            self.calls+=1
            if self.calls>1000:raise ValueError("hard_api_budget_exhausted")
        pacing=0.;header={}
        if profile["provider"]=="google":
            with self.lock:
                slot=max(time.monotonic(),self.next_google);self.next_google=slot+2.1
                pool=self.pools.get(name,self.keys)
                if not pool and _credential is None:raise ValueError("no_usable_model_credential")
                cursor=self.model_cursor.get(name,0)
                key=_credential if _credential is not None else pool[cursor%len(pool)]
                if _credential is None:self.model_cursor[name]=cursor+1
            pacing=max(0,slot-time.monotonic())
            if pacing:time.sleep(pacing)
            header={"x-goog-api-key":key}
            url="https://generativelanguage.googleapis.com/v1beta/models/"+profile["model"]+":generateContent"
            body={"systemInstruction":{"parts":[{"text":system}]},"contents":[{"role":"user","parts":[{"text":user}]}],
                  "generationConfig":{"temperature":0,"maxOutputTokens":4096,
                      "thinkingConfig":profile["thinking"],"responseMimeType":"application/json","responseJsonSchema":schema}}
        else:
            url="http://127.0.0.1:11434/api/chat"
            body={"model":profile["model"],"stream":False,"think":False,"keep_alive":"30s","format":schema,
                  "messages":[{"role":"system","content":system},{"role":"user","content":user}],
                  "options":{"temperature":0,"num_predict":4096,"num_ctx":8192,"num_thread":2}}
        result={"api_success":False,"status":None,"errors":[],"retryable":False,"usage":None}
        provider_start=time.perf_counter()
        try:
            if profile["provider"]=="ollama_local":
                with self.local_lock:
                    provider_start=time.perf_counter()
                    response=self.client.post(url,json=body)
            else:
                response=self.client.post(url,json=body,headers=header)
            result["status"]=response.status_code
            if response.status_code!=200:
                status=response.status_code
                result.update(errors=["auth_failure" if status in (401,403) else "quota_exhausted" if status==429 else "provider_contract_failure" if status in (400,404,422) else "provider_failure"],
                              retryable=status in (408,409,429) or status>=500)
                return result
            obj=response.json()
            if profile["provider"]=="google":
                candidate=(obj.get("candidates") or [{}])[0]
                text="".join(p.get("text","") for p in candidate.get("content",{}).get("parts",[]) if not p.get("thought"))
                finish=candidate.get("finishReason","unknown")
                usage=obj.get("usageMetadata",{})
                version=obj.get("modelVersion","unknown")
                result.update(api_success=True,text=text,finish_reason={"STOP":"stop","MAX_TOKENS":"length"}.get(finish,"content_filter"),
                    resolved_model=version if isinstance(version,str) and re.fullmatch(r"gemini-[A-Za-z0-9_.-]{1,100}",version) else "unknown",
                    usage={"prompt_tokens":int(usage.get("promptTokenCount",0)),"completion_tokens":int(usage.get("candidatesTokenCount",0))+int(usage.get("thoughtsTokenCount",0)),
                        "visible_output_tokens":int(usage.get("candidatesTokenCount",0)),"reasoning_tokens":int(usage.get("thoughtsTokenCount",0)),
                        "total_tokens":int(usage.get("totalTokenCount",0))})
            else:
                finish=obj.get("done_reason","unknown")
                result.update(api_success=True,text=obj.get("message",{}).get("content",""),
                    thinking_present=bool(obj.get("message",{}).get("thinking")),
                    finish_reason="stop" if finish=="stop" else "length" if finish=="length" else "unknown",
                    resolved_model=obj.get("model") if obj.get("model")==profile["model"] else "unknown",usage={"prompt_tokens":int(obj.get("prompt_eval_count",0)),
                        "completion_tokens":int(obj.get("eval_count",0)),"reasoning_tokens":None,
                        "total_tokens":int(obj.get("prompt_eval_count",0))+int(obj.get("eval_count",0))})
            return result
        except httpx.TimeoutException:
            result.update(errors=["provider_timeout"],retryable=True);return result
        except httpx.TransportError:
            result.update(errors=["provider_transport_failure"],retryable=True);return result
        except Exception:
            result.update(errors=["parser_internal_failure"]);return result
        finally:
            result.update(latency_seconds=time.perf_counter()-provider_start,
                          wall_seconds=time.perf_counter()-start,pacing_seconds=pacing)


def native_evaluate(job,providers,prompt):
    result={k:v for k,v in job.items() if k!="cases"}
    ids=[c["id"] for c in job["cases"]];result.update(ids=ids,attempts=[])
    user=json.dumps({"pairs":[{k:c[k] for k in ("id","Q","C")}for c in job["cases"]]},ensure_ascii=False)
    start=time.perf_counter()
    for number in (1,2):
        raw=providers.one(job["backend"],prompt,user,relation_schema(ids))
        if raw["api_success"] and raw["resolved_model"]!=PROFILES[job["backend"]]["model"]:
            raw.pop("text",None)
            raw.update(api_success=False,errors=["provider_model_mismatch"],retryable=False)
        if raw["api_success"]:
            parsed=classify_relation_response(raw.pop("text"),raw["finish_reason"],set(ids))
            record={**parsed,"usage":raw["usage"],"latency_seconds":raw["latency_seconds"],
                    "wall_seconds":raw["wall_seconds"],"pacing_seconds":raw["pacing_seconds"],
                    "resolved_model":raw["resolved_model"]}
            retry=not record["valid"] and not set(record["errors"])&{"parser_internal_failure","unexpected_finish_reason"}
        else:
            record={"valid":False,"decisions":{},"errors":raw["errors"],"metadata":{"finish_reason":"unknown","http_status":raw["status"]},
                    "usage":None,"latency_seconds":raw["latency_seconds"],"wall_seconds":raw["wall_seconds"],
                    "pacing_seconds":raw["pacing_seconds"]}
            retry=raw["retryable"]
        record["attempt"]=number;result["attempts"].append(record)
        if record["valid"] or not retry or number==2:break
        time.sleep(.5)
    result.update(first_pass_valid=result["attempts"][0]["valid"],eventual_valid=result["attempts"][-1]["valid"],
                  elapsed_seconds=time.perf_counter()-start)
    return result
