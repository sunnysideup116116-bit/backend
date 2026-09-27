"""Bounded, request-local authoritative metadata checks for semantic rollout.

No preference requirement for requesters; no account creation, quota mutation,
positive cross-request cache, retries, raw profile projection or secret logging.
Social and 9001 use the existing Social profile database and shared Appwrite
identity/signing configuration. The supplied Graph session owns its deadline.
"""
import math
import time
from urllib.parse import quote

import requests
from neo4j import Query
from pymongo import timeout as mongo_timeout
from urllib3.util import Timeout

from matchmaker_agent.semantic_rollout_policy import valid_owner_id

PROFILE_FIELDS = {
    "user_id": 1, "enabled": 1, "active": 1, "is_active": 1,
    "disabled": 1, "is_disabled": 1, "blocked": 1, "is_blocked": 1,
    "deleted_at": 1, "status": 1, "preference_bootstrap_pending": 1,
}
_profiles = None


def initialize_profiles():
    """Resolve Mongo/SRV at service startup, never inside a request deadline."""
    from matchmaker_agent.semantic_rollout_policy import rollout_mode
    if rollout_mode() == 'enabled_accounts':
        from database import profiles_coll
        global _profiles
        _profiles = profiles_coll


class SemanticEligibilityUnavailable(RuntimeError):
    pass


def state_allows_use(row):
    if not isinstance(row, dict):
        return False
    for key in ("enabled", "active", "is_active"):
        if key in row and row[key] is not None and row[key] is not True:
            return False
    for key in ("disabled", "is_disabled", "blocked", "is_blocked"):
        if key in row and row[key] is not None and row[key] is not False:
            return False
    status = row.get("status")
    if status not in (None, 'active', 'enabled'):
        return False
    return not row.get("deleted_at") and status not in {
        "disabled", "blocked", "deleted", "suspended", "inactive",
    } and not row.get("preference_bootstrap_pending") and not row.get("preference_projection_pending")


def lookup_enabled_account(owner, *, deadline, clock=time.monotonic, http=requests):
    """One GET under the caller's remaining budget; no retry or provider usage."""
    from agent_quota.service import AppwriteStore
    remaining = deadline - clock()
    if remaining <= 0:
        raise SemanticEligibilityUnavailable("semantic_eligibility_unavailable")
    try:
        # Reuse the project's existing shared admin-config resolver. Creating
        # this transport does not create/refill/read a quota account.
        store = AppwriteStore()
        budget = min(2.0, remaining)
        response = http.get(store.endpoint + "/users/" + quote(owner, safe=""),
            headers={**store.headers, "Accept": "application/json"},
            timeout=Timeout(total=budget, connect=min(.5, budget), read=budget),
            verify=store.verify, allow_redirects=False)
        if clock() >= deadline:
            raise SemanticEligibilityUnavailable("semantic_eligibility_unavailable")
        if response.status_code == 404:
            return False
        if response.status_code != 200:
            raise SemanticEligibilityUnavailable("semantic_eligibility_unavailable")
        account = response.json()
        if not isinstance(account, dict) or account.get("$id") != owner or type(account.get("status")) is not bool:
            raise SemanticEligibilityUnavailable("semantic_eligibility_unavailable")
        return account["status"]
    except Exception:
        raise SemanticEligibilityUnavailable("semantic_eligibility_unavailable") from None


class EnabledUserChecks:
    """One bounded lookup per owner per operation; final recheck uses a NEW instance."""
    def __init__(self, session, *, deadline, profiles=None, account_lookup=None, clock=time.monotonic):
        self.session, self.deadline, self.clock = session, deadline, clock
        self.profiles, self.account_lookup = profiles, account_lookup or lookup_enabled_account
        self.cache = {}
        self.unavailable = False

    def check(self, owner):
        if self.clock() >= self.deadline:
            self.unavailable = True
            return False
        if not valid_owner_id(owner):
            return False
        if owner in self.cache:
            return self.cache[owner]
        # Request-path candidate hard bound is 50, plus its requester. This
        # bounds work per request, not population size or rollout membership.
        if len(self.cache) >= 51:
            return False
        self.cache[owner] = False
        try:
            if not self.account_lookup(owner, deadline=self.deadline, clock=self.clock):
                return False
            with mongo_timeout(max(.001, min(2.0, self.deadline - self.clock()))):
                if self.profiles is None:
                    if _profiles is None:
                        raise SemanticEligibilityUnavailable('semantic_eligibility_unavailable')
                    self.profiles = _profiles
                profiles = list(self.profiles.find({"user_id": owner}, PROFILE_FIELDS).limit(2))
            if (len(profiles) != 1 or profiles[0].get("user_id") != owner
                    or not profiles[0].get("_id") or not state_allows_use(profiles[0])):
                return False
            remaining = self.deadline - self.clock()
            if not math.isfinite(remaining) or remaining <= 0:
                self.unavailable = True
                return False
            users = list(self.session.run(Query("""
                MATCH (u:User {id:$owner}) RETURN u.id AS id,u.enabled AS enabled,
                  u.active AS active,u.is_active AS is_active,u.disabled AS disabled,u.blocked AS blocked,
                  u.is_disabled AS is_disabled,u.is_blocked AS is_blocked,
                  u.deleted_at AS deleted_at,u.status AS status,
                  u.preference_projection_pending AS preference_projection_pending LIMIT 2
            """, timeout=min(2.0, remaining)), owner=owner))
            allowed = (self.clock() < self.deadline and len(users) == 1
                and users[0].get("id") == owner and state_allows_use(dict(users[0])))
            self.cache[owner] = bool(allowed)
            return bool(allowed)
        except Exception:
            # Unavailability/ambiguity is not "enabled". Never expose DB,
            # HTTP or provider exception bodies to clients/telemetry.
            self.unavailable = True
            return False
