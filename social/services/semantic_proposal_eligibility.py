"""Fresh final semantic proposal checks; exact matching never enters here."""
import time
from pymongo import timeout as mongo_timeout

from matchmaker_agent.semantic_rollout_policy import pair_route_allowed, rollout_mode
from services.match_state_service import has_verified_acceptance
from services.preference_semantic_service import recheck_semantic_proposal
from services.risk_block_service import risk_block_service
from services.profile_projection import without_expired_recent_context


def proposal_eligible(requester, candidate, evidence, search_context, *, profiles, matches,
                      profile_filter, qualify, target_stances, candidate_stances, deadline,
                      requester_prefers_query=False, clock=time.monotonic):
    if not pair_route_allowed(requester, candidate):
        return False
    if rollout_mode() != "enabled_accounts":
        return True
    deadline = min(deadline, clock()+5.0)
    try:
        if clock() >= deadline or risk_block_service.is_pair_blocked(requester, candidate, deadline=deadline):
            return False
        with mongo_timeout(max(.001, min(2.0, deadline-clock()))):
            owners = list(profiles.find({"user_id": requester}, {"_id": 0}).limit(2))
            if len(owners) != 1:
                return False
            owner = without_expired_recent_context(owners[0])
            people = list(profiles.find(profile_filter(owner, {requester}, candidate_ids=[candidate],
                require_active_context=False), {"_id": 0}).limit(2))
            if len(people) != 1 or people[0].get("user_id") != candidate:
                return False
            person = without_expired_recent_context(people[0])
            history = list(matches.find({"$or": [{"from_user": requester, "to_user": candidate},
                {"from_user": candidate, "to_user": requester}]}).limit(101))
            if len(history) > 100:
                return False
            for previous in history:
                state = previous.get("status")
                if state in {"draft", "pending"} or state == "accepted" and has_verified_acceptance(previous):
                    return False
                if state == "declined" and (time.time()-float(previous.get("created_at", 0)) < 30*86400
                        or int(previous.get("context_revision", 0)) == int(owner.get("current_context_revision", 0))):
                    return False
            # Profile restrictions are fresh. The 9001 proof below separately
            # rereads ALL bounded active polarity/conflict state, not these
            # earlier retrieval stance snapshots or fail-open memory caches.
            result = qualify(owner, person, target_stances=target_stances,
                candidate_stances=candidate_stances, vector_score=0,
                search_context=search_context, preference_evidence=evidence)
            if not result.get("eligible") or not result.get("semantic_related_preference_matched"):
                return False
        if clock() >= deadline:
            return False
        return recheck_semantic_proposal(requester, candidate,
            search_context.get("canonical_preference_key", ""), evidence,
            deadline=deadline, requester_prefers_query=requester_prefers_query)
    except Exception:
        return False
