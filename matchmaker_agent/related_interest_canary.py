"""Server-owned two-account isolation, independent of relation/identity policy.

Operator injects the same JSON array into Social and 9001 via start_all.sh.
No client flag, profile nickname, request field or implicit all-user default.
"""
import json
import os
import re

from matchmaker_agent.related_interest_contract import enabled

COHORT_ENV = "MATCH_RELATED_INTEREST_CANARY_USER_IDS"


def canary_cohort() -> frozenset[str]:
    raw = os.getenv(COHORT_ENV, "")
    if not raw or len(raw) > 300:
        return frozenset()
    try:
        values = json.loads(raw)
    except (ValueError, TypeError):
        return frozenset()
    if (not isinstance(values, list) or len(values) != 2
            or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", v)
                   for v in values)
            or values[0] == values[1]):
        return frozenset()
    return frozenset(values)


def canary_requester_enabled(requester_id: str) -> bool:
    # enabled() gives the shared kill file precedence; re-read at work boundaries.
    return bool(enabled()
                and os.getenv("MATCH_PREFERENCE_SEMANTIC_MODE", "off").strip().lower() == "active"
                and isinstance(requester_id, str) and requester_id in canary_cohort())


def canary_pair_enabled(requester_id: str, candidate_id: str) -> bool:
    return bool(canary_requester_enabled(requester_id)
                and isinstance(candidate_id, str) and candidate_id != requester_id
                and candidate_id in canary_cohort())
