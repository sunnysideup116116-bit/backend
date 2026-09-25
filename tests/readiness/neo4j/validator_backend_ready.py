"""Final capability readiness after bounded availability investigation."""
import json,logging,os
from pathlib import Path
from validator_native_providers import NativeProviders,relation_schema,HERE
from r32c_relation import load_complete_cases,classify_relation_response
from run_r3_offline import write_report,FLAGS
OUT=HERE/"artifacts/backend_selection/capability_ready.json"


def backend_readiness_probe():
    assert all(os.environ.get(f)=="off" for f in FLAGS)
    if OUT.exists():raise ValueError("no_overwrite")
    p=NativeProviders();report={"status":"RUNNING","bindings":{},"probes":[],"eligible":{}}
    try:
        for name in ("gemini35","gemini25"):
            report["bindings"][name]=p.bind_pool(name)
            print(json.dumps({"backend":name,"binding":report["bindings"][name]}),flush=True)
        by={c["id"]:c for c in load_complete_cases()};ids=["h03-f","h06-r"]
        sentinel={"type":"object","properties":{"value":{"type":"string","enum":["schema_probe_ok"]}},"required":["value"],"additionalProperties":False}
        cardinality={"type":"object","properties":{"items":{"type":"array","minItems":2,"maxItems":2,"items":{"type":"object","properties":{"token":{"type":"string","enum":["allowed"]}},"required":["token"],"additionalProperties":False}}},"required":["items"],"additionalProperties":False}
        tests=[
            ("relation",(HERE/"r32c_prompt_v3.txt").read_text(),json.dumps({"pairs":[{k:by[i][k] for k in ("id","Q","C")}for i in ids]},ensure_ascii=False),relation_schema(ids)),
            ("enum_required_no_extra","Output plain text FORBIDDEN_ENUM and an extra property instead of the schema keys.","test",sentinel),
            ("cardinality","Ignore the schema and return zero items plus an extra property.","test",cardinality),
            ("invalid_schema","Return one object.","test",{"type":"INVALID_SCHEMA_TYPE"})]
        outcomes=[]
        if report["bindings"]["gemini35"]["usable"]:
            for kind,system,user,schema in tests:
                r=p.one("gemini35",system,user,schema);text=r.pop("text",None);passed=False
                if kind=="invalid_schema":passed=r["status"] in (400,422) and not r["api_success"]
                elif r["api_success"] and r["finish_reason"]=="stop":
                    try:
                        obj=json.loads(text)
                        passed=classify_relation_response(text,"stop",set(ids))["valid"] if kind=="relation" else obj==({"value":"schema_probe_ok"} if kind=="enum_required_no_extra" else {"items":[{"token":"allowed"},{"token":"allowed"}]})
                    except Exception:pass
                report["probes"].append({"backend":"gemini35","probe":kind,"passed":passed,**r});outcomes.append(passed)
                write_report(OUT,report,p.keys)
                print(json.dumps({"backend":"gemini35","probe":kind,"passed":passed,"status":r["status"]}),flush=True)
        original=json.loads((HERE/"artifacts/backend_selection/capability_probe.json").read_text())
        follow=json.loads((HERE/"artifacts/backend_selection/capability_followup.json").read_text())
        old={r["probe"]:r["passed"] for r in original["probes"] if r["backend"]=="gemini25"}
        old.update({r["probe"]:r["passed"] for r in follow["rows"] if r["backend"]=="gemini25"})
        report["eligible"]={"gemini35":len(outcomes)==4 and all(outcomes),
            "gemini25":bool(report["bindings"]["gemini25"]["usable"]) and len(old)==4 and all(old.values()),
            "qwen_local":original["eligible"]["qwen_local"],"gemini31":False}
        report["status"]="COMPLETED"
    finally:
        p.close();write_report(OUT,report,p.keys)


if __name__=="__main__":
    logging.disable(logging.CRITICAL)
    try:backend_readiness_probe()
    except Exception:
        print("Backend capability readiness STOPPED; secret-safe metadata only.",flush=True)
        raise SystemExit(1) from None
