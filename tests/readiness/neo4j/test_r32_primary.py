"""Hermetic offline tests; no provider, Graph, credentials, or runtime mutation."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import r31_diagnostics
import r32_primary


class R32PrimaryTests(unittest.TestCase):
    def row(self, **updates):
        return {"id": "h01-f", "decision": "YES", "relation": "equivalent", **updates}

    def response(self, *rows):
        return json.dumps({"results": list(rows)})

    def classify(self, text, ids=("h01-f",), finish="stop"):
        return r32_primary.classify_primary_response(text, finish, ids)

    def assert_closed(self, result, error):
        self.assertFalse(result["valid"])
        self.assertIn(error, result["errors"])
        self.assertEqual(result["decisions"], {})
        self.assertEqual(result["metadata"]["secondary_primary_disagreement_ids"], [])

    def test_yes_secondary_mismatch_does_not_veto_primary(self):
        row = self.row(relation="role_mismatch")
        result = self.classify(self.response(row))
        self.assertTrue(result["valid"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["decisions"], {"h01-f": row})
        self.assertTrue(result["metadata"]["secondary_primary_disagreement"])
        self.assertEqual(result["metadata"]["secondary_primary_disagreement_ids"], ["h01-f"])

    def test_no_and_abstain_secondary_equivalent_cannot_promote_to_yes(self):
        for primary in ("NO", "ABSTAIN"):
            with self.subTest(primary=primary):
                row = self.row(decision=primary)
                result = self.classify(self.response(row))
                self.assertTrue(result["valid"])
                self.assertEqual(result["decisions"]["h01-f"], row)
                # Proposed expansion rule depends on the preserved primary only.
                eligible = [d for d in result["decisions"].values() if d["decision"] == "YES"]
                self.assertEqual(eligible, [])

    def test_every_legal_enum_combination_preserves_original_values(self):
        relations = set().union(*r31_diagnostics.ALLOWED.values())
        for primary, aligned_relations in r31_diagnostics.ALLOWED.items():
            for relation in relations:
                with self.subTest(primary=primary, relation=relation):
                    row = self.row(decision=primary, relation=relation)
                    result = self.classify(self.response(row))
                    self.assertTrue(result["valid"])
                    self.assertEqual(result["decisions"], {"h01-f": row})
                    self.assertEqual(result["metadata"]["secondary_primary_disagreement"],
                                     relation not in aligned_relations)

    def test_valid_aligned_output_is_identical_except_added_diagnostics(self):
        text = self.response(self.row())
        original = r31_diagnostics.classify_response(text, "stop", ("h01-f",))
        result = self.classify(text)
        self.assertFalse(result["metadata"].pop("secondary_primary_disagreement"))
        self.assertEqual(result["metadata"].pop("secondary_primary_disagreement_ids"), [])
        self.assertEqual(result, original)

    def test_mixed_valid_rows_preserve_whole_batch_and_bound_diagnostic_ids(self):
        rows = [self.row(id="h02-f", decision="NO"), self.row(),
                self.row(id="h03-f", decision="ABSTAIN")]
        result = self.classify(self.response(*rows), ids=("h01-f", "h02-f", "h03-f"))
        self.assertTrue(result["valid"])
        self.assertEqual(result["decisions"], {r["id"]: r for r in rows})
        self.assertEqual(result["metadata"]["secondary_primary_disagreement_ids"],
                         ["h02-f", "h03-f"])

    def test_invalid_secondary_is_error_not_semantic_no(self):
        for relation in ("made_up_relation", None, [], {}):
            with self.subTest(relation_type=type(relation).__name__):
                result = self.classify(self.response(self.row(relation=relation)))
                self.assert_closed(result, "invalid_secondary_relation_enum")

    def test_invalid_primary_is_error(self):
        self.assert_closed(self.classify(self.response(self.row(decision="maybe"))),
                           "invalid_primary_enum")

    def test_mismatch_plus_genuine_schema_errors_never_salvages_rows(self):
        mismatch = self.row(relation="role_mismatch")
        tests = [
            (self.response(mismatch), ("h01-f", "h02-f"), "missing_case", "stop"),
            (self.response(mismatch, mismatch), ("h01-f",), "duplicate_case", "stop"),
            (self.response({**mismatch, "extra": "untrusted-hidden-value"}), ("h01-f",),
             "extra_unexpected_fields", "stop"),
            (self.response(mismatch, self.row(id="h02-f", relation="bad")),
             ("h01-f", "h02-f"), "invalid_secondary_relation_enum", "stop"),
            (self.response(mismatch, self.row(id="unexpected-id")),
             ("h01-f",), "case_id_mismatch", "stop"),
            (self.response(mismatch), ("h01-f",), "truncated_response", "length"),
        ]
        for text, ids, error, finish in tests:
            with self.subTest(error=error):
                result = self.classify(text, ids=ids, finish=finish)
                self.assert_closed(result, error)
                self.assertTrue(result["metadata"]["secondary_primary_disagreement"])
                self.assertNotIn("untrusted-hidden-value", json.dumps(result))
                self.assertNotIn("unexpected-id", json.dumps(result))

    def test_empty_truncated_malformed_and_fenced_outputs_fail_closed(self):
        for content, finish, error in (
                (None, "stop", "empty_response"),
                ("", "length", "truncated_response"),
                ("not JSON", "stop", "invalid_json"),
                ("```json\n" + self.response(self.row()) + "\n```", "stop", "invalid_json")):
            with self.subTest(error=error):
                self.assert_closed(self.classify(content, finish=finish), error)

    def test_duplicate_json_fields_not_repaired_on_mismatch(self):
        content = ('{"results":[{"id":"h01-f","decision":"NO",'
                   '"decision":"YES","relation":"role_mismatch"}]}')
        self.assert_closed(self.classify(content), "duplicate_json_field")

    def test_internal_reparse_failure_has_no_response_or_exception_leak(self):
        text = self.response(self.row(relation="role_mismatch"))
        original = r31_diagnostics.classify_response(text, "stop", ("h01-f",))
        with patch.object(r32_primary, "classify_response", return_value=original), \
                patch.object(r32_primary.json, "loads", side_effect=RuntimeError("do-not-echo")):
            result = self.classify(text)
        self.assert_closed(result, "parser_internal_failure")
        self.assertNotIn("do-not-echo", json.dumps(result))

    def test_frozen_r31_classifier_retains_its_original_mismatch_error(self):
        text = self.response(self.row(relation="role_mismatch"))
        original = r31_diagnostics.classify_response(text, "stop", ("h01-f",))
        self.assertEqual(original["errors"], ["decision_relation_mismatch"])
        self.assertFalse(original["valid"])
        self.assertEqual(original["decisions"], {})


if __name__ == "__main__":
    unittest.main()
