"""Secret-safe, fail-closed diagnostics for the synthetic-only R3.1 experiment.

No provider, filesystem, Graph, runtime configuration or prompt mutations occur here.
Only known input IDs and allowlisted metadata leave the response classifier. Unknown
returned IDs, field names/values, response text and exception strings are never logged.
"""
from __future__ import annotations

import json
import re

from run_r3_offline import ALLOWED

MAX_CASES = 12
MAX_RESPONSE_CHARS = 65536
ERROR_ORDER = (
    "parser_internal_failure", "invalid_response_type", "empty_response",
    "truncated_response", "unexpected_finish_reason", "response_too_large",
    "invalid_json", "duplicate_json_field", "invalid_output_schema",
    "extra_unexpected_fields", "case_id_mismatch", "duplicate_case",
    "missing_case", "invalid_primary_enum", "invalid_secondary_relation_enum",
    "decision_relation_mismatch",
)


def classify_response(content, finish_reason, expected_ids):
    """Return strict whole-batch decisions, or ERROR metadata with no decisions.

    A failed batch is never repaired, salvaged, mapped to NO/ABSTAIN, or accepted
    because a subset was valid. Multiple observed errors can describe one attempt.
    ``truncated_response`` requires provider ``finish_reason=length``; malformed
    JSON alone is not evidence of truncation. Unknown ID text is not retained.
    """
    errors = set()
    metadata = {"char_count": len(content) if isinstance(content, str) else None,
                "expected_case_count": 0, "returned_case_count": 0,
                "missing_case_ids": [], "duplicate_case_ids": [],
                "unknown_case_id_count": 0, "unexpected_field_count": 0,
                "finish_reason": finish_reason if isinstance(finish_reason, str)
                and finish_reason in {"stop", "length", "content_filter", "tool_calls",
                                      "function_call"} else "unknown"}
    decisions = {}
    try:
        if (not isinstance(expected_ids, (list, tuple, set, frozenset))
                or not 1 <= len(expected_ids) <= MAX_CASES
                or any(not isinstance(case_id, str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", case_id)
                       for case_id in expected_ids)
                or len(set(expected_ids)) != len(expected_ids)):
            errors.add("parser_internal_failure")
        else:
            expected = set(expected_ids)
            metadata["expected_case_count"] = len(expected)
            if finish_reason == "length":
                errors.add("truncated_response")
            elif finish_reason != "stop":
                errors.add("unexpected_finish_reason")

            obj = None
            if not isinstance(content, str):
                errors.add("empty_response" if content is None else "invalid_response_type")
            elif not content.strip():
                errors.add("empty_response")
            elif len(content) > MAX_RESPONSE_CHARS:
                errors.add("response_too_large")
            else:
                def reject_duplicate_fields(pairs):
                    fields = {}
                    for name, value in pairs:
                        if name in fields:
                            errors.add("duplicate_json_field")
                        fields[name] = value
                    return fields

                def reject_nonfinite_constant(_value):
                    raise ValueError("nonfinite_json_constant")

                try:
                    obj = json.loads(content, object_pairs_hook=reject_duplicate_fields,
                                     parse_constant=reject_nonfinite_constant)
                except (ValueError, RecursionError):
                    errors.add("invalid_json")

            if isinstance(obj, dict):
                metadata["unexpected_field_count"] += len(set(obj) - {"results"})
                if set(obj) - {"results"}:
                    errors.add("extra_unexpected_fields")
                rows = obj.get("results")
                if not isinstance(rows, list):
                    errors.add("invalid_output_schema")
                    errors.add("missing_case")
                    metadata["missing_case_ids"] = sorted(expected)
                else:
                    metadata["returned_case_count"] = len(rows)
                    seen = set()
                    duplicates = set()
                    # A bounded response may still contain many tiny rows. Inspect
                    # at most one extra row, and never accept an overlarge batch.
                    if len(rows) > MAX_CASES:
                        errors.add("invalid_output_schema")
                    for row in rows[:MAX_CASES + 1]:
                        if not isinstance(row, dict):
                            errors.add("invalid_output_schema")
                            continue
                        extra = set(row) - {"id", "decision", "relation"}
                        metadata["unexpected_field_count"] += len(extra)
                        if extra:
                            errors.add("extra_unexpected_fields")
                        if set(row) != {"id", "decision", "relation"}:
                            errors.add("invalid_output_schema")
                        case_id = row.get("id")
                        if not isinstance(case_id, str) or case_id not in expected:
                            errors.add("case_id_mismatch")
                            metadata["unknown_case_id_count"] += 1
                        elif case_id in seen:
                            errors.add("duplicate_case")
                            duplicates.add(case_id)
                        else:
                            seen.add(case_id)
                        decision, relation = row.get("decision"), row.get("relation")
                        valid_primary = isinstance(decision, str) and decision in ALLOWED
                        valid_relation = (isinstance(relation, str)
                                          and any(relation in kinds for kinds in ALLOWED.values()))
                        if not valid_primary:
                            errors.add("invalid_primary_enum")
                        if not valid_relation:
                            errors.add("invalid_secondary_relation_enum")
                        if valid_primary and valid_relation and relation not in ALLOWED[decision]:
                            errors.add("decision_relation_mismatch")
                        if isinstance(case_id, str) and case_id in expected:
                            decisions[case_id] = {"id": case_id, "decision": decision,
                                                  "relation": relation}
                    metadata["duplicate_case_ids"] = sorted(duplicates)
                    metadata["missing_case_ids"] = sorted(expected - seen)
                    if expected - seen:
                        errors.add("missing_case")
            elif obj is not None or not errors.intersection(
                    {"empty_response", "invalid_response_type", "response_too_large", "invalid_json"}):
                errors.add("invalid_output_schema")
    except Exception:
        # Do not stringify or retain unexpected parser exceptions/objects.
        errors.add("parser_internal_failure")
    return {"valid": not errors, "errors": [e for e in ERROR_ORDER if e in errors],
            "decisions": decisions if not errors else {}, "metadata": metadata}


def classify_provider_error(exc):
    """Classify an API exception without reading its message, request or body."""
    subtype, retryable = "unexpected_provider_failure", False
    try:
        status = getattr(exc, "status_code", None)
        if type(status) is int:
            if status in {401, 403}:
                subtype = "auth_failure"
            elif status == 429:
                subtype, retryable = "quota_exhausted", True
            elif status in {400, 404, 405, 415, 422}:
                subtype = "provider_contract_failure"
            elif status in {408, 504}:
                subtype, retryable = "timeout", True
            elif 500 <= status <= 599:
                subtype, retryable = "provider_failure", True
        elif isinstance(exc, TimeoutError) or type(exc).__name__ in {
                "APITimeoutError", "TimeoutException", "ReadTimeout", "ConnectTimeout"}:
            subtype, retryable = "timeout", True
        elif isinstance(exc, ConnectionError) or type(exc).__name__ in {
                "APIConnectionError", "ConnectError", "ReadError", "RemoteProtocolError"}:
            subtype, retryable = "transport_failure", True
    except Exception:
        pass
    return {"error": "provider_api_failure", "subtype": subtype, "retryable": retryable}
