"""R3.4 hermetic batch/budget-only and binary gate tests."""
import ast,json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parent))
import run_r34_offline as runner
from r34_metrics import r34_metrics,r34_select
from r32c_relation import load_complete_cases
from run_r32c_offline import RELATION_TO_PRIMARY


class R34Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases=load_complete_cases()
        cls.plan=runner.r34_schedule(cls.cases)

    def jobs(self,arm):
        result=[]
        for j in self.plan:
            if j["arm"]!=arm:continue
            decisions={c["id"]:{"id":c["id"],"decision":c["expected"],"relation":c["relation"]} for c in j["cases"]}
            a={"valid":True,"errors":[],"decisions":decisions,"metadata":{"finish_reason":"stop"},
               "latency_seconds":1,"usage":{"completion_tokens":100}}
            result.append({**{k:v for k,v in j.items() if k!="cases"},"ids":list(decisions),
                           "attempts":[a],"first_pass_valid":True,"eventual_valid":True,"elapsed_seconds":1})
        return result

    def test_only_frozen_development_inputs_and_fixed_schedule(self):
        self.assertEqual(len(self.plan),864)
        self.assertEqual(self.plan,runner.r34_schedule(self.cases))
        self.assertEqual(len({j["job_id"] for j in self.plan}),864)
        for arm,size,budget in runner.ARMS:
            for r in (1,2,3):
                jobs=[j for j in self.plan if j["arm"]==arm and j["repeat"]==r]
                self.assertEqual(len(jobs),96//size)
                self.assertEqual(sorted(c["id"] for j in jobs for c in j["cases"]),sorted(c["id"] for c in self.cases))
                self.assertTrue(all(j["max_tokens"]==budget for j in jobs))

    def test_all_perfect_arms_meet_gate(self):
        for arm,_,_ in runner.ARMS:
            r=r34_metrics(self.jobs(arm),self.cases)
            self.assertTrue(r["PASS"])
            self.assertEqual(r["acceptance_consistency"],1)
            self.assertEqual(r["final_pooled_yes"]["TP"],84)
            self.assertEqual(r["case_observations"],288)

    def test_budget_adapter_changes_only_max_tokens(self):
        seen=[]
        real=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:seen.append(kw))))
        original={"model":"synthetic","messages":[{"role":"user","content":"full qualifier"}],"temperature":0,"max_tokens":4096}
        runner.r34_budget_client(real,8192).chat.completions.create(**original)
        self.assertEqual(seen,[{**original,"max_tokens":8192}])
        self.assertEqual(original["max_tokens"],4096)
        with self.assertRaises(ValueError):
            runner.r34_budget_client(real,8192).chat.completions.create(**{**original,"reasoning_effort":"none"})

    def test_valid_NO_ABSTAIN_not_retried_in_single_batch(self):
        for rel in ("unrelated","lexical_ambiguity"):
            job=next(j for j in self.plan if j["batch_size"]==1)
            row={"id":job["cases"][0]["id"],"relation":rel}
            response=SimpleNamespace(model=runner.frozen.MODEL,usage=None,choices=[
                SimpleNamespace(finish_reason="stop",message=SimpleNamespace(content=json.dumps({"results":[row]})))])
            seen=[]
            def create(**kw):
                seen.append(kw);return response
            real=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            out=runner.frozen.evaluate_relation_batch(job,runner.r34_budget_client(real,8192),"same prompt","synthetic-key")
            self.assertEqual(len(seen),1);self.assertTrue(out["eventual_valid"])

    def test_failed_batch_cannot_salvage_and_missing_round_rejected(self):
        jobs=self.jobs("b1_t4096")
        jobs[0]["attempts"][0]["valid"]=False
        with self.assertRaisesRegex(ValueError,"fail_open"):
            r34_metrics(jobs,self.cases)
        with self.assertRaisesRegex(ValueError,"incomplete"):
            r34_metrics(self.jobs("b1_t4096")[:-1],self.cases)

    def test_selection_is_predeclared_and_semantics_do_not_regress(self):
        results={a:r34_metrics(self.jobs(a),self.cases) for a,_,_ in runner.ARMS}
        self.assertEqual(r34_select(results),"b2_t4096")
        results["b2_t4096"]["PASS"]=False
        self.assertEqual(r34_select(results),"b2_t8192")
        results["b2_t8192"]["final_pooled_yes"]["recall"]=.99
        self.assertEqual(r34_select(results),"b1_t4096")
        for r in results.values():r["PASS"]=False
        self.assertIsNone(r34_select(results))

    def test_no_R33_holdout_or_runtime_import(self):
        for name in ("run_r34_offline.py","r34_metrics.py","r34_budget_probe.py"):
            text=(runner.HERE/name).read_text()
            self.assertNotIn("r33_holdout.json",text)
            tree=ast.parse(text)
            imports=[n.module or "" for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
            self.assertFalse(any(m.startswith(("r33_data","r33_metrics","neo4j","services.")) for m in imports))


if __name__=="__main__":
    unittest.main()
