"""Prospectively frozen R3.3 binary acceptance metrics; no model/Graph IO."""
from collections import Counter
import math
import statistics
from r32c_relation import map_relation
from r33_data import r33_gate, r33_schedule


def r33_stats(values):
    if not values:
        return None
    ordered = sorted(values)
    return {"count":len(values),"median":statistics.median(values),
            "p95":ordered[math.ceil(.95*len(ordered))-1],"mean":statistics.mean(values),
            "min":min(values),"max":max(values)}


def r33_summarize(jobs, cases):
    gate = r33_gate()
    cfg = gate["configuration"]
    expected = {j["job_id"]:j for j in r33_schedule(cases)}
    if len(jobs) != len(expected) or {j["job_id"] for j in jobs} != set(expected):
        raise ValueError("incomplete_or_duplicate_schedule")
    by_id = {c["id"]:c for c in cases}
    observations, first_observations, attempts = {}, {}, []
    fail_open = 0
    for job in jobs:
        planned = expected[job["job_id"]]
        if (job["repeat"] != planned["repeat"] or job["ids"] != [c["id"] for c in planned["cases"]]
                or not 1 <= len(job["attempts"]) <= 2):
            raise ValueError("schedule_or_attempt_policy_changed")
        if len(job["attempts"]) == 2 and job["attempts"][0]["valid"]:
            raise ValueError("valid_result_was_retried")
        if job["first_pass_valid"] != job["attempts"][0]["valid"] or job["eventual_valid"] != job["attempts"][-1]["valid"]:
            raise ValueError("validity_mismatch")
        for attempt in job["attempts"]:
            attempts.append(attempt)
            if attempt["valid"]:
                if attempt["errors"] or set(attempt["decisions"]) != set(job["ids"]):
                    raise ValueError("invalid_valid_output")
                for cid, row in attempt["decisions"].items():
                    if row["id"] != cid or row["decision"] != map_relation(row["relation"]):
                        raise ValueError("model_or_saved_primary_override")
            elif attempt["decisions"]:
                # Report attempted salvage as a gate failure, never honor it.
                fail_open += sum(row.get("decision") == "YES" or row.get("relation") in
                    {"equivalent","candidate_more_specific"} for row in attempt["decisions"].values())
                if not fail_open:
                    raise ValueError("invalid_batch_salvage")
        for output, attempt in ((first_observations,job["attempts"][0]),(observations,job["attempts"][-1])):
            for cid in job["ids"]:
                row = attempt["decisions"][cid] if attempt["valid"] else {"decision":"ERROR","relation":"ERROR"}
                output[(cid,job["repeat"])] = {"decision":row["decision"],"relation":row["relation"],
                                             "accept":row["decision"]=="YES"}
    if len(observations) != len(cases)*cfg["rounds"]:
        raise ValueError("missing_case_observation")
    counts = Counter({"TP":0,"FP":0,"FN":0,"TN":0})
    confusion = {g:dict.fromkeys(("YES","NO","ABSTAIN","ERROR"),0) for g in ("YES","NO","ABSTAIN")}
    relation_correct, relation_valid = 0,0
    for (cid,_repeat), row in observations.items():
        positive = by_id[cid]["expected"] == "YES"
        counts[("TP" if row["accept"] else "FN") if positive else ("FP" if row["accept"] else "TN")] += 1
        confusion[by_id[cid]["expected"]][row["decision"]] += 1
        relation_valid += row["relation"]!="ERROR"
        relation_correct += row["relation"]==by_id[cid]["relation"]
    rows = []
    for case in cases:
        predictions = [{"round":r,**observations[(case["id"],r)]} for r in range(1,cfg["rounds"]+1)]
        rows.append({**case,"predictions":predictions,
            "acceptance_consistent":len({p["accept"] for p in predictions})==1,
            "relation_consistent":all(p["relation"]!="ERROR" for p in predictions) and len({p["relation"] for p in predictions})==1,
            "primary_consistent":all(p["decision"]!="ERROR" for p in predictions) and len({p["decision"] for p in predictions})==1})
    first_valid = sum(j["first_pass_valid"] for j in jobs)
    eventual_valid = sum(j["eventual_valid"] for j in jobs)
    consistent = sum(r["acceptance_consistent"] for r in rows)
    precision_den = counts["TP"]+counts["FP"]
    recall_den = counts["TP"]+counts["FN"]
    safety = {name:sum(o["accept"] for (cid,_),o in observations.items() if by_id[cid]["relation"]==relation)
        for name,relation in (("broad_to_specific_false_accept","candidate_more_broad"),
                              ("role_false_accept","role_mismatch"),("constraint_false_accept","constraint_conflict"))}
    fractions = {"acceptance_precision":(counts["TP"],precision_den),
        "acceptance_recall":(counts["TP"],recall_den),"acceptance_consistency":(consistent,len(cases)),
        "first_pass_validity":(first_valid,len(jobs)),"eventual_validity":(eventual_valid,len(jobs))}
    verdict = {name:den>0 and num*gate["blocking_gates"][name]["denominator"] >=
               den*gate["blocking_gates"][name]["numerator"] for name,(num,den) in fractions.items()}
    verdict.update({name:value==0 for name,value in safety.items()})
    verdict["error_fail_open"] = fail_open==0
    diagnostics = {}
    for field in ("category","language"):
        diagnostics[field] = {}
        for value in sorted({c[field] for c in cases}):
            subset = [(by_id[cid],o) for (cid,_),o in observations.items() if by_id[cid][field]==value]
            diagnostics[field][value] = {"observations":len(subset),
                "false_accepts":sum(c["expected"]!="YES" and o["accept"] for c,o in subset),
                "false_rejects":sum(c["expected"]=="YES" and not o["accept"] for c,o in subset),
                "ERROR":sum(o["decision"]=="ERROR" for _,o in subset),
                "relation_correct":sum(c["relation"]==o["relation"] for c,o in subset)}
    return {"case_count":len(cases),"rounds":cfg["rounds"],"observations":len(observations),
        "first_requests":len(jobs),"attempts":len(attempts),"retries":len(attempts)-len(jobs),
        "first_valid":first_valid,"eventual_valid":eventual_valid,
        "first_pass_validity":first_valid/len(jobs),"eventual_validity":eventual_valid/len(jobs),
        "acceptance_confusion":dict(counts),"primary_confusion":confusion,
        "acceptance_precision":counts["TP"]/precision_den if precision_den else None,
        "acceptance_recall":counts["TP"]/recall_den if recall_den else None,
        "acceptance_consistent_cases":consistent,"acceptance_consistency":consistent/len(cases),
        "complete_valid_cases":sum(all(p["decision"]!="ERROR" for p in r["predictions"]) for r in rows),
        "relation_consistent_cases":sum(r["relation_consistent"] for r in rows),
        "relation_consistency":sum(r["relation_consistent"] for r in rows)/len(cases),
        "primary_consistent_cases":sum(r["primary_consistent"] for r in rows),
        "relation_accuracy_valid":relation_correct/relation_valid if relation_valid else None,
        "relation_accuracy_all":relation_correct/len(observations),
        "false_accepts":[r for r in rows if r["expected"]!="YES" and any(p["accept"] for p in r["predictions"])],
        "false_rejects":[r for r in rows if r["expected"]=="YES" and any(not p["accept"] for p in r["predictions"])],
        "relation_mistakes":[r for r in rows if any(p["relation"]!=r["relation"] for p in r["predictions"])],
        "NO_ABSTAIN_disagreements":[r["id"] for r in rows if {"NO","ABSTAIN"}<=set(p["decision"] for p in r["predictions"])],
        "ERROR_observations":sum(o["decision"]=="ERROR" for o in observations.values()),
        "error_fail_open":fail_open,"safety":safety,
        "ambiguity":dict(Counter(o["decision"] for (cid,_),o in observations.items() if by_id[cid]["expected"]=="ABSTAIN")),
        "first_latency_seconds":r33_stats([j["attempts"][0]["latency_seconds"] for j in jobs]),
        "eventual_latency_seconds":r33_stats([j["elapsed_seconds"] for j in jobs]),
        "completion_tokens":r33_stats([a["usage"]["completion_tokens"] for a in attempts if a.get("usage")]),
        "total_completion_tokens":sum(a["usage"]["completion_tokens"] for a in attempts if a.get("usage")),
        "finish_reasons":dict(Counter(a["metadata"].get("finish_reason","unknown") for a in attempts)),
        "error_taxonomy":dict(Counter(e for a in attempts for e in a["errors"])),
        "blocking_gates":verdict,"PASS":all(verdict.values()),"diagnostics":diagnostics,"cases":rows,
        "production_ready":False,"label_provenance":gate["label_provenance"]}
