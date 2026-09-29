"""Bounded internal pilot observations; no raw labels, messages or memory writes."""
import math
import statistics
import time
from matchmaker_agent.related_interest_contract import POLICY, bounded_counts, bounded_ann_observations


def record_shadow(job_id, owner_id, *, status, counts=None, ann_observations=None,
                  qualified_count=0, error_code=None, elapsed_seconds=0):
    """Only an existing job's aggregate observation; never its visible outcome."""
    if not job_id or not owner_id or status not in {"running", "success", "failed"}:
        return False
    try:
        from database import db
        from pymongo import timeout
        from services.preference_semantic_service import SEMANTIC_ERROR_CODES
        meta = {"policy_version": POLICY, "telemetry_version": 3, "status": status,
            "triggered": True, "rollout_mode": "enabled_accounts", "observation_only": True,
            "validator_counts": bounded_counts(counts),
            "ann_observations": bounded_ann_observations(ann_observations),
            "qualified_count": max(0, min(50, int(qualified_count))),
            "error_code": error_code if error_code in SEMANTIC_ERROR_CODES else None,
            "elapsed_seconds": round(max(0, min(300, float(elapsed_seconds))), 3)}
        if status != "running":
            meta["completed_at"] = time.time()
        with timeout(.5):
            result = db["match_search_jobs"].update_one(
                {"job_id": job_id, "user_id": owner_id}, {"$set": {"semantic_shadow": meta}}, upsert=False)
        return bool(result.matched_count)
    except Exception:
        return False


def record_search(job_id, owner_id, *, counts=None, ann_observations=None, triggered=True, requester_verified=None):
    if not job_id or not owner_id:
        return False
    try:
        from database import db
        from pymongo import timeout
        from matchmaker_agent.semantic_rollout_policy import rollout_mode
        mode = rollout_mode()
        fields = {"related_interest_pilot.policy_version": POLICY,
                  "related_interest_pilot.telemetry_version": 3 if mode == "enabled_accounts" else 2,
                  "related_interest_pilot.rollout_mode": mode}
        if requester_verified is not None:
            fields['related_interest_pilot.requester_eligibility_verified'] = requester_verified is True
        if triggered:
            fields["related_interest_pilot.triggered"] = True
        if counts is not None:
            fields["related_interest_pilot.validator_counts"] = bounded_counts(counts)
        if ann_observations is not None:
            fields["related_interest_pilot.ann_observations"] = bounded_ann_observations(ann_observations)
        with timeout(0.5):
            result = db["match_search_jobs"].update_one(
                {"job_id": job_id, "user_id": owner_id}, {"$set": fields}, upsert=False)
        return bool(result.matched_count)
    except Exception:
        # Optional metrics must not undo a search/proposal or expose DB errors.
        return False


def summarize(jobs, proposals, openings=()):
    result = {"fallback_trigger_jobs": 0, "validator": bounded_counts({}),
        "semantic_proposals": 0, "semantic_invitations": 0,
        "confirmation_outcomes": {k: 0 for k in ("awaiting_confirmation", "confirmed", "declined_before_invitation", "other")},
        "invitation_outcomes": {k: 0 for k in ("pending", "accepted", "declined", "expired", "other")}}
    jobs = list(jobs)
    shadow = [j["semantic_shadow"] for j in jobs if (j.get("semantic_shadow") or {}).get("policy_version") == POLICY]
    result["shadow"] = {"jobs": len(shadow), "terminal": sum(s.get("status") in {"success", "failed"} for s in shadow),
        "typed_unavailable": sum(s.get("error_code") in {"semantic_validator_unavailable", "semantic_retrieval_timeout"} for s in shadow),
        "validator": {k: sum(bounded_counts(s.get("validator_counts"))[k] for s in shadow) for k in bounded_counts({})}}
    proposals = list(proposals)
    valid_jobs = []
    for job in jobs:
        meta = job.get("related_interest_pilot") or {}
        if meta.get("policy_version") != POLICY:
            continue
        valid_jobs.append(job)
        result["fallback_trigger_jobs"] += int(meta.get("triggered") is True)
        for key, count in bounded_counts(meta.get("validator_counts")).items():
            result["validator"][key] += count
    for proposal in proposals:
        if (proposal.get("related_interest_pilot") or {}).get("policy_version") != POLICY:
            continue
        result["semantic_proposals"] += 1
        # Distinguish a declined draft from an invitation actually sent.
        if not any(row.get("to") == "pending" for row in proposal.get("state_history", []) if isinstance(row, dict)):
            outcome = {"draft": "awaiting_confirmation", "declined": "declined_before_invitation"}.get(proposal.get("status"), "other")
            result["confirmation_outcomes"][outcome] += 1
            continue
        result["confirmation_outcomes"]["confirmed"] += 1
        result["semantic_invitations"] += 1
        status = proposal.get("status")
        outcome = status if status in result["invitation_outcomes"] else "other"
        result["invitation_outcomes"][outcome] += 1
    triggered = [j for j in valid_jobs if (j.get("related_interest_pilot") or {}).get("triggered") is True]
    unavailable = sum(j.get("error_code") == "semantic_validator_unavailable" for j in triggered)
    result.update(preference_search_jobs=len(valid_jobs),
        exact_hit_jobs=sum(type(n := (j.get("retrieval_diagnostics") or {}).get("qualified_exact_count")) is int
            and n > 0 for j in valid_jobs),
        exact_only_jobs=sum(not (j.get("related_interest_pilot") or {}).get("triggered") for j in valid_jobs),
        validator_unavailable_jobs=unavailable,
        validator_unavailable_rate=round(unavailable/len(triggered), 4) if triggered else None)
    attempts = result["validator"]["attempts"]
    concepts = sum(result["validator"][k] for k in ("accepted", "rejected", "error"))
    detailed = all((j.get("related_interest_pilot") or {}).get("telemetry_version") in {2, 3} for j in triggered)
    result.update(retry_metrics_complete=detailed,
        validator_retry_rate=round(result["validator"]["retries"]/attempts, 4) if attempts and detailed else None,
        validator_attempt_error_rate=round(result["validator"]["attempt_errors"]/attempts, 4) if attempts and detailed else None,
        validator_concept_error_rate=round(result["validator"]["error"]/concepts, 4) if concepts else None)
    observations = [hit for j in valid_jobs for hit in bounded_ann_observations(
        (j.get("related_interest_pilot") or {}).get("ann_observations"))]
    scores = [hit["similarity"] for hit in observations]
    result["ann"] = {"hits": len(scores), "unique_concepts": len({h["concept_key"] for h in observations}),
        "score_min": min(scores) if scores else None, "score_median": statistics.median(scores) if scores else None,
        "score_max": max(scores) if scores else None}
    durations = []
    sources = {'job_creation_to_completion': 0, 'legacy_processing_only': 0, 'missing_or_invalid': 0}
    for job in valid_jobs:
        # Current jobs persist created_at, not started_at. Include queue time;
        # retain old processing-only samples with an explicit source count.
        start = job.get("created_at") if type(job.get("created_at")) in (int, float) else job.get("started_at")
        end = job.get("completed_at")
        if (type(start) in (int, float) and type(end) in (int, float)
                and math.isfinite(start) and math.isfinite(end) and 0 <= end-start <= 31*86400):
            durations.append(end-start)
            sources['job_creation_to_completion' if type(job.get('created_at')) in (int, float) else 'legacy_processing_only'] += 1
        else:
            sources['missing_or_invalid'] += 1
    durations.sort()
    result["latency_seconds"] = {"samples": len(durations),
        "p50": round(statistics.median(durations), 3) if durations else None,
        "p95": round(durations[math.ceil(len(durations)*.95)-1], 3) if durations else None}
    result['latency_source_counts'] = sources
    modes = [entry.get("reason_render_mode") for p in proposals
        if (p.get("related_interest_pilot") or {}).get("policy_version") == POLICY
        for entry in (p.get("friend_intro_v4") or {}).values() if isinstance(entry, dict)]
    known = [m for m in modes if m in {"fact_bound", "neutral_fallback"}]
    result["reason_rendering"] = {"known": len(known), "unknown": len(modes)-len(known),
        "neutral_fallback": known.count("neutral_fallback"),
        "fallback_rate": round(known.count("neutral_fallback")/len(known), 4) if known else None}
    outcomes = [(m.get("metadata") or {}).get("opening_outcome") for m in openings]
    known_openings = [m for m in outcomes if m in {"related_interest_fact_bound", "related_interest_neutral_fallback"}]
    result["opening_rendering"] = {"known": len(known_openings),
        "neutral_fallback": known_openings.count("related_interest_neutral_fallback"),
        "fallback_rate": round(known_openings.count("related_interest_neutral_fallback")/len(known_openings), 4) if known_openings else None}
    return result
