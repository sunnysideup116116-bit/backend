"""Pure offline R3.2b metrics; no provider, Graph, secret or filesystem IO.

The frozen R3.2 calculation is reused rather than rewriting historical metrics.
Directionality is defined by the frozen gold relation, including cases categorized
as cross-language/composite rather than only the broad_narrow category.
"""
from collections import Counter

from run_r32_offline import condition_summary


BROAD_CANDIDATE = "candidate_broader_insufficient"
SPECIFIC_CANDIDATE = "candidate_specific_satisfies_broader_query"
PRIMARIES = {"YES", "NO", "ABSTAIN"}


def summarize_condition(jobs, cases):
    """Summarize exactly three complete schedules without salvaging failed rows.

    Every case must have a scheduled job in each round. An invalid whole batch
    still has its scheduled IDs, but contributes ERROR (not NO) for every ID.
    Missing jobs/duplicate rows are invalid evidence, not perfect empty samples.
    Output contains synthetic Q/C text only; callers must supply frozen fixtures.
    """
    if not cases or len(cases) > 96 or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("invalid_synthetic_case_set")
    by_id = {case["id"]: case for case in cases}
    if {job["repeat"] for job in jobs} != {1, 2, 3}:
        raise ValueError("three_complete_rounds_required")
    scheduled = {repeat: set() for repeat in (1, 2, 3)}
    final = {repeat: {} for repeat in (1, 2, 3)}
    first_finish = Counter()
    final_finish = Counter()
    for job in jobs:
        repeat = job["repeat"]
        ids = job["ids"]
        if (not ids or len(ids) > 2 or len(set(ids)) != len(ids)
                or not set(ids) <= set(by_id) or scheduled[repeat].intersection(ids)):
            raise ValueError("invalid_or_duplicate_scheduled_cases")
        scheduled[repeat].update(ids)
        attempts = job["attempts"]
        if not 1 <= len(attempts) <= 2:
            raise ValueError("invalid_attempt_count")
        if (job["first_pass_valid"] != attempts[0]["valid"]
                or job["eventual_valid"] != attempts[-1]["valid"]):
            raise ValueError("attempt_validity_mismatch")
        if len(attempts) == 2 and attempts[0]["valid"]:
            raise ValueError("valid_primary_must_not_retry")
        for attempt in attempts:
            decisions = attempt["decisions"]
            if attempt["valid"]:
                if set(decisions) != set(ids):
                    raise ValueError("validated_decisions_must_match_scheduled_ids")
                for case_id, row in decisions.items():
                    if row["id"] != case_id or row["decision"] not in PRIMARIES:
                        raise ValueError("invalid_validated_primary")
            elif decisions:
                raise ValueError("failed_batch_must_not_salvage_decisions")
        for case_id in ids:
            final[repeat][case_id] = attempts[-1]["decisions"].get(case_id, {
                "id": case_id, "decision": "ERROR", "relation": "ERROR"})
        first_finish[attempts[0]["metadata"].get("finish_reason", "unknown")] += 1
        final_finish[attempts[-1]["metadata"].get("finish_reason", "unknown")] += 1
    if any(ids != set(by_id) for ids in scheduled.values()):
        raise ValueError("missing_scheduled_cases")

    result = condition_summary(jobs, cases, "R3.2b")
    result["first_finish_reasons"] = dict(sorted(first_finish.items()))
    result["eventual_finish_reasons"] = dict(sorted(final_finish.items()))
    result["rounds"] = 3
    result["consistency_denominator"] = len(cases)
    result["directional_mistakes"] = []
    result["directional_secondary_mistakes"] = []
    result["ambiguity_cases"] = []
    result["directional_outcomes"] = {}
    for relation in (BROAD_CANDIDATE, SPECIFIC_CANDIDATE):
        selected = [c for c in cases if c["relation"] == relation]
        result["directional_outcomes"][relation] = {
            "cases": len(selected), "expected_outputs": len(selected) * 3,
            "decisions": dict(sorted(Counter(final[r][c["id"]]["decision"]
                for c in selected for r in (1, 2, 3)).items()))}
    for case_id in sorted(by_id):
        case = by_id[case_id]
        rows = [{"round": repeat, "decision": final[repeat][case_id]["decision"],
                 "relation": final[repeat][case_id]["relation"]}
                for repeat in (1, 2, 3)]
        record = {"id": case_id, "Q": case["Q"], "C": case["C"],
                  "category": case["category"], "language": case["language"],
                  "expected": case["expected"], "gold_relation": case["relation"],
                  "rounds": rows}
        if case["relation"] in {BROAD_CANDIDATE, SPECIFIC_CANDIDATE}:
            if any(row["decision"] != case["expected"] for row in rows):
                result["directional_mistakes"].append(record)
            if any(row["decision"] != "ERROR" and row["relation"] != case["relation"]
                   for row in rows):
                result["directional_secondary_mistakes"].append(record)
        if case["expected"] == "ABSTAIN":
            result["ambiguity_cases"].append(record)
    result["broad_to_specific_false_yes"] = sum(
        final[r][c["id"]]["decision"] == "YES"
        for c in cases if c["relation"] == BROAD_CANDIDATE for r in (1, 2, 3))
    result["specific_to_broad_false_no"] = sum(
        final[r][c["id"]]["decision"] == "NO"
        for c in cases if c["relation"] == SPECIFIC_CANDIDATE for r in (1, 2, 3))
    result["all_directional_error_outputs"] = sum(
        final[r][c["id"]]["decision"] != c["expected"]
        for c in cases if c["relation"] in {BROAD_CANDIDATE, SPECIFIC_CANDIDATE}
        for r in (1, 2, 3))
    result["ambiguity_outcomes_including_errors"] = dict(sorted(Counter(
        final[r][c["id"]]["decision"] for c in cases if c["expected"] == "ABSTAIN"
        for r in (1, 2, 3)).items()))
    result["proposed_engineering_gates"]["broad_to_specific_zero_false_yes"] = (
        result["broad_to_specific_false_yes"] == 0)
    result["all_gates_pass"] = all(result["proposed_engineering_gates"].values())
    result["production_ready"] = False
    result["directional_scope"] = (
        "gold relations across every category; ERROR/ABSTAIN remain distinct; "
        "all three rounds with missing/error fail consistency; development only")
    return result
