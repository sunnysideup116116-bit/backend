"""Hermetic R3.1 failure taxonomy tests; no provider, secret, files or Graph."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import r31_diagnostics as diagnostic
from run_r3_offline import ALLOWED


class R31DiagnosticsTests(unittest.TestCase):
    def check_failure(self, text, wanted, ids=("h01-f",), finish="stop"):
        result = diagnostic.classify_response(text, finish, ids)
        self.assertFalse(result["valid"])
        self.assertIn(wanted, result["errors"])
        self.assertEqual(result["decisions"], {})
        return result

    def row(self, **updates):
        return {"id": "h01-f", "decision": "YES", "relation": "equivalent", **updates}

    def response(self, *rows):
        return json.dumps({"results": list(rows)})

    def test_frozen_mapping_is_reused_and_every_valid_pair_is_accepted(self):
        self.assertIs(diagnostic.ALLOWED, ALLOWED)
        for primary, relations in ALLOWED.items():
            for relation in relations:
                with self.subTest(primary=primary, relation=relation):
                    row = self.row(decision=primary, relation=relation)
                    result = diagnostic.classify_response(self.response(row), "stop", {"h01-f"})
                    self.assertTrue(result["valid"])
                    self.assertEqual(result["decisions"], {"h01-f": row})

    def test_empty_responses(self):
        for value in (None, "", "  \n"):
            with self.subTest(value=value): self.check_failure(value, "empty_response")

    def test_truncation_not_inferred_from_invalid_json(self):
        self.check_failure(self.response(self.row()), "truncated_response", finish="length")
        result = self.check_failure('{"results":[', "invalid_json")
        self.assertNotIn("truncated_response", result["errors"])
        result = self.check_failure('{"results":[', "invalid_json", finish="length")
        self.assertIn("truncated_response", result["errors"])

    def test_code_fences_and_trailing_text_are_not_repaired(self):
        valid = self.response(self.row())
        for text in ("```json\n" + valid + "\n```", valid + " comment"):
            self.check_failure(text, "invalid_json")

    def test_missing_case_and_duplicate_case(self):
        result = self.check_failure(self.response(self.row()), "missing_case", ids=("h01-f", "h01-r"))
        self.assertEqual(result["metadata"]["missing_case_ids"], ["h01-r"])
        result = self.check_failure(self.response(self.row(), self.row()), "duplicate_case")
        self.assertEqual(result["metadata"]["duplicate_case_ids"], ["h01-f"])

    def test_unknown_ids_and_fields_are_not_echoed(self):
        marker = "untrusted-output-never-retained"
        result = self.check_failure(self.response(self.row(id=marker)), "case_id_mismatch")
        self.assertNotIn(marker, json.dumps(result))
        self.assertEqual(result["metadata"]["unknown_case_id_count"], 1)
        result = self.check_failure(self.response({**self.row(), marker: marker}), "extra_unexpected_fields")
        self.assertNotIn(marker, json.dumps(result))

    def test_extra_top_level_field(self):
        self.check_failure(json.dumps({"results": [self.row()], "comment": "unused"}), "extra_unexpected_fields")

    def test_primary_enum_and_types(self):
        for primary in ("MAYBE", "yes", None, [], {}, 4):
            self.check_failure(self.response(self.row(decision=primary)), "invalid_primary_enum")

    def test_secondary_enum_and_types(self):
        for relation in ("semantic_related", "EquiValent", None, [], {}, 4):
            self.check_failure(self.response(self.row(relation=relation)), "invalid_secondary_relation_enum")

    def test_primary_secondary_mismatch(self):
        self.check_failure(self.response(self.row(relation="role_mismatch")), "decision_relation_mismatch")

    def test_any_error_rejects_entire_batch_not_only_bad_row(self):
        self.check_failure(self.response(self.row(), self.row(id="h01-r", decision="broken")),
                           "invalid_primary_enum", ids=("h01-f", "h01-r"))

    def test_invalid_response_types_and_shapes(self):
        for value in ([], {}, 1, b"anything"):
            self.check_failure(value, "invalid_response_type")
        for text in ("null", "[]", "1", '{"results":{}}', '{"results":[null]}'):
            self.check_failure(text, "invalid_output_schema")

    def test_invalid_expected_ids_are_internal_errors(self):
        for value in ([], ["h01-f"] * 2, {None}, ["a"] * 13, "h01-f", (x for x in ["h01-f"])):
            self.check_failure(self.response(self.row()), "parser_internal_failure", ids=value)

    def test_nonstring_id_fails_closed_without_parser_crash(self):
        for value in (None, [], {}, 1):
            result = self.check_failure(self.response(self.row(id=value)), "case_id_mismatch")
            self.assertNotIn("parser_internal_failure", result["errors"])

    def test_missing_row_fields(self):
        self.check_failure('{"results":[{"id":"h01-f"}]}', "invalid_output_schema")

    def test_duplicate_json_fields_not_last_value_wins(self):
        self.check_failure('{"results":[{"id":"h01-f","decision":"NO","decision":"YES","relation":"equivalent"}]}',
                           "duplicate_json_field")

    def test_nonfinite_json_and_oversize_response(self):
        self.check_failure('{"results":NaN}', "invalid_json")
        self.check_failure(" " * (diagnostic.MAX_RESPONSE_CHARS + 1) + "{}", "response_too_large")

    def test_row_count_is_bounded(self):
        self.check_failure(self.response(*[self.row()] * 100), "invalid_output_schema")

    def test_unexpected_finish_reason_not_echoed(self):
        result = self.check_failure(self.response(self.row()), "unexpected_finish_reason", finish="untrusted-provider-text")
        self.assertNotIn("untrusted-provider-text", json.dumps(result))

    def test_internal_exception_is_classified_without_stringifying(self):
        with patch.object(diagnostic.json, "loads", side_effect=RuntimeError("never-echo-internal-text")):
            result = self.check_failure("{}", "parser_internal_failure")
        self.assertNotIn("never-echo", json.dumps(result))

    def test_provider_failure_classification_has_no_exception_text(self):
        class UnsafeException(Exception):
            def __str__(self): raise AssertionError("do not stringify")
        for code, kind, retry in ((401, "auth_failure", False), (403, "auth_failure", False),
                (429, "quota_exhausted", True), (503, "provider_failure", True),
                (504, "timeout", True), (400, "provider_contract_failure", False)):
            exc = UnsafeException(); exc.status_code = code
            self.assertEqual(diagnostic.classify_provider_error(exc),
                             {"error": "provider_api_failure", "subtype": kind, "retryable": retry})
        for exc, kind in ((TimeoutError(), "timeout"), (ConnectionError(), "transport_failure")):
            self.assertEqual(diagnostic.classify_provider_error(exc)["subtype"], kind)
        self.assertFalse(diagnostic.classify_provider_error(UnsafeException())["retryable"])


if __name__ == "__main__":
    unittest.main()
