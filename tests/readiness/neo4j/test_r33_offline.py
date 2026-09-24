"""Hermetic R3.3 pre-scoring regression; no real provider or Graph."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import r33_data as data
import r33_metrics as metrics
import run_r33_offline as runner
import run_r32c_offline as frozen
from readiness_frozen_git import FrozenTestGit


class R33Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        git_patch = patch.object(data, "subprocess", FrozenTestGit(data.ROOT))
        git_patch.start()
        cls.addClassCleanup(git_patch.stop)
        cls.cases,cls.audit=data.r33_inputs()
        cls.plan=data.r33_schedule(cls.cases)
        cls.by_id={c["id"]:c for c in cls.cases}

    def jobs(self):
        result=[]
        for j in self.plan:
            decisions={c["id"]:{"id":c["id"],"relation":c["relation"],"decision":c["expected"]} for c in j["cases"]}
            attempt={"valid":True,"errors":[],"decisions":decisions,"metadata":{"finish_reason":"stop"},
                     "latency_seconds":1,"usage":{"completion_tokens":100}}
            result.append({**{k:v for k,v in j.items() if k!="cases"},"ids":list(decisions),
                "attempts":[attempt],"first_pass_valid":True,"eventual_valid":True,"elapsed_seconds":1})
        return result

    def change(self,jobs,cid,repeat,relation):
        job=next(j for j in jobs if j["repeat"]==repeat and cid in j["ids"])
        row=job["attempts"][-1]["decisions"][cid]
        row.update(relation=relation,decision=frozen.RELATION_TO_PRIMARY[relation])

    def fail(self,job):
        job.update(first_pass_valid=False,eventual_valid=False)
        job["attempts"]=[{"valid":False,"errors":["truncated_response"],"decisions":{},
                         "metadata":{"finish_reason":"length"},"latency_seconds":1,"usage":None}]

    def test_gate_exactly_user_approved_and_prospective(self):
        gate=data.r33_gate()
        self.assertEqual(len(gate["blocking_gates"]),9)
        self.assertEqual(gate["blocking_gates"]["eventual_validity"],{"numerator":99,"denominator":100})
        self.assertEqual(gate["blocking_gates"]["acceptance_recall"],{"numerator":95,"denominator":100})
        self.assertEqual(gate["configuration"]["reasoning_effort"],"omitted/default")
        self.assertEqual(gate["configuration"]["max_tokens"],4096)

    def test_new_dataset_coverage_novelty_and_disclosure(self):
        self.assertEqual(len(self.cases),192)
        self.assertEqual(self.audit["novelty_canonical_pair_overlap"],0)
        self.assertEqual(set(self.audit["languages"].values()),{48})
        self.assertEqual(len(self.audit["categories"]),12)
        self.assertEqual(set(self.audit["categories"].values()),{16})
        self.assertEqual(self.audit["primary_labels"],{"YES":48,"NO":128,"ABSTAIN":16})
        self.assertIn("not independently human reviewed",data.r33_gate()["label_provenance"].lower())

    def test_full_input_prefix_collisions_and_persisted_identity(self):
        self.assertEqual(len(self.audit["identical_prefix40_groups"]),16)
        self.assertGreater(self.audit["semantic_max_codepoints"],40)
        for c in self.cases:
            if c["prefix40"]:
                self.assertEqual(c["Q"][:40],c["C"][:40])
                self.assertNotEqual(c["query_key"],c["candidate_key"])
                self.assertNotEqual(c["Q"],c["C"])
            self.assertLessEqual(max(len(c["Q"]),len(c["C"])),500)

    def test_schedule_three_rounds_unique_bounded_and_repeatable(self):
        self.assertEqual(self.plan,data.r33_schedule(self.cases))
        self.assertEqual(len(self.plan),288)
        self.assertEqual(len({j["job_id"] for j in self.plan}),288)
        for repeat in (1,2,3):
            self.assertEqual(sorted(c["id"] for j in self.plan if j["repeat"]==repeat for c in j["cases"]),sorted(self.by_id))

    def test_model_wire_has_no_labels_and_preserves_full_semantics(self):
        prompt=(data.HERE/"r32c_prompt_v3.txt").read_text()
        for job in self.plan:
            request=frozen.relation_request(job,prompt)
            self.assertEqual(set(request),{"model","messages","temperature","max_tokens"})
            self.assertEqual(request["temperature"],0)
            self.assertEqual(request["max_tokens"],4096)
            actual=json.loads(request["messages"][1]["content"])["pairs"]
            self.assertEqual(actual,[{k:c[k] for k in ("id","Q","C")} for c in job["cases"]])

    def test_perfect_fixture_gold_passes_without_input_mutation(self):
        jobs=self.jobs()
        before=deepcopy(jobs)
        r=metrics.r33_summarize(jobs,self.cases)
        self.assertTrue(r["PASS"])
        self.assertEqual(r["acceptance_confusion"],{"TP":144,"FP":0,"FN":0,"TN":432})
        self.assertEqual(r["acceptance_consistency"],1)
        self.assertEqual(jobs,before)

    def test_NO_to_ABSTAIN_is_diagnostic_not_gate(self):
        jobs=self.jobs()
        cid=next(c["id"] for c in self.cases if c["relation"]=="unrelated")
        self.change(jobs,cid,1,"lexical_ambiguity")
        r=metrics.r33_summarize(jobs,self.cases)
        self.assertTrue(r["PASS"])
        self.assertIn(cid,r["NO_ABSTAIN_disagreements"])
        self.assertEqual(r["acceptance_consistency"],1)
        self.assertLess(r["relation_consistency"],1)

    def test_each_safety_class_blocks_single_false_accept(self):
        for relation,metric in (("candidate_more_broad","broad_to_specific_false_accept"),
                                ("role_mismatch","role_false_accept"),("constraint_conflict","constraint_false_accept")):
            with self.subTest(relation=relation):
                jobs=self.jobs()
                cid=next(c["id"] for c in self.cases if c["relation"]==relation)
                self.change(jobs,cid,1,"equivalent")
                r=metrics.r33_summarize(jobs,self.cases)
                self.assertFalse(r["PASS"])
                self.assertEqual(r["safety"][metric],1)
                self.assertEqual(r["false_accepts"][0]["id"],cid)

    def test_precision_and_consistency_use_exact_denominators(self):
        ids=[c["id"] for c in self.cases if c["relation"]=="unrelated"][:2]
        jobs=self.jobs()
        self.change(jobs,ids[0],1,"equivalent")
        one=metrics.r33_summarize(jobs,self.cases)
        self.assertTrue(one["blocking_gates"]["acceptance_precision"])
        self.assertTrue(one["blocking_gates"]["acceptance_consistency"])
        self.change(jobs,ids[1],1,"equivalent")
        two=metrics.r33_summarize(jobs,self.cases)
        self.assertFalse(two["blocking_gates"]["acceptance_precision"])
        self.assertFalse(two["blocking_gates"]["acceptance_consistency"])

    def test_recall_95_not_99(self):
        jobs=self.jobs()
        selected=[(c["id"],r) for c in self.cases if c["expected"]=="YES" for r in (1,2,3)][:8]
        for cid,r in selected[:7]:
            self.change(jobs,cid,r,"unknown")
        self.assertTrue(metrics.r33_summarize(jobs,self.cases)["blocking_gates"]["acceptance_recall"])
        self.change(jobs,*selected[7],"unknown")
        self.assertFalse(metrics.r33_summarize(jobs,self.cases)["blocking_gates"]["acceptance_recall"])

    def test_error_reject_is_separate_and_validity_99_boundary(self):
        jobs=self.jobs()
        eligible=[j for j in jobs if all(self.by_id[cid]["expected"]!="YES" for cid in j["ids"])]
        for j in eligible[:2]:
            self.fail(j)
        r=metrics.r33_summarize(jobs,self.cases)
        self.assertTrue(r["PASS"])
        self.assertEqual(r["ERROR_observations"],4)
        self.assertEqual(r["error_fail_open"],0)
        self.fail(eligible[2])
        r=metrics.r33_summarize(jobs,self.cases)
        self.assertFalse(r["blocking_gates"]["first_pass_validity"])
        self.assertFalse(r["blocking_gates"]["eventual_validity"])

    def test_error_fail_open_blocks_even_if_binary_mapping_rejects(self):
        jobs=self.jobs()
        self.fail(jobs[0])
        cid=jobs[0]["ids"][0]
        jobs[0]["attempts"][0]["decisions"]={cid:{"id":cid,"relation":"equivalent","decision":"YES"}}
        r=metrics.r33_summarize(jobs,self.cases)
        self.assertFalse(r["PASS"])
        self.assertEqual(r["error_fail_open"],1)

    def test_valid_NO_ABSTAIN_never_retry_and_ERROR_only_once(self):
        job=self.plan[0]
        for relation in ("unrelated","unknown"):
            rows=[{"id":c["id"],"relation":relation} for c in job["cases"]]
            response=SimpleNamespace(usage=None,model=frozen.MODEL,choices=[
                SimpleNamespace(finish_reason="stop",message=SimpleNamespace(content=json.dumps({"results":rows})))])
            calls=[]
            def create(**kwargs):
                calls.append(kwargs)
                return response
            client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            result=frozen.evaluate_relation_batch(job,client,"frozen prompt","synthetic-test-secret")
            self.assertEqual(len(calls),1)
            self.assertTrue(result["eventual_valid"])

    def test_missing_schedule_duplicate_case_and_extra_retry_reject(self):
        jobs=self.jobs()
        for invalid in (jobs[:-1],jobs+[jobs[0]]):
            with self.assertRaises(ValueError):
                metrics.r33_summarize(invalid,self.cases)
        jobs[0]["attempts"]*=2
        with self.assertRaisesRegex(ValueError,"valid_result_was_retried"):
            metrics.r33_summarize(jobs,self.cases)

    def test_model_primary_cannot_override_relation(self):
        jobs=self.jobs()
        jobs[0]["attempts"][0]["decisions"][jobs[0]["ids"][0]]["decision"]="INVALID"
        with self.assertRaisesRegex(ValueError,"primary_override"):
            metrics.r33_summarize(jobs,self.cases)

    def test_snapshot_binds_files_case_order_and_full_inputs(self):
        snap,cases,jobs,_=data.r33_snapshot()
        self.assertEqual(len(cases),192)
        self.assertEqual(len(jobs),288)
        self.assertEqual(snap,data.r33_snapshot()[0])
        self.assertFalse(snap["production_ready"])
        self.assertTrue(snap["no_production_graph"])
        self.assertEqual(set(snap["runtime_flags"].values()),{"off"})


if __name__=="__main__":
    unittest.main()
