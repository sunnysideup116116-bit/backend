"""Offline-only R3.2 primary-authoritative response classification.

Reuse the frozen R3.1 syntax/protocol gate without changing its historical contract.
Valid secondary labels are diagnostics, never a veto or promotion of the model's
primary decision. Genuine schema/transport failures must remain whole-batch ERROR.
This module performs no provider, filesystem, configuration, or Graph operations.
"""
from __future__ import annotations

import json

from r31_diagnostics import ALLOWED, MAX_CASES, classify_response


def classify_primary_response(content, finish_reason, expected_ids):
    """Preserve primary YES/NO/ABSTAIN; diagnose legal secondary disagreement.

    Only a sole ``decision_relation_mismatch`` can be reclassified as valid: the
    original parser has already verified JSON, bounds, exact IDs, unique fields,
    complete rows, both enums, and finish reason. This is not partial salvage.
    Any genuine error retains ``valid=False`` and an empty decision dictionary.
    Known synthetic IDs alone may appear in disagreement metadata.
    """
    result = classify_response(content, finish_reason, expected_ids)
    mismatch = "decision_relation_mismatch" in result["errors"]
    result["metadata"]["secondary_primary_disagreement"] = mismatch
    result["metadata"]["secondary_primary_disagreement_ids"] = []
    if result["errors"] != ["decision_relation_mismatch"]:
        return result

    try:
        # Exact same immutable text, already fully schema-validated by R3.1.
        # Never strip fences, add IDs, repair fields, or rewrite model values.
        rows = json.loads(content)["results"]
        expected = set(expected_ids)
        if not 1 <= len(rows) <= MAX_CASES or len(rows) != len(expected):
            raise ValueError("validated_reparse_invariant")
        decisions = {}
        disagreement_ids = []
        legal_relations = set().union(*ALLOWED.values())
        for row in rows:
            if (set(row) != {"id", "decision", "relation"}
                    or row["id"] not in expected or row["id"] in decisions
                    or row["decision"] not in ALLOWED
                    or row["relation"] not in legal_relations):
                raise ValueError("validated_reparse_invariant")
            decisions[row["id"]] = row
            if row["relation"] not in ALLOWED[row["decision"]]:
                disagreement_ids.append(row["id"])
        if set(decisions) != expected or not disagreement_ids:
            raise ValueError("validated_reparse_invariant")
    except Exception:
        # No response, exception message, or unvalidated values leave this path.
        result["valid"] = False
        result["decisions"] = {}
        result["errors"] = ["parser_internal_failure", "decision_relation_mismatch"]
        return result

    result["valid"] = True
    result["errors"] = []
    result["decisions"] = decisions
    result["metadata"]["secondary_primary_disagreement_ids"] = sorted(disagreement_ids)
    return result
