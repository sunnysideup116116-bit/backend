"""Count-only pilot observations on the existing owner-bound job; no memory write."""
from matchmaker_agent.related_interest_contract import POLICY, bounded_counts


def record_search(job_id, owner_id, *, counts=None):
    if not job_id or not owner_id:
        return False
    try:
        from database import db
        from pymongo import timeout
        fields = {"related_interest_pilot.policy_version": POLICY,
                  "related_interest_pilot.triggered": True}
        if counts is not None:
            fields["related_interest_pilot.validator_counts"] = bounded_counts(counts)
        with timeout(0.5):
            result = db["match_search_jobs"].update_one(
                {"job_id": job_id, "user_id": owner_id}, {"$set": fields}, upsert=False)
        return bool(result.matched_count)
    except Exception:
        # Optional metrics must not undo a search/proposal or expose DB errors.
        return False


def summarize(jobs, proposals):
    result = {"fallback_trigger_jobs": 0, "validator": bounded_counts({}),
        "semantic_proposals": 0, "semantic_invitations": 0,
        "invitation_outcomes": {k: 0 for k in ("pending", "accepted", "declined", "expired", "other")}}
    for job in jobs:
        meta = job.get("related_interest_pilot") or {}
        if meta.get("policy_version") != POLICY:
            continue
        result["fallback_trigger_jobs"] += int(meta.get("triggered") is True)
        for key, count in bounded_counts(meta.get("validator_counts")).items():
            result["validator"][key] += count
    for proposal in proposals:
        if (proposal.get("related_interest_pilot") or {}).get("policy_version") != POLICY:
            continue
        result["semantic_proposals"] += 1
        # Distinguish a declined draft from an invitation actually sent.
        if not any(row.get("to") == "pending" for row in proposal.get("state_history", []) if isinstance(row, dict)):
            continue
        result["semantic_invitations"] += 1
        status = proposal.get("status")
        outcome = status if status in result["invitation_outcomes"] else "other"
        result["invitation_outcomes"][outcome] += 1
    return result
