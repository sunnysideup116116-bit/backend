"""Fresh R3.4 final protocol; old pair identities used only for novelty exclusion."""
from collections import Counter
from pathlib import Path
from importlib import metadata
import hashlib,json,random,subprocess,sys
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2];sys.path.insert(0,str(ROOT))
from matchmaker_agent.concept_identity import canonicalize_concept,canonicalize_fresh_concept,stored_concept_identity
from r32c_relation import RELATION_TO_PRIMARY,map_relation
from run_r32_offline import condition_summary
CANDIDATE_COMMIT="132cba404b4ac8294308f22e93aa5573d54de23c"


def r34_final_plan():
    path="tests/readiness/neo4j/r34_selected_config.json"
    source=(ROOT/path).read_bytes()
    if source!=subprocess.check_output(["git","show",CANDIDATE_COMMIT+":"+path],cwd=ROOT):
        raise ValueError("candidate_changed")
    config=json.loads(source);cfg=config["configuration"]
    for name,digest in config["frozen_sources"].items():
        if hashlib.sha256((HERE/name).read_bytes()).hexdigest()!=digest:raise ValueError("semantic_source_changed")
    if {k:metadata.version(k) for k in config["sdk_versions"]}!=config["sdk_versions"]:raise ValueError("sdk_changed")
    raw=json.loads((HERE/"r34_final_holdout.json").read_text())
    if raw["candidate_freeze_commit"]!=CANDIDATE_COMMIT or raw["data_classification"]!="synthetic_non_user_data" or len(raw["groups"])!=80:
        raise ValueError("invalid_final_fixture")
    old=json.loads((HERE/"r3_holdout.json").read_text())["groups"]
    retired=json.loads((HERE/"r33_holdout.json").read_text())["groups"]
    r25=json.loads((HERE/"fixtures.json").read_text())["semantic_pairs"]
    previous=set()
    for func in (canonicalize_concept,canonicalize_fresh_concept):
        for a,b in [(g["a"],g["b"]) for g in old+retired]+[(g["left"],g["right"]) for g in r25]:
            ka,kb=func(a).key,func(b).key;previous.update(((ka,kb),(kb,ka)))
    cases=[];seen=set();prefix_count=0
    for g in raw["groups"]:
        identities=[canonicalize_fresh_concept(g[k]) for k in ("a","b")]
        plain=[canonicalize_concept(g[k]) for k in ("a","b")]
        for pair in (tuple(i.key for i in identities),tuple(i.key for i in plain)):
            if pair in previous or pair[0]==pair[1]:raise ValueError("old_or_exact_pair")
        pair=tuple(i.key for i in identities)
        if pair in seen:raise ValueError("duplicate_pair")
        seen.update((pair,tuple(reversed(pair))))
        if g["prefix40"]:
            assert identities[0].semantic_text[:40]==identities[1].semantic_text[:40]
            assert min(len(i.semantic_text) for i in identities)>40
            prefix_count+=1
        assert all(stored_concept_identity(i.as_dict())==i for i in identities)
        for d,qi,ci,languages in (("f",0,1,g["languages"]),("r",1,0,list(reversed(g["languages"])))):
            gold=g[d]
            if map_relation(gold["relation"])!=gold["primary"] or not gold["rationale"]:raise ValueError("gold_changed")
            cases.append({"id":g["id"]+"-"+d,"Q":identities[qi].semantic_text,"C":identities[ci].semantic_text,
                "source_Q":g["a" if qi==0 else "b"],"source_C":g["a" if ci==0 else "b"],
                "expected":gold["primary"],"relation":gold["relation"],"category":g["category"],
                "language":"-".join(languages),"rationale":gold["rationale"],"prefix40":g["prefix40"]})
    assert len(cases)==160 and len({c["id"] for c in cases})==160 and prefix_count>=8
    assert Counter(c["language"] for c in cases)==dict.fromkeys(("en-en","zh-zh","en-zh","zh-en"),40)
    jobs=[]
    for repeat,seed in enumerate(cfg["seeds"],1):
        ordered=list(cases);random.Random(seed).shuffle(ordered)
        for offset in range(0,160,cfg["batch_size"]):
            jobs.append({"job_id":f"r34final-r{repeat}-b{offset//cfg['batch_size']+1:03}",
                "repeat":repeat,"batch_size":cfg["batch_size"],"cases":ordered[offset:offset+cfg["batch_size"]]})
    random.Random(cfg["schedule_seed"]).shuffle(jobs)
    return config,cases,jobs,{"categories":dict(Counter(c["category"] for c in cases)),
        "languages":dict(Counter(c["language"] for c in cases)),"gold":dict(Counter(c["expected"] for c in cases)),
        "old_pair_overlap":0,"same_prefix40_pairs":prefix_count,"max_chars":max(len(c[k]) for c in cases for k in ("Q","C"))}


def r34_final_metrics(jobs,cases):
    by_id={c["id"]:c for c in cases};observed={}
    for j in jobs:
        if not 1<=len(j["attempts"])<=2 or len(j["ids"])!=2:raise ValueError("attempt_or_batch_changed")
        if len(j["attempts"])==2 and j["attempts"][0]["valid"]:raise ValueError("valid_retried")
        for a in j["attempts"]:
            if not a["valid"] and a["decisions"]:raise ValueError("ERROR_fail_open_or_salvage")
            if a["valid"]:
                assert set(a["decisions"])==set(j["ids"]) and not a["errors"]
                assert all(row["id"]==cid and row["decision"]==map_relation(row["relation"]) for cid,row in a["decisions"].items())
        for cid in j["ids"]:
            assert cid in by_id and (cid,j["repeat"]) not in observed
            observed[(cid,j["repeat"])]=j["attempts"][-1]["decisions"].get(cid,{"decision":"ERROR","relation":"ERROR"})
    assert set(observed)=={(cid,r) for cid in by_id for r in (1,2,3)}
    result=condition_summary(jobs,cases,"R3.4-fresh-final")
    result.pop("proposed_engineering_gates");result.pop("all_gates_pass");result.pop("special_cases")
    consistent=sum(len({observed[(cid,r)]["decision"]=="YES" for r in (1,2,3)})==1 for cid in by_id)
    yes=result["final_pooled_yes"]
    fractions={"precision":(yes["TP"],yes["TP"]+yes["FP"],99),"recall":(yes["TP"],yes["TP"]+yes["FN"],95),
        "consistency":(consistent,160,99),"first_valid":(result["first_valid"],len(jobs),99),
        "eventual_valid":(result["eventual_valid"],len(jobs),99)}
    gates={k:d>0 and n*100>=d*t for k,(n,d,t) in fractions.items()}
    broad=sum(o["decision"]=="YES" for (cid,_),o in observed.items() if by_id[cid]["relation"]=="candidate_more_broad")
    gates.update(broad_zero=broad==0,role_zero=result["role_false_yes"]==0,
                 constraint_zero=result["constraint_false_yes"]==0,error_fail_open_zero=True)
    rows=[{**c,"predictions":[{"round":r,**observed[(c["id"],r)]} for r in (1,2,3)]} for c in cases]
    result.update(PASS=all(gates.values()),blocking_gates=gates,acceptance_consistency=consistent/160,
        acceptance_consistent_cases=consistent,broad_to_specific_false_accept=broad,error_fail_open=0,
        relation_consistency=result["secondary_consistent_cases"]/160,
        false_accepts=[r for r in rows if r["expected"]!="YES" and any(p["decision"]=="YES" for p in r["predictions"])],
        false_rejects=[r for r in rows if r["expected"]=="YES" and any(p["decision"]!="YES" for p in r["predictions"])],
        relation_mistakes=[r for r in rows if any(p["relation"]!=r["relation"] for p in r["predictions"])],
        scope="160 new final cases; ERROR remains explicit; R3.3 remains retired FAIL",production_ready=False)
    return result
