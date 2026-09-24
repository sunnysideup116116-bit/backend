"""Hermetic native-schema comparison tests; no credentials/network."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json,sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parent))
from validator_native_providers import relation_schema,native_evaluate,PROFILES
from validator_backend_metrics import backend_metrics
from r32c_relation import load_complete_cases,RELATION_TO_PRIMARY


class BackendSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases=load_complete_cases()
    def jobs(self):
        out=[]
        for repeat in (1,2,3):
            for i in range(0,96,2):
                cases=self.cases[i:i+2];d={c["id"]:{"id":c["id"],"relation":c["relation"],"decision":c["expected"]} for c in cases}
                a={"valid":True,"errors":[],"decisions":d,"metadata":{"finish_reason":"stop"},"latency_seconds":1,"usage":{"completion_tokens":50}}
                out.append({"ids":list(d),"repeat":repeat,"batch_size":2,"attempts":[a],"first_pass_valid":True,"eventual_valid":True,"elapsed_seconds":1})
        return out

    def test_native_schema_only_relation_not_model_acceptance(self):
        s=relation_schema(["h01-f","h02-r"]);row=s["properties"]["results"]["items"]
        self.assertEqual(set(row["properties"]),{"id","relation"})
        self.assertEqual(row["properties"]["relation"]["enum"],list(RELATION_TO_PRIMARY))
        self.assertEqual(row["properties"]["id"]["enum"],["h01-f","h02-r"])
        self.assertFalse(s["additionalProperties"]);self.assertFalse(row["additionalProperties"])
        self.assertEqual(s["properties"]["results"]["minItems"],2)
        self.assertEqual(s["properties"]["results"]["maxItems"],2)

    def test_stricter_995_first_pass_not_99(self):
        jobs=self.jobs()
        self.assertTrue(backend_metrics(jobs,self.cases)["PASS"])
        good=jobs[0]["attempts"][0]
        bad={**good,"valid":False,"errors":["truncated_response"],"decisions":{}}
        jobs[0]["attempts"]=[bad,good];jobs[0]["first_pass_valid"]=False
        r=backend_metrics(jobs,self.cases)
        self.assertFalse(r["blocking_gates"]["first_pass_validity"])
        self.assertTrue(r["blocking_gates"]["eventual_validity"])

    def test_eventual_requires_sample100_and_fail_closed(self):
        jobs=self.jobs();jobs[0]["first_pass_valid"]=False;jobs[0]["eventual_valid"]=False
        jobs[0]["attempts"][0].update(valid=False,errors=["provider_timeout"],decisions={})
        r=backend_metrics(jobs,self.cases)
        self.assertFalse(r["PASS"]);self.assertFalse(r["blocking_gates"]["eventual_validity"])
        self.assertEqual(r["error_fail_open"],0)
        self.assertEqual(r["final_pooled_yes"]["ERROR"],2)

    def evaluate(self,items):
        job={"backend":"gemini25","repeat":1,"job_id":"synthetic","batch_size":2,"cases":self.cases[:2]}
        calls=[];responses=iter(items)
        def one(*args):
            calls.append(args);return next(responses)
        with patch("validator_native_providers.time.sleep"):
            result=native_evaluate(job,SimpleNamespace(one=one),"same frozen prompt")
        return result,calls

    def valid(self,relation):
        rows=[{"id":c["id"],"relation":relation}for c in self.cases[:2]]
        return {"api_success":True,"text":json.dumps({"results":rows}),"finish_reason":"stop","usage":None,
            "latency_seconds":.1,"wall_seconds":.1,"pacing_seconds":0,"resolved_model":"gemini-2.5-flash"}

    def test_no_or_abstain_is_not_retried(self):
        for relation in ("unrelated","lexical_ambiguity"):
            r,calls=self.evaluate([self.valid(relation)])
            self.assertEqual(len(calls),1);self.assertTrue(r["eventual_valid"])

    def test_transient_error_exactly_one_retry_and_counted_first_failure(self):
        error={"api_success":False,"errors":["quota_exhausted"],"retryable":True,"status":429,
            "latency_seconds":.1,"wall_seconds":.1,"pacing_seconds":0}
        r,calls=self.evaluate([error,self.valid("unrelated")])
        self.assertEqual(len(calls),2);self.assertFalse(r["first_pass_valid"]);self.assertTrue(r["eventual_valid"])
        r,calls=self.evaluate([error,error])
        self.assertEqual(len(calls),2);self.assertFalse(r["eventual_valid"])

    def test_native_enum_does_not_override_semantic_mapping(self):
        bad=self.valid("candidate_more_broad")
        r,calls=self.evaluate([bad])
        self.assertTrue(all(d["decision"]=="NO" for d in r["attempts"][0]["decisions"].values()))

    def test_no_cloud_deepseek_and_no_retired_holdout_reader(self):
        self.assertFalse(any("deepseek" in p["model"] for p in PROFILES.values()))
        for name in ("validator_native_providers.py","run_validator_backend_selection.py","validator_backend_metrics.py"):
            text=(Path(__file__).parent/name).read_text()
            self.assertNotIn("r33_holdout.json",text);self.assertNotIn("r34_final_holdout.json",text)
        self.assertNotIn("final",{"prepare","run","summarize"})


if __name__=="__main__":
    unittest.main()
