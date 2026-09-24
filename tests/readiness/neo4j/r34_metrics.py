"""R3.4 development-only binary gates; no retired holdout imports."""
from collections import Counter
from r32c_relation import map_relation
from run_r32_offline import condition_summary


def r34_metrics(jobs,cases):
    if len(cases)!=96 or len({c["id"] for c in cases})!=96:
        raise ValueError("development_96_only")
    by_id={c["id"]:c for c in cases}
    outputs={}
    for j in jobs:
        if j["repeat"] not in (1,2,3) or len(j["ids"])!=j["batch_size"] or j["batch_size"] not in (1,2):
            raise ValueError("bad_schedule")
        if not 1<=len(j["attempts"])<=2 or (len(j["attempts"])==2 and j["attempts"][0]["valid"]):
            raise ValueError("retry_policy_changed")
        for a in j["attempts"]:
            if not a["valid"] and a["decisions"]:
                raise ValueError("ERROR_fail_open_or_salvage")
            if a["valid"]:
                if set(a["decisions"])!=set(j["ids"]) or a["errors"]:
                    raise ValueError("invalid_valid_batch")
                for cid,row in a["decisions"].items():
                    if row["id"]!=cid or row["decision"]!=map_relation(row["relation"]):
                        raise ValueError("primary_override")
        for cid in j["ids"]:
            if cid not in by_id or (cid,j["repeat"]) in outputs:
                raise ValueError("duplicate_or_unknown_case")
            outputs[(cid,j["repeat"])]=j["attempts"][-1]["decisions"].get(cid,{"decision":"ERROR","relation":"ERROR"})
    if set(outputs)!={(cid,r) for cid in by_id for r in (1,2,3)}:
        raise ValueError("incomplete_rounds")
    result=condition_summary(jobs,cases,"R3.4-development")
    result["historical_three_way_gates_diagnostic_only"]=result.pop("proposed_engineering_gates")
    result.pop("all_gates_pass",None)
    consistent=sum(len({outputs[(cid,r)]["decision"]=="YES" for r in (1,2,3)})==1 for cid in by_id)
    broad=sum(row["decision"]=="YES" for (cid,_),row in outputs.items() if by_id[cid]["relation"]=="candidate_more_broad")
    yes=result["final_pooled_yes"]
    ratio={"first_pass_validity":(result["first_valid"],len(jobs),99),
        "eventual_validity":(result["eventual_valid"],len(jobs),99),
        "acceptance_precision":(yes["TP"],yes["TP"]+yes["FP"],99),
        "acceptance_recall":(yes["TP"],yes["TP"]+yes["FN"],95),
        "acceptance_consistency":(consistent,96,99)}
    gates={name:den>0 and num*100>=den*threshold for name,(num,den,threshold) in ratio.items()}
    gates.update(broad_zero=broad==0,role_zero=result["role_false_yes"]==0,
                 constraint_zero=result["constraint_false_yes"]==0,error_fail_open_zero=True)
    records=[]
    for c in cases:
        predictions=[{"round":r,**outputs[(c["id"],r)]} for r in (1,2,3)]
        records.append({**c,"predictions":predictions})
    result.update(acceptance_consistent_cases=consistent,acceptance_consistency=consistent/96,
        broad_to_specific_false_accept=broad,error_fail_open=0,blocking_gates=gates,PASS=all(gates.values()),
        false_accepts=[c for c in records if c["expected"]!="YES" and any(p["decision"]=="YES" for p in c["predictions"])],
        false_rejects=[c for c in records if c["expected"]=="YES" and any(p["decision"]!="YES" for p in c["predictions"])],
        relation_consistency=result["secondary_consistent_cases"]/96,
        case_observations=288,scope="R3.4 96-case development only, no R3.3 reuse")
    return result


def r34_select(results):
    baseline=results["b2_t4096"]
    eligible=[]
    for name,r in results.items():
        no_drop=all(r["final_pooled_yes"][k] is not None and baseline["final_pooled_yes"][k] is not None
                    and r["final_pooled_yes"][k]>=baseline["final_pooled_yes"][k] for k in ("precision","recall"))
        no_drop=no_drop and r["acceptance_consistency"]>=baseline["acceptance_consistency"]
        r["semantic_non_regression_vs_baseline"]=no_drop
        if r["PASS"] and no_drop:
            eligible.append(name)
    order=("b2_t4096","b2_t8192","b1_t4096","b1_t8192")
    return next((name for name in order if name in eligible),None)
