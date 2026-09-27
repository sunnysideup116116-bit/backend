"""Server-owned rollout routing; readiness is checked separately, never inferred.

The default preserves the deployed canary during a staged code rollout. Only an
explicit operator switch to enabled_accounts removes the legacy list constraint.
Neither mode grants account/profile/Graph authority from a request body.
"""
import os
import re

from matchmaker_agent.related_interest_canary import (
    canary_cohort, canary_pair_enabled, canary_requester_enabled,
)
from matchmaker_agent.related_interest_contract import enabled

ROLLOUT_MODE_ENV = "MATCH_RELATED_INTEREST_ROLLOUT_MODE"


def rollout_mode():
    value = os.getenv(ROLLOUT_MODE_ENV, "canary").strip().lower()
    return value if value in {"canary", "enabled_accounts"} else "off"


def valid_owner_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is not None


def requester_route_allowed(owner):
    if rollout_mode() == "canary":
        return canary_requester_enabled(owner)
    return bool(rollout_mode() == "enabled_accounts" and valid_owner_id(owner)
        and enabled() and os.getenv("MATCH_PREFERENCE_SEMANTIC_MODE", "off").strip().lower() == "active")


def pair_route_allowed(requester, candidate):
    if rollout_mode() == "canary":
        return canary_pair_enabled(requester, candidate)
    return bool(requester_route_allowed(requester) and valid_owner_id(candidate) and candidate != requester)


def legacy_owner_scope():
    """None means data-gated expansion, NOT implicit readiness or authorization."""
    return sorted(canary_cohort()) if rollout_mode() == "canary" else None
