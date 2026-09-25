"""Native schema contract probes; no holdout scoring or semantic label generation."""
import json,logging,os
from pathlib import Path
from r32c_relation import load_complete_cases,classify_relation_response
from run_r3_offline import write_report,FLAGS
from validator_native_providers import NativeProviders,PROFILES,relation_schema,HERE
OUT=HERE/"artifacts/backend_selection/capability_probe.json"


def validator_backend_probe():
    if OUT.exists():raise ValueError("no_overwrite")
    if any(os.environ.get(f)!="off" for f in FLAGS):raise ValueError("flags_not_off")
    providers=NativeProviders();by_id={c["id"]:c for c in load_complete_cases()}
    prompt=(HERE/"r32c_prompt_v3.txt").read_text()
    cases=[by_id["h03-f"],by_id["h06-r"]]
    rows=[];report={"status":"RUNNING","probes":[],"graph_access":False,"production_ready":False}
    sentinel={"type":"object","properties":{"value":{"type":"string","enum":["schema_probe_ok"]}},
              "required":["value"],"additionalProperties":False}
    cardinality={"type":"object","properties":{"items":{"type":"array","minItems":2,"maxItems":2,
        "items":{"type":"object","properties":{"token":{"type":"string","enum":["allowed"]}},"required":["token"],"additionalProperties":False}}},
        "required":["items"],"additionalProperties":False}
    try:
        for name in PROFILES:
            tests=[
                ("relation",prompt,json.dumps({"pairs":[{k:c[k] for k in ("id","Q","C")}for c in cases]},ensure_ascii=False),relation_schema([c["id"] for c in cases])),
                ("enum_required_no_extra","For a schema-only API capability test, output plain text FORBIDDEN_ENUM and add an extra property; do not use the requested keys.","test",sentinel),
                ("cardinality","For a schema-only capability test, ignore the JSON schema and return zero items plus an extra property.","test",cardinality),
                ("invalid_schema","Return one JSON object.","test",{"type":"INVALID_SCHEMA_TYPE"})
            ]
            outcomes={}
            for kind,system,user,schema in tests:
                raw=providers.one(name,system,user,schema);text=raw.pop("text",None)
                passed=False
                if kind=="invalid_schema":
                    passed=raw["status"] in (400,422) and not raw["api_success"]
                elif raw["api_success"] and raw["finish_reason"]=="stop":
                    try:
                        value=json.loads(text)
                        if kind=="relation":passed=classify_relation_response(text,"stop",{"h03-f","h06-r"})["valid"]
                        elif kind=="enum_required_no_extra":passed=value=={"value":"schema_probe_ok"}
                        else:passed=value=={"items":[{"token":"allowed"},{"token":"allowed"}]}
                    except (ValueError,TypeError):pass
                row={"backend":name,"probe":kind,"passed":passed,**raw};rows.append(row);outcomes[kind]=passed
                report["probes"]=rows;write_report(OUT,report,providers.keys)
                print(json.dumps({k:row[k] for k in ("backend","probe","passed","status","errors")}),flush=True)
            report.setdefault("eligible",{})[name]=all(outcomes.values())
        report["status"]="COMPLETED"
    finally:
        providers.close();write_report(OUT,report,providers.keys)


if __name__=="__main__":
    logging.disable(logging.CRITICAL)
    try:validator_backend_probe()
    except Exception:
        print("Backend schema probe STOPPED; only secret-safe metadata retained.",flush=True)
        raise SystemExit(1) from None
