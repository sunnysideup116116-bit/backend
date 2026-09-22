"""Pure, fail-closed R3.2c development metrics; no provider, secret or Graph IO.

Primary decisions are always re-derived from the relation enum. A stored primary
may be checked for provenance, but never overrides the deterministic mapping.
The frozen R3.2 summary supplies common latency/schema/acceptance calculations.
"""
from collections import Counter
from copy import deepcopy

from r32c_relation import map_relation
from run_r32_offline import condition_summary


BROAD_CANDIDATE = "candidate_more_broad"
SPECIFIC_CANDIDATE = "candidate_more_specific"
ROUNDS = (1, 2, 3)
CASE_COUNT = 96


def summarize_relation_condition(jobs, cases):
    """Require three complete batch-2 rounds; retain failed batches as ERROR.

    Inputs must be the bounded synthetic development fixtures, never user data.
    Missing schedules, contradictory derived primaries, retries of valid output
    and salvaged rows from an invalid batch make the evidence invalid outright.
    This function never mutates its inputs or rounds values before checking gates.
    """
    if (len(cases) != CASE_COUNT
            or len({case["id"] for case in cases}) != CASE_COUNT):
        raise ValueError("exactly_96_unique_synthetic_cases_required")
    by_id = {case["id"]: case for case in cases}
    for case in cases:
        if (not isinstance(case["id"], str) or not case["id"]
                or not case.get("source_relation")
                or map_relation(case["relation"]) != case["expected"]):
            raise ValueError("invalid_or_inconsistent_gold_relation_projection")
    if {job["repeat"] for job in jobs} != set(ROUNDS):
        raise ValueError("three_complete_rounds_required")

    normalized = deepcopy(jobs)
    scheduled = {repeat: set() for repeat in ROUNDS}
    first = {repeat: {} for repeat in ROUNDS}
    final = {repeat: {} for repeat in ROUNDS}
    first_finish, final_finish = Counter(), Counter()
    for job in normalized:
        repeat, ids, attempts = job["repeat"], job["ids"], job["attempts"]
        if (len(ids) != 2 or len(set(ids)) != 2 or job.get("batch_size", 2) != 2
                or not set(ids) <= set(by_id) or scheduled[repeat].intersection(ids)):
            raise ValueError("invalid_or_duplicate_batch2_schedule")
        scheduled[repeat].update(ids)
        if not 1 <= len(attempts) <= 2:
            raise ValueError("invalid_attempt_count")
        if (type(job["first_pass_valid"]) is not bool
                or type(job["eventual_valid"]) is not bool
                or job["first_pass_valid"] != attempts[0]["valid"]
                or job["eventual_valid"] != attempts[-1]["valid"]):
            raise ValueError("attempt_validity_mismatch")
        if len(attempts) == 2 and attempts[0]["valid"]:
            raise ValueError("valid_relation_must_not_retry")
        for attempt in attempts:
            if type(attempt["valid"]) is not bool:
                raise ValueError("invalid_validity_type")
            decisions = attempt["decisions"]
            if attempt["valid"]:
                if attempt["errors"] or set(decisions) != set(ids):
                    raise ValueError("validated_relations_must_match_scheduled_ids")
                for case_id, row in decisions.items():
                    if row["id"] != case_id:
                        raise ValueError("validated_relation_id_mismatch")
                    mapped = map_relation(row["relation"])
                    if "decision" in row and row["decision"] != mapped:
                        raise ValueError("stored_primary_must_equal_deterministic_mapping")
                    row["decision"] = mapped
            elif decisions:
                raise ValueError("failed_batch_must_not_salvage_relations")
        for case_id in ids:
            error = {"id": case_id, "decision": "ERROR", "relation": "ERROR"}
            first[repeat][case_id] = attempts[0]["decisions"].get(case_id, error)
            final[repeat][case_id] = attempts[-1]["decisions"].get(case_id, error)
        first_finish[attempts[0]["metadata"].get("finish_reason", "unknown")] += 1
        final_finish[attempts[-1]["metadata"].get("finish_reason", "unknown")] += 1
    if any(ids != set(by_id) for ids in scheduled.values()):
        raise ValueError("missing_scheduled_cases")

    result = condition_summary(normalized, cases, "R3.2c")
    result.update(rounds=3, batch_size=2, consistency_denominator=CASE_COUNT,
                  relation_mistakes=[], primary_mistakes=[], all_mistakes=[],
                  directional_mistakes=[], directional_relation_mistakes=[],
                  ambiguity_cases=[], directional_outcomes={}, category_results={},
                  language_results={}, production_ready=False)
    result["first_finish_reasons"] = dict(sorted(first_finish.items()))
    result["eventual_finish_reasons"] = dict(sorted(final_finish.items()))
    result["relation_consistency_all_cases"] = result["secondary_consistent_cases"] / CASE_COUNT
    result["relation_consistent_cases"] = result["secondary_consistent_cases"]
    result["relation_unstable_or_error_ids"] = sorted(case_id for case_id in by_id
        if any(final[r][case_id]["relation"] == "ERROR" for r in ROUNDS)
        or len({final[r][case_id]["relation"] for r in ROUNDS}) != 1)
    for phase, outputs in (("first", first), ("final", final)):
        valid = [(case, outputs[r][case["id"]]) for case in cases for r in ROUNDS
                 if outputs[r][case["id"]]["relation"] != "ERROR"]
        correct = sum(row["relation"] == case["relation"] for case, row in valid)
        result[f"relation_accuracy_valid_{phase}"] = correct / len(valid) if valid else None
        result[f"relation_correct_{phase}"] = correct
        result[f"relation_accuracy_all_{phase}"] = correct / (CASE_COUNT * len(ROUNDS))
    confusion = Counter((case["relation"], final[r][case["id"]]["relation"])
                        for case in cases for r in ROUNDS)
    result["relation_confusion"] = [{"gold": gold, "predicted": predicted, "count": count}
                                    for (gold, predicted), count in sorted(confusion.items())]

    for relation in (BROAD_CANDIDATE, SPECIFIC_CANDIDATE):
        selected = [case for case in cases if case["relation"] == relation]
        result["directional_outcomes"][relation] = {
            "cases": len(selected), "expected_outputs": len(selected) * 3,
            "decisions": dict(sorted(Counter(final[r][c["id"]]["decision"]
                for c in selected for r in ROUNDS).items()))}
    for case_id in sorted(by_id):
        case = by_id[case_id]
        rows = [{"round": repeat, "decision": final[repeat][case_id]["decision"],
                 "relation": final[repeat][case_id]["relation"]} for repeat in ROUNDS]
        primary_wrong = any(row["decision"] != case["expected"] for row in rows)
        relation_wrong = any(row["relation"] != case["relation"] for row in rows)
        record = {"id": case_id, "Q": case["Q"], "C": case["C"],
                  "category": case["category"], "language": case["language"],
                  "expected": case["expected"], "source_relation": case["source_relation"],
                  "gold_relation": case["relation"], "rounds": rows}
        if primary_wrong:
            result["primary_mistakes"].append(record)
        if relation_wrong:
            result["relation_mistakes"].append(record)
        if primary_wrong or relation_wrong:
            result["all_mistakes"].append(record)
        if case["relation"] in {BROAD_CANDIDATE, SPECIFIC_CANDIDATE}:
            if primary_wrong:
                result["directional_mistakes"].append(record)
            if relation_wrong:
                result["directional_relation_mistakes"].append(record)
        if case["expected"] == "ABSTAIN":
            result["ambiguity_cases"].append(record)

    result["broad_to_specific_false_yes"] = sum(final[r][c["id"]]["decision"] == "YES"
        for c in cases if c["relation"] == BROAD_CANDIDATE for r in ROUNDS)
    result["specific_to_broad_false_no"] = sum(final[r][c["id"]]["decision"] == "NO"
        for c in cases if c["relation"] == SPECIFIC_CANDIDATE for r in ROUNDS)
    result["all_directional_error_outputs"] = sum(final[r][c["id"]]["decision"] != c["expected"]
        for c in cases if c["relation"] in {BROAD_CANDIDATE, SPECIFIC_CANDIDATE} for r in ROUNDS)
    result["ambiguity_outcomes_including_errors"] = dict(sorted(Counter(
        final[r][c["id"]]["decision"] for c in cases if c["expected"] == "ABSTAIN"
        for r in ROUNDS).items()))
    for field, output_key in (("category", "category_results"), ("language", "language_results")):
        for value in sorted({case[field] for case in cases}):
            selected = [case for case in cases if case[field] == value]
            rows = [(case, final[r][case["id"]]) for case in selected for r in ROUNDS]
            valid = [(case, row) for case, row in rows if row["decision"] != "ERROR"]
            result[output_key][value] = {
                "cases": len(selected), "expected_outputs": len(rows), "valid_outputs": len(valid),
                "errors": len(rows) - len(valid),
                "primary_accuracy_valid": sum(c["expected"] == row["decision"] for c, row in valid) / len(valid) if valid else None,
                "relation_accuracy_valid": sum(c["relation"] == row["relation"] for c, row in valid) / len(valid) if valid else None,
                "false_yes": sum(c["expected"] != "YES" and row["decision"] == "YES" for c, row in valid)}
    precision = result["final_pooled_yes"]["precision"]
    result["proposed_engineering_gates"] = {
        "first_pass_ge_99": result["first_pass_rate"] >= .99,
        "eventual_sample_100": result["eventual_rate"] == 1,
        "mapped_yes_precision_ge_99": precision is not None and precision >= .99,
        "primary_consistency_ge_99": result["primary_consistency_all_cases"] >= .99,
        "relation_consistency_ge_99": result["relation_consistency_all_cases"] >= .99,
        "broad_to_specific_zero_false_yes": result["broad_to_specific_false_yes"] == 0,
        "role_zero_false_yes": result["role_false_yes"] == 0,
        "constraint_zero_false_yes": result["constraint_false_yes"] == 0}
    result["all_gates_pass"] = all(result["proposed_engineering_gates"].values())
    result["scope"] = (
        "96-case development only, not final holdout; primary recomputed from relation; "
        "ERROR never NO; failed batches not salvaged; 3 complete rounds; "
        "all-case consistency includes missing/error; pooled repeats not independent")
    return result
