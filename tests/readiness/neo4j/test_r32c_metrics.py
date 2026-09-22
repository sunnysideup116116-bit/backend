"""Hermetic full-sample R3.2c reporting and deterministic-policy regressions."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import r32c_metrics as metrics
from r32c_relation import RELATION_TO_PRIMARY, load_complete_cases, map_relation


class R32CMetricTests(unittest.TestCase):
    def cases(self):
        relations = tuple(RELATION_TO_PRIMARY)
        return [{"id": f"fixture-{i:03}", "Q": "Synthetic bounded complete query",
                 "C": "Synthetic bounded complete candidate preference",
                 "expected": map_relation(relations[i % len(relations)]),
                 "relation": relations[i % len(relations)],
                 "source_relation": relations[i % len(relations)],
                 "category": "composite" if i % 2 else "cross_language",
                 "language": ("EN-EN", "ZH-ZH", "EN-ZH", "ZH-EN")[i % 4]}
                for i in range(96)]

    def jobs(self, cases):
        jobs = []
        for repeat in (1, 2, 3):
            for offset in range(0, 96, 2):
                batch = cases[offset:offset + 2]
                rows = {case["id"]: {"id": case["id"], "relation": case["relation"]}
                        for case in batch}
                attempt = {"valid": True, "decisions": rows, "errors": [],
                           "metadata": {"finish_reason": "stop"}, "latency_seconds": 1.0,
                           "usage": {"completion_tokens": 30}}
                jobs.append({"ids": list(rows), "repeat": repeat, "batch_size": 2,
                             "attempts": [attempt], "first_pass_valid": True,
                             "eventual_valid": True, "elapsed_seconds": 1.0})
        return jobs

    def change_relation(self, jobs, case_id, repeat, relation):
        job = next(job for job in jobs if job["repeat"] == repeat and case_id in job["ids"])
        job["attempts"][-1]["decisions"][case_id] = {"id": case_id, "relation": relation}

    def fail_job(self, job):
        job.update(first_pass_valid=False, eventual_valid=False)
        job["attempts"][-1].update(valid=False, decisions={}, errors=["truncated_response"],
                                    metadata={"finish_reason": "length"})

    def test_complete_frozen_development_projection_is_reportable(self):
        cases = load_complete_cases()
        result = metrics.summarize_relation_condition(self.jobs(cases), cases)
        self.assertEqual(result["expected_case_outputs"], 288)
        self.assertEqual(result["relation_correct_final"], 288)
        self.assertEqual(result["final_pooled_yes"]["TP"], 28 * 3)
        self.assertEqual(result["final_pooled_yes"]["TN"], 68 * 3)
        self.assertTrue(result["all_gates_pass"])
        self.assertFalse(result["production_ready"])
        self.assertIn("not final holdout", result["scope"])

    def test_perfect_relations_derive_primary_without_input_mutation(self):
        cases = self.cases()
        jobs = self.jobs(cases)
        unchanged = deepcopy((cases, jobs))
        result = metrics.summarize_relation_condition(jobs, cases)
        self.assertEqual((cases, jobs), unchanged)
        self.assertEqual(result["relation_accuracy_valid_first"], 1)
        self.assertEqual(result["relation_accuracy_valid_final"], 1)
        self.assertEqual(result["relation_accuracy_all_final"], 1)
        self.assertEqual(result["primary_consistency_all_cases"], 1)
        self.assertEqual(result["relation_consistency_all_cases"], 1)
        self.assertEqual(result["all_mistakes"], [])
        self.assertEqual(result["first_latency_seconds"]["median"], 1)
        self.assertEqual(result["completion_tokens"]["mean"], 30)
        self.assertEqual(result["total_completion_tokens"], 144 * 30)
        self.assertEqual(result["first_finish_reasons"], {"stop": 144})
        self.assertTrue(result["all_gates_pass"])

    def test_stored_primary_cannot_override_relation(self):
        cases = self.cases()
        jobs = self.jobs(cases)
        row = jobs[0]["attempts"][0]["decisions"][cases[0]["id"]]
        row["decision"] = "NO"
        with self.assertRaisesRegex(ValueError, "stored_primary"):
            metrics.summarize_relation_condition(jobs, cases)
        row["decision"] = "YES"
        self.assertTrue(metrics.summarize_relation_condition(jobs, cases)["all_gates_pass"])

    def test_invalid_relation_cannot_be_treated_as_abstain(self):
        cases = self.cases()
        for invalid in ("YES", "candidate_broader_insufficient", "bad", None, []):
            jobs = self.jobs(cases)
            self.change_relation(jobs, cases[0]["id"], 1, invalid)
            with self.subTest(value=invalid), self.assertRaisesRegex(ValueError, "relation_enum"):
                metrics.summarize_relation_condition(jobs, cases)

    def test_all_directional_errors_include_composite_cross_language(self):
        cases = self.cases()
        jobs = self.jobs(cases)
        specific, broad = cases[1], cases[2]
        self.change_relation(jobs, specific["id"], 1, "candidate_more_broad")
        self.change_relation(jobs, broad["id"], 1, "candidate_more_specific")
        self.change_relation(jobs, broad["id"], 2, "unknown")
        result = metrics.summarize_relation_condition(jobs, cases)
        self.assertEqual(result["broad_to_specific_false_yes"], 1)
        self.assertEqual(result["specific_to_broad_false_no"], 1)
        self.assertEqual(result["all_directional_error_outputs"], 3)
        self.assertEqual([row["id"] for row in result["directional_mistakes"]],
                         [specific["id"], broad["id"]])
        self.assertEqual(result["directional_mistakes"][1]["rounds"], [
            {"round": 1, "decision": "YES", "relation": "candidate_more_specific"},
            {"round": 2, "decision": "ABSTAIN", "relation": "unknown"},
            {"round": 3, "decision": "NO", "relation": "candidate_more_broad"}])
        self.assertEqual(result["directional_mistakes"][0]["source_relation"], specific["source_relation"])
        self.assertFalse(result["proposed_engineering_gates"]["broad_to_specific_zero_false_yes"])

    def test_relation_only_error_is_not_hidden_by_correct_acceptance(self):
        cases = self.cases()
        jobs = self.jobs(cases)
        self.change_relation(jobs, cases[0]["id"], 3, "candidate_more_specific")
        result = metrics.summarize_relation_condition(jobs, cases)
        self.assertEqual(result["primary_consistency_all_cases"], 1)
        self.assertEqual(result["relation_consistency_all_cases"], 95 / 96)
        self.assertEqual(result["relation_accuracy_valid_final"], 287 / 288)
        self.assertEqual(result["primary_mistakes"], [])
        self.assertEqual(len(result["all_mistakes"]), 1)
        self.assertEqual(result["relation_unstable_or_error_ids"], [cases[0]["id"]])
        self.assertFalse(result["proposed_engineering_gates"]["relation_consistency_ge_99"])
        self.assertFalse(result["all_gates_pass"])

    def test_95_of_96_primary_consistency_is_not_rounded_up(self):
        cases = self.cases()
        jobs = self.jobs(cases)
        self.change_relation(jobs, cases[0]["id"], 3, "unknown")
        result = metrics.summarize_relation_condition(jobs, cases)
        self.assertEqual(result["primary_consistency_all_cases"], 95 / 96)
        self.assertFalse(result["proposed_engineering_gates"]["primary_consistency_ge_99"])

    def test_errors_are_not_no_and_reduce_all_output_accuracy(self):
        cases = self.cases()
        jobs = self.jobs(cases)
        self.fail_job(jobs[-48])
        result = metrics.summarize_relation_condition(jobs, cases)
        self.assertEqual(result["final_pooled_yes"]["ERROR"], 2)
        self.assertEqual(result["final_pooled_yes"]["FN"], 2)
        self.assertEqual(result["specific_to_broad_false_no"], 0)
        self.assertEqual(result["all_directional_error_outputs"], 1)
        self.assertEqual(result["relation_accuracy_valid_final"], 1)
        self.assertEqual(result["relation_accuracy_all_final"], 286 / 288)
        self.assertEqual(result["primary_consistency_all_cases"], 94 / 96)
        self.assertEqual(result["directional_mistakes"][0]["rounds"][-1]["decision"], "ERROR")
        self.assertFalse(result["proposed_engineering_gates"]["eventual_sample_100"])

    def test_ambiguity_outcomes_and_relation_confusion_remain_distinct(self):
        cases = self.cases()
        jobs = self.jobs(cases)
        ambiguous = cases[6]
        self.change_relation(jobs, ambiguous["id"], 1, "unrelated")
        self.change_relation(jobs, ambiguous["id"], 2, "unknown")
        failure = next(j for j in jobs if j["repeat"] == 3 and ambiguous["id"] in j["ids"])
        self.fail_job(failure)
        result = metrics.summarize_relation_condition(jobs, cases)
        self.assertEqual(result["ambiguity_outcomes_including_errors"]["NO"], 1)
        self.assertEqual(result["ambiguity_outcomes_including_errors"]["ERROR"], 1)
        record = next(row for row in result["ambiguity_cases"] if row["id"] == ambiguous["id"])
        self.assertEqual([row["decision"] for row in record["rounds"]], ["NO", "ABSTAIN", "ERROR"])
        self.assertIn({"gold": "lexical_ambiguity", "predicted": "unknown", "count": 1},
                      result["relation_confusion"])

    def test_role_and_constraint_have_separate_zero_false_yes_gates(self):
        cases = self.cases()
        jobs = self.jobs(cases)
        self.change_relation(jobs, cases[4]["id"], 1, "equivalent")
        self.change_relation(jobs, cases[5]["id"], 1, "candidate_more_specific")
        result = metrics.summarize_relation_condition(jobs, cases)
        self.assertEqual(result["role_false_yes"], 1)
        self.assertEqual(result["constraint_false_yes"], 1)
        self.assertFalse(result["proposed_engineering_gates"]["role_zero_false_yes"])
        self.assertFalse(result["proposed_engineering_gates"]["constraint_zero_false_yes"])

    def test_failed_first_attempt_and_eventual_success_are_separate(self):
        cases = self.cases()
        jobs = self.jobs(cases)
        original = deepcopy(jobs[0]["attempts"][0])
        self.fail_job(jobs[0])
        jobs[0]["attempts"].append(original)
        jobs[0]["eventual_valid"] = True
        result = metrics.summarize_relation_condition(jobs, cases)
        self.assertEqual(result["first_pass_rate"], 143 / 144)
        self.assertEqual(result["eventual_rate"], 1)
        self.assertEqual(result["first_finish_reasons"], {"length": 1, "stop": 143})
        self.assertEqual(result["eventual_finish_reasons"], {"stop": 144})
        self.assertEqual(result["relation_correct_first"], 286)
        self.assertEqual(result["relation_correct_final"], 288)
        self.assertTrue(result["all_gates_pass"])

    def test_invalid_attempt_and_schedule_evidence_is_rejected(self):
        cases = self.cases()
        jobs = self.jobs(cases)
        variants = [jobs[1:], jobs + [deepcopy(jobs[0])], jobs[:96]]
        for bad_jobs in variants:
            with self.subTest(count=len(bad_jobs)), self.assertRaises(ValueError):
                metrics.summarize_relation_condition(bad_jobs, cases)
        mutations = (
            lambda j: j[0].update(batch_size=4),
            lambda j: j[0].update(ids=j[0]["ids"][:1]),
            lambda j: j[0]["attempts"].append(deepcopy(j[0]["attempts"][0])),
            lambda j: j[0].update(first_pass_valid=False),
            lambda j: j[0]["attempts"][0].update(errors=["schema_failure"]),
            lambda j: j[0]["attempts"][0]["decisions"].pop(cases[0]["id"]),
            lambda j: j[0]["attempts"][0]["decisions"][cases[0]["id"]].update(id="other"),
        )
        for index, mutate in enumerate(mutations):
            bad = deepcopy(jobs)
            mutate(bad)
            with self.subTest(mutation=index), self.assertRaises(ValueError):
                metrics.summarize_relation_condition(bad, cases)

    def test_failed_batch_cannot_salvage_relations(self):
        cases = self.cases()
        jobs = self.jobs(cases)
        row = deepcopy(jobs[0]["attempts"][0]["decisions"])
        self.fail_job(jobs[0])
        jobs[0]["attempts"][0]["decisions"] = row
        with self.assertRaisesRegex(ValueError, "salvage"):
            metrics.summarize_relation_condition(jobs, cases)

    def test_case_projection_count_and_gold_labels_are_checked(self):
        cases = self.cases()
        jobs = self.jobs(cases)
        for bad_cases in (cases[:-1], cases + cases[:1], cases[:-1] + cases[:1]):
            with self.subTest(count=len(bad_cases)), self.assertRaises(ValueError):
                metrics.summarize_relation_condition(jobs, bad_cases)
        bad_cases = deepcopy(cases)
        bad_cases[0]["expected"] = "NO"
        with self.assertRaisesRegex(ValueError, "gold_relation_projection"):
            metrics.summarize_relation_condition(jobs, bad_cases)

    def test_no_predicted_yes_is_not_perfect_precision(self):
        cases = self.cases()
        jobs = self.jobs(cases)
        for job in jobs:
            for row in job["attempts"][0]["decisions"].values():
                row["relation"] = "unrelated"
        result = metrics.summarize_relation_condition(jobs, cases)
        self.assertIsNone(result["final_pooled_yes"]["precision"])
        self.assertFalse(result["all_gates_pass"])


if __name__ == "__main__":
    unittest.main()
