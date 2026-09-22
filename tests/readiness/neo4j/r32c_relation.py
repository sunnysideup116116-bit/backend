"""Offline R3.2c complete synthetic input and relation-only acceptance policy.

No canonicalization, provider client, Graph client or runtime settings are loaded.
The original fixture bytes/primary labels remain frozen. New relation vocabulary
is projected before scoring: old clear, disambiguated homonyms become unrelated;
old unresolved ambiguity becomes lexical_ambiguity. This is an ontology mapping,
not model-output-driven relabeling. The model never supplies acceptance policy.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from run_r3_offline import expand_cases

HERE = Path(__file__).resolve().parent
SOURCE_FIXTURE_SHA256 = "0943c22f0f3aa4e9091644f811161ca8342fa00838359f2ded9239cf303b70e7"
MAX_CONCEPT_CHARS = 120
MAX_CASES = 2
MAX_RESPONSE_CHARS = 65536
RELATION_TO_PRIMARY = {
    "equivalent": "YES",
    "candidate_more_specific": "YES",
    "candidate_more_broad": "NO",
    "sibling_related": "NO",
    "role_mismatch": "NO",
    "constraint_conflict": "NO",
    "lexical_ambiguity": "ABSTAIN",
    "unrelated": "NO",
    "unknown": "ABSTAIN",
}
ERROR_ORDER = (
    "parser_internal_failure", "invalid_response_type", "empty_response",
    "truncated_response", "unexpected_finish_reason", "response_too_large",
    "invalid_json", "duplicate_json_field", "invalid_output_schema",
    "extra_unexpected_fields", "case_id_mismatch", "duplicate_case",
    "missing_case", "invalid_relation_enum",
)


def map_relation(relation):
    """A strict deterministic map; invalid enum is ERROR, never NO or ABSTAIN."""
    if not isinstance(relation, str) or relation not in RELATION_TO_PRIMARY:
        raise ValueError("invalid_relation_enum")
    return RELATION_TO_PRIMARY[relation]


def load_complete_cases():
    """Read exactly the frozen 96 synthetic cases without lossy preprocessing."""
    source = (HERE / "r3_holdout.json").read_bytes()
    if hashlib.sha256(source).hexdigest() != SOURCE_FIXTURE_SHA256:
        raise ValueError("frozen_fixture_changed")
    cases = expand_cases(json.loads(source))
    if len(cases) != 96 or len({case["id"] for case in cases}) != 96:
        raise ValueError("expected_frozen_96_cases")
    output = []
    for case in cases:
        if not all(isinstance(case.get(field), str)
                   and 0 < len(case[field]) <= MAX_CONCEPT_CHARS for field in ("Q", "C")):
            raise ValueError("invalid_complete_concept_bound")
        old = case["relation"]
        projected = {
            "candidate_specific_satisfies_broader_query": "candidate_more_specific",
            "candidate_broader_insufficient": "candidate_more_broad",
        }.get(old, old)
        if old == "lexical_ambiguity":
            if case["category"] != "homonym" or case["expected"] != "NO":
                raise ValueError("unreviewed_relation_projection")
            projected = "unrelated"
        elif old == "unknown":
            if case["category"] != "ambiguous" or case["expected"] != "ABSTAIN":
                raise ValueError("unreviewed_relation_projection")
            projected = "lexical_ambiguity"
        if map_relation(projected) != case["expected"]:
            raise ValueError("projection_must_preserve_primary_gold")
        output.append({**case, "source_relation": old, "relation": projected})
    return output


def classify_relation_response(content, finish_reason, expected_ids):
    """Validate a whole one/two-case batch and derive decisions in code only.

    Any schema/provider finish error clears every decision. No partial salvage,
    model-proposed decision field, reasoning, unknown field/ID, raw text or error
    string is retained. Diagnostics contain only enums/counts/known fixture IDs.
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
                    r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", case_id) for case_id in expected_ids)
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
                extras = set(obj) - {"results"}
                metadata["unexpected_field_count"] += len(extras)
                if extras:
                    errors.add("extra_unexpected_fields")
                rows = obj.get("results")
                if not isinstance(rows, list):
                    errors.update({"invalid_output_schema", "missing_case"})
                    metadata["missing_case_ids"] = sorted(expected)
                else:
                    metadata["returned_case_count"] = len(rows)
                    if len(rows) > MAX_CASES:
                        errors.add("invalid_output_schema")
                    seen, duplicates = set(), set()
                    for row in rows[:MAX_CASES + 1]:
                        if not isinstance(row, dict):
                            errors.add("invalid_output_schema")
                            continue
                        extras = set(row) - {"id", "relation"}
                        metadata["unexpected_field_count"] += len(extras)
                        if extras:
                            errors.add("extra_unexpected_fields")
                        if set(row) != {"id", "relation"}:
                            errors.add("invalid_output_schema")
                        case_id, relation = row.get("id"), row.get("relation")
                        if not isinstance(case_id, str) or case_id not in expected:
                            errors.add("case_id_mismatch")
                            metadata["unknown_case_id_count"] += 1
                        elif case_id in seen:
                            errors.add("duplicate_case")
                            duplicates.add(case_id)
                        else:
                            seen.add(case_id)
                        valid_relation = isinstance(relation, str) and relation in RELATION_TO_PRIMARY
                        if not valid_relation:
                            errors.add("invalid_relation_enum")
                        if isinstance(case_id, str) and case_id in expected and valid_relation:
                            decisions[case_id] = {"id": case_id, "relation": relation,
                                                  "decision": map_relation(relation)}
                    metadata["duplicate_case_ids"] = sorted(duplicates)
                    metadata["missing_case_ids"] = sorted(expected - seen)
                    if expected - seen:
                        errors.add("missing_case")
            elif obj is not None or not errors.intersection(
                    {"empty_response", "invalid_response_type", "response_too_large", "invalid_json"}):
                errors.add("invalid_output_schema")
    except Exception:
        errors.add("parser_internal_failure")
    return {"valid": not errors, "errors": [error for error in ERROR_ORDER if error in errors],
            "decisions": decisions if not errors else {}, "metadata": metadata}
