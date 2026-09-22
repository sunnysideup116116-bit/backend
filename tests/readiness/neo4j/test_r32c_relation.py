"""Hermetic complete-input and strict relation-only mapping tests; no provider IO."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import r32c_relation as relation


class R32cRelationTests(unittest.TestCase):
    def payload(self, kind="equivalent", case_id="h01-f"):
        return json.dumps({"results": [{"id": case_id, "relation": kind}]})

    def assert_failed(self, content, error, ids=("h01-f",), finish="stop"):
        result = relation.classify_relation_response(content, finish, ids)
        self.assertFalse(result["valid"])
        self.assertIn(error, result["errors"])
        self.assertEqual(result["decisions"], {})
        return result

    def test_each_enum_maps_to_user_fixed_policy(self):
        expected = {"equivalent": "YES", "candidate_more_specific": "YES",
                    "candidate_more_broad": "NO", "sibling_related": "NO",
                    "role_mismatch": "NO", "constraint_conflict": "NO",
                    "lexical_ambiguity": "ABSTAIN", "unrelated": "NO", "unknown": "ABSTAIN"}
        self.assertEqual(relation.RELATION_TO_PRIMARY, expected)
        for kind, primary in expected.items():
            with self.subTest(kind=kind):
                self.assertEqual(relation.map_relation(kind), primary)
                parsed = relation.classify_relation_response(self.payload(kind), "stop", {"h01-f"})
                self.assertTrue(parsed["valid"])
                self.assertEqual(parsed["decisions"]["h01-f"],
                                 {"id": "h01-f", "relation": kind, "decision": primary})

    def test_invalid_enum_never_maps_to_rejection_or_acceptance(self):
        for bad in (None, [], {}, 0, True, "YES", "Equivalent", "equivalent ", "candidate_broader_insufficient"):
            with self.subTest(value=bad), self.assertRaisesRegex(ValueError, "invalid_relation_enum"):
                relation.map_relation(bad)

    def test_frozen_full_input_exact_and_primary_labels_unchanged(self):
        source = (relation.HERE / "r3_holdout.json").read_bytes()
        self.assertEqual(hashlib.sha256(source).hexdigest(), relation.SOURCE_FIXTURE_SHA256)
        original = relation.expand_cases(json.loads(source))
        cases = relation.load_complete_cases()
        self.assertEqual(len(cases), 96)
        self.assertEqual(Counter(c["expected"] for c in cases), {"YES": 28, "NO": 60, "ABSTAIN": 8})
        for old, new in zip(original, cases):
            for field in ("id", "Q", "C", "expected", "language", "category", "group"):
                self.assertEqual(new[field], old[field])
            self.assertEqual(new["source_relation"], old["relation"])
            self.assertEqual(relation.map_relation(new["relation"]), old["expected"])
        by_id = {c["id"]: c for c in cases}
        self.assertEqual(by_id["h11-r"]["Q"], "Watching Waterfalls on Short Accessible Trails")
        self.assertEqual(by_id["h47-r"]["Q"], "Visiting Quiet Castles without Guided Groups")
        self.assertGreater(len(by_id["h11-r"]["Q"]), 40)
        self.assertGreater(len(by_id["h47-r"]["Q"]), 40)
        self.assertEqual((relation.HERE / "r3_holdout.json").read_bytes(), source)

    def test_predeclared_projection_not_primary_relabeling(self):
        cases = relation.load_complete_cases()
        for case in cases:
            if case["category"] == "homonym":
                self.assertEqual((case["source_relation"], case["relation"], case["expected"]),
                                 ("lexical_ambiguity", "unrelated", "NO"))
            elif case["category"] == "ambiguous":
                self.assertEqual((case["source_relation"], case["relation"], case["expected"]),
                                 ("unknown", "lexical_ambiguity", "ABSTAIN"))
            elif case["source_relation"] == "candidate_specific_satisfies_broader_query":
                self.assertEqual(case["relation"], "candidate_more_specific")
            elif case["source_relation"] == "candidate_broader_insufficient":
                self.assertEqual(case["relation"], "candidate_more_broad")

    def test_fixture_tamper_rejected_before_expansion(self):
        with patch.object(Path, "read_bytes", return_value=b"changed"), \
                patch.object(relation, "expand_cases") as expand:
            with self.assertRaisesRegex(ValueError, "frozen_fixture_changed"):
                relation.load_complete_cases()
            expand.assert_not_called()

    def test_complete_input_bound_rejects_never_truncates(self):
        cases = relation.expand_cases(json.loads((relation.HERE / "r3_holdout.json").read_bytes()))
        for text in ("x" * 121, "", None):
            modified = deepcopy(cases)
            modified[0]["Q"] = text
            with self.subTest(text_length=len(text) if isinstance(text, str) else None), \
                    patch.object(relation, "expand_cases", return_value=modified):
                with self.assertRaisesRegex(ValueError, "invalid_complete_concept_bound"):
                    relation.load_complete_cases()
                self.assertEqual(modified[0]["Q"], text)
        modified = deepcopy(cases)
        modified[0]["Q"] = "x" * 120
        with patch.object(relation, "expand_cases", return_value=modified):
            self.assertEqual(relation.load_complete_cases()[0]["Q"], "x" * 120)

    def test_unreviewed_projection_and_primary_change_rejected(self):
        cases = relation.expand_cases(json.loads((relation.HERE / "r3_holdout.json").read_bytes()))
        for index, change in ((0, {"expected": "NO"}), (16, {"category": "ambiguous"}),
                              (22, {"category": "homonym"})):
            modified = deepcopy(cases)
            modified[index].update(change)
            with self.subTest(change=change), patch.object(relation, "expand_cases", return_value=modified):
                with self.assertRaises(ValueError):
                    relation.load_complete_cases()

    def test_batch_one_or_two_only_and_known_ids(self):
        text = json.dumps({"results": [{"id": "h01-f", "relation": "equivalent"},
                                       {"id": "h01-r", "relation": "candidate_more_broad"}]})
        result = relation.classify_relation_response(text, "stop", {"h01-r", "h01-f"})
        self.assertTrue(result["valid"])
        self.assertEqual({row["decision"] for row in result["decisions"].values()}, {"YES", "NO"})
        for ids in ([], ["h01-f"] * 2, ["h01-f", "h01-r", "h02-f"], "h01-f", ["bad id"], [None]):
            with self.subTest(ids=ids):
                self.assert_failed(self.payload(), "parser_internal_failure", ids=ids)

    def test_model_acceptance_secondary_or_explanation_cannot_override_policy(self):
        for field, value in (("decision", "YES"), ("secondary", "equivalent"),
                             ("confidence", 1), ("reason", "synthetic explanation")):
            content = json.dumps({"results": [{"id": "h01-f", "relation": "candidate_more_broad", field: value}]})
            with self.subTest(field=field):
                self.assert_failed(content, "extra_unexpected_fields")

    def test_invalid_json_empty_nonfinite_fences_and_duplicate_fields(self):
        for content in ("not-json", "{", "```json\n" + self.payload() + "\n```",
                        '{"results":NaN}', '{"results":Infinity}', '{"results":-Infinity}'):
            with self.subTest(content=content):
                self.assert_failed(content, "invalid_json")
        for content in (None, "", "  "):
            self.assert_failed(content, "empty_response")
        self.assert_failed(2, "invalid_response_type")
        self.assert_failed('{"results":[],"results":[]}', "duplicate_json_field")
        self.assert_failed('{"results":[{"id":"h01-f","relation":"equivalent","relation":"unknown"}]}',
                           "duplicate_json_field")

    def test_missing_duplicate_and_unknown_ids_fail_whole_batch(self):
        self.assert_failed(self.payload(), "missing_case", ids=("h01-f", "h01-r"))
        duplicate = json.dumps({"results": [{"id": "h01-f", "relation": "equivalent"}] * 2})
        self.assert_failed(duplicate, "duplicate_case")
        self.assert_failed(self.payload(case_id="h99-f"), "case_id_mismatch")
        for obj in ([], None, {"results": None}, {"results": [None]}, {"results": [{"id": "h01-f"}]}):
            self.assert_failed(json.dumps(obj), "invalid_output_schema")
        self.assert_failed(json.dumps({"results": []}), "missing_case")

    def test_invalid_relation_no_partial_salvage(self):
        for bad in ("YES", "candidate_broader_insufficient", "equivalent ", {}, [], None, 1):
            content = json.dumps({"results": [{"id": "h01-f", "relation": "equivalent"},
                                             {"id": "h01-r", "relation": bad}]})
            self.assert_failed(content, "invalid_relation_enum", ids=("h01-f", "h01-r"))

    def test_finish_reason_and_size_are_fail_closed(self):
        self.assert_failed(self.payload(), "truncated_response", finish="length")
        for finish in (None, "unexpected", "content_filter", "tool_calls"):
            self.assert_failed(self.payload(), "unexpected_finish_reason", finish=finish)
        self.assert_failed("x" * (relation.MAX_RESPONSE_CHARS + 1), "response_too_large")
        result = self.assert_failed("{bad", "invalid_json")
        self.assertNotIn("truncated_response", result["errors"])

    def test_metadata_never_retains_unknown_text_fields_enum_or_exception(self):
        marker = "synthetic-secret-marker-not-for-retention"
        content = json.dumps({marker: marker, "results": [{"id": marker, "relation": marker, marker: marker}]})
        result = self.assert_failed(content, "extra_unexpected_fields", finish=marker)
        self.assertNotIn(marker, json.dumps(result))
        self.assertEqual(result["metadata"]["unknown_case_id_count"], 1)
        self.assertEqual(result["metadata"]["unexpected_field_count"], 2)
        with patch.object(relation.json, "loads", side_effect=RuntimeError(marker)):
            result = self.assert_failed(self.payload(), "parser_internal_failure")
            self.assertNotIn(marker, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
