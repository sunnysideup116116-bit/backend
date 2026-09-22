"""Hermetic directionality metrics; synthetic objects only, no provider/Graph IO."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import r32b_metrics as metrics


def metric_case(case_id="h01-f", expected="YES", relation="equivalent", category="equivalent"):
    return {"id": case_id, "Q": "Synthetic Query", "C": "Synthetic Preference",
            "expected": expected, "relation": relation, "category": category,
            "language": "EN-EN"}


def metric_jobs(cases):
    jobs = []
    for repeat in (1, 2, 3):
        for case in cases:
            row = {"id": case["id"], "decision": case["expected"], "relation": case["relation"]}
            attempt = {"valid": True, "decisions": {case["id"]: row}, "errors": [],
                       "metadata": {"finish_reason": "stop"}, "latency_seconds": 1.0,
                       "usage": {"completion_tokens": 30}}
            jobs.append({"ids": [case["id"]], "repeat": repeat, "attempts": [attempt],
                         "first_pass_valid": True, "eventual_valid": True, "elapsed_seconds": 1.0})
    return jobs


class R32bMetricsTests(unittest.TestCase):
    def test_directional_errors_cover_every_gold_relation_category(self):
        cases = [metric_case(), metric_case("h02-f", "NO", metrics.BROAD_CANDIDATE, "cross_language"),
                 metric_case("h03-f", "YES", metrics.SPECIFIC_CANDIDATE, "composite")]
        jobs = metric_jobs(cases)
        jobs[1]["attempts"][0]["decisions"]["h02-f"].update(decision="YES", relation="equivalent")
        jobs[2]["attempts"][0]["decisions"]["h03-f"]["decision"] = "NO"
        jobs[4]["attempts"][0]["decisions"]["h02-f"]["decision"] = "ABSTAIN"
        before = deepcopy((jobs, cases))
        result = metrics.summarize_condition(jobs, cases)
        self.assertEqual((jobs, cases), before)
        self.assertEqual(result["broad_to_specific_false_yes"], 1)
        self.assertEqual(result["specific_to_broad_false_no"], 1)
        self.assertEqual(result["all_directional_error_outputs"], 3)
        self.assertEqual([r["id"] for r in result["directional_mistakes"]], ["h02-f", "h03-f"])
        self.assertEqual(result["directional_mistakes"][0]["rounds"], [
            {"round": 1, "decision": "YES", "relation": "equivalent"},
            {"round": 2, "decision": "ABSTAIN", "relation": metrics.BROAD_CANDIDATE},
            {"round": 3, "decision": "NO", "relation": metrics.BROAD_CANDIDATE}])
        self.assertFalse(result["proposed_engineering_gates"]["broad_to_specific_zero_false_yes"])

    def test_error_is_not_no_and_counts_against_consistency(self):
        cases = [metric_case(relation=metrics.SPECIFIC_CANDIDATE),
                 metric_case("h02-f", "ABSTAIN", "unknown", "ambiguity")]
        jobs = metric_jobs(cases)
        for job in jobs[-2:]:
            job.update(first_pass_valid=False, eventual_valid=False)
            job["attempts"][0].update(valid=False, decisions={}, errors=["truncated_response"],
                                      metadata={"finish_reason": "length"})
        result = metrics.summarize_condition(jobs, cases)
        self.assertEqual(result["specific_to_broad_false_no"], 0)
        self.assertEqual(result["all_directional_error_outputs"], 1)
        self.assertEqual(result["directional_mistakes"][0]["rounds"][-1]["decision"], "ERROR")
        self.assertEqual(result["ambiguity_outcomes_including_errors"], {"ABSTAIN": 2, "ERROR": 1})
        self.assertEqual(result["final_pooled_yes"]["ERROR"], 2)
        self.assertEqual(result["final_pooled_yes"]["FN"], 1)
        self.assertEqual(result["primary_consistency_all_cases"], 0)
        self.assertFalse(result["all_gates_pass"])

    def test_perfect_condition_passes_without_production_claim(self):
        cases = [metric_case(), metric_case("h02-f", "NO", metrics.BROAD_CANDIDATE),
                 metric_case("h03-f", "YES", metrics.SPECIFIC_CANDIDATE)]
        result = metrics.summarize_condition(metric_jobs(cases), cases)
        self.assertTrue(result["all_gates_pass"])
        self.assertFalse(result["production_ready"])
        self.assertEqual(result["rounds"], 3)
        self.assertEqual(result["directional_mistakes"], [])
        self.assertEqual(result["consistency_denominator"], 3)

    def test_full_96_case_denominator_and_no_rounding_up(self):
        cases = [metric_case(f"h{i:02}-f") for i in range(96)]
        jobs = metric_jobs(cases)
        jobs[-1]["attempts"][0]["decisions"]["h95-f"]["decision"] = "ABSTAIN"
        result = metrics.summarize_condition(jobs, cases)
        self.assertEqual(result["consistency_denominator"], 96)
        self.assertEqual(result["primary_consistency_all_cases"], 95 / 96)
        self.assertFalse(result["proposed_engineering_gates"]["primary_consistency_ge_99"])

    def test_duplicate_or_missing_schedule_is_rejected(self):
        cases = [metric_case(), metric_case("h02-f")]
        jobs = metric_jobs(cases)
        for bad_jobs in (jobs + [deepcopy(jobs[0])], jobs[1:]):
            with self.subTest(job_count=len(bad_jobs)):
                with self.assertRaises(ValueError):
                    metrics.summarize_condition(bad_jobs, cases)
        bad_jobs = deepcopy(jobs)
        bad_jobs[0]["attempts"][0]["decisions"].clear()
        with self.assertRaisesRegex(ValueError, "validated_decisions"):
            metrics.summarize_condition(bad_jobs, cases)
        bad_jobs = deepcopy(jobs)
        bad_jobs[0].update(first_pass_valid=False, eventual_valid=False)
        bad_jobs[0]["attempts"][0]["valid"] = False
        with self.assertRaisesRegex(ValueError, "salvage"):
            metrics.summarize_condition(bad_jobs, cases)

    def test_repeat_and_case_boundaries_are_enforced(self):
        cases = [metric_case()]
        with self.assertRaisesRegex(ValueError, "three_complete"):
            metrics.summarize_condition(metric_jobs(cases)[:2], cases)
        with self.assertRaisesRegex(ValueError, "case_set"):
            metrics.summarize_condition([], [])
        with self.assertRaisesRegex(ValueError, "case_set"):
            metrics.summarize_condition(metric_jobs(cases), cases * 2)
        too_many = [metric_case(f"h{i}") for i in range(97)]
        with self.assertRaisesRegex(ValueError, "case_set"):
            metrics.summarize_condition(metric_jobs(too_many), too_many)

    def test_first_pass_retry_and_finish_reasons_are_separate(self):
        cases = [metric_case()]
        jobs = metric_jobs(cases)
        failed = deepcopy(jobs[0]["attempts"][0])
        failed.update(valid=False, decisions={}, errors=["empty_response", "truncated_response"],
                      metadata={"finish_reason": "length"})
        jobs[0]["attempts"].insert(0, failed)
        jobs[0]["first_pass_valid"] = False
        result = metrics.summarize_condition(jobs, cases)
        self.assertEqual(result["first_finish_reasons"], {"length": 1, "stop": 2})
        self.assertEqual(result["eventual_finish_reasons"], {"stop": 3})
        self.assertEqual(result["first_pass_rate"], 2 / 3)
        self.assertEqual(result["eventual_rate"], 1)
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(result["final_pooled_yes"]["TP"], 3)
        self.assertFalse(result["all_gates_pass"])
        jobs[0]["attempts"][0] = deepcopy(jobs[0]["attempts"][1])
        jobs[0]["first_pass_valid"] = True
        with self.assertRaisesRegex(ValueError, "must_not_retry"):
            metrics.summarize_condition(jobs, cases)

    def test_no_predicted_yes_cannot_pass(self):
        cases = [metric_case(expected="NO", relation=metrics.BROAD_CANDIDATE)]
        result = metrics.summarize_condition(metric_jobs(cases), cases)
        self.assertIsNone(result["final_pooled_yes"]["precision"])
        self.assertFalse(result["all_gates_pass"])


if __name__ == "__main__":
    unittest.main()
