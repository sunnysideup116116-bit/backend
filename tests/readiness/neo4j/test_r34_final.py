"""Hermetic tests for the selected-config fresh final experiment."""
import json,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from r34_final_contract import r34_final_plan,r34_final_metrics
import run_r32c_offline as frozen


class R34FinalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config,cls.cases,cls.plan,cls.audit=r34_final_plan()

    def jobs(self):
        out=[]
        for j in self.plan:
            decisions={c["id"]:{"id":c["id"],"decision":c["expected"],"relation":c["relation"]} for c in j["cases"]}
            a={"valid":True,"decisions":decisions,"errors":[],"metadata":{"finish_reason":"stop"},
               "latency_seconds":1,"usage":{"completion_tokens":100}}
            out.append({**{k:v for k,v in j.items() if k!="cases"},"ids":list(decisions),
                        "attempts":[a],"first_pass_valid":True,"eventual_valid":True,"elapsed_seconds":1})
        return out

    def test_candidate_fixed_before_new_labels(self):
        self.assertEqual(self.config["selected_arm"],"b2_t4096")
        self.assertFalse(self.config["improvement_proven"])
        self.assertEqual(self.config["configuration"]["max_tokens"],4096)
        self.assertEqual(len(self.cases),160)
        self.assertEqual(len(self.plan),240)
        self.assertEqual(self.audit["old_pair_overlap"],0)
        self.assertEqual(set(self.audit["languages"].values()),{40})
        self.assertEqual(self.audit["same_prefix40_pairs"],8)

    def test_complete_wire_and_schedule(self):
        for j in self.plan:
            request=frozen.relation_request(j,"frozen")
            self.assertEqual(set(request),{"model","temperature","max_tokens","messages"})
            self.assertEqual(json.loads(request["messages"][1]["content"])["pairs"],[{k:c[k] for k in ("id","Q","C")}for c in j["cases"]])
        for r in (1,2,3):
            self.assertEqual(len({c["id"] for j in self.plan if j["repeat"]==r for c in j["cases"]}),160)
        self.assertTrue(any(len(c["Q"])>40 for c in self.cases))

    def test_gold_perfect_result(self):
        r=r34_final_metrics(self.jobs(),self.cases)
        self.assertTrue(r["PASS"])
        self.assertEqual(r["final_pooled_yes"]["TP"],96)
        self.assertEqual(r["acceptance_consistency"],1)
        self.assertEqual(r["false_accepts"],[])

    def test_one_false_accept_not_hidden_by_rounding(self):
        jobs=self.jobs()
        target=next(c["id"] for c in self.cases if c["expected"]=="NO")
        j=next(j for j in jobs if target in j["ids"])
        j["attempts"][0]["decisions"][target].update(decision="YES",relation="equivalent")
        r=r34_final_metrics(jobs,self.cases)
        self.assertFalse(r["blocking_gates"]["precision"])
        self.assertEqual(len(r["false_accepts"]),1)

    def test_invalid_output_fail_closed_and_retry_guard(self):
        jobs=self.jobs()
        jobs[0]["attempts"][0]["valid"]=False
        with self.assertRaisesRegex(ValueError,"fail_open"):
            r34_final_metrics(jobs,self.cases)
        jobs=self.jobs();jobs[0]["attempts"]*=2
        with self.assertRaisesRegex(ValueError,"valid_retried"):
            r34_final_metrics(jobs,self.cases)

    def test_schema_exact_99_boundary(self):
        jobs=self.jobs()
        for j in jobs[:2]:
            j["attempts"][0].update(valid=False,decisions={},errors=["truncated_response"])
            j.update(first_pass_valid=False,eventual_valid=False)
        r=r34_final_metrics(jobs,self.cases)
        self.assertTrue(r["blocking_gates"]["first_valid"])
        self.assertTrue(r["blocking_gates"]["eventual_valid"])
        jobs[2]["attempts"][0].update(valid=False,decisions={},errors=["truncated_response"])
        jobs[2].update(first_pass_valid=False,eventual_valid=False)
        r=r34_final_metrics(jobs,self.cases)
        self.assertFalse(r["blocking_gates"]["first_valid"])
        self.assertFalse(r["blocking_gates"]["eventual_valid"])


if __name__=="__main__":
    unittest.main()
