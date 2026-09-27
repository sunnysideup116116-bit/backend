"""Pure readiness classification; neither Graph labels nor membership imply consent."""
from collections import Counter

from matchmaker_agent.concept_identity import stored_concept_identity
from matchmaker_agent.semantic_evidence_readiness import verified_vector
from matchmaker_agent.semantic_rollout_policy import valid_owner_id


def classify_owner(account, profiles, users, snapshot, vectors, fingerprint, *, preview_check):
    from services.semantic_user_eligibility import state_allows_use
    owner = account.get("$id") if isinstance(account, dict) else None
    result = {"user_id": owner if valid_owner_id(owner) else "", "category": "BLOCKED",
        "requester_eligible": False, "semantic_ready_candidate": False, "v2_clean": False,
        "prefers": 0, "avoids": 0, "unresolved_legacy": 0, "requires_owner_confirmation": False,
        "reason_codes": []}
    if not valid_owner_id(owner) or account.get("status") is not True:
        return {**result, "reason_codes": ["account_disabled_or_invalid"]}
    if len(profiles) != 1 or profiles[0].get("user_id") != owner or not profiles[0].get("_id"):
        return {**result, "reason_codes": ["profile_missing_or_ambiguous"]}
    if len(users) != 1 or users[0].get("id") != owner:
        return {**result, "reason_codes": ["graph_identity_missing_or_ambiguous"]}
    if not state_allows_use(profiles[0]) or not state_allows_use(users[0]):
        return {**result, "reason_codes": ["identity_disabled_or_projection_pending"]}
    result["requester_eligible"] = True
    if not isinstance(snapshot, dict) or snapshot.get("owner") != owner:
        return {**result, "reason_codes": ["preference_snapshot_unavailable"]}
    rows = [r for r in snapshot.get("rows", []) if r.get("relation") in {"PREFERS", "AVOIDS"}
        and r.get("properties", {}).get("active") is not False]
    result.update(prefers=sum(r['relation'] == 'PREFERS' for r in rows), avoids=sum(r['relation'] == 'AVOIDS' for r in rows))
    if not rows:
        return {**result, "category": "EMPTY"}
    identities = [stored_concept_identity(r.get("concept", {})) for r in rows]
    result['unresolved_legacy'] = sum(i is None for i in identities)
    result['requires_owner_confirmation'] = result['unresolved_legacy'] > 0
    result['v2_clean'] = all(i is not None and i.canonicalization_version == 'v2' for i in identities)
    ready = []
    for row, identity in zip(rows, identities):
        key = identity.key if identity else None
        records = vectors.get(key, [])
        ready.append(bool(identity and len(records) == 1 and verified_vector(records[0], fingerprint)
            and records[0].get('semantic_input_hash') == identity.semantic_input_hash))
    result['semantic_ready_candidate'] = any(ok and r['relation'] == 'PREFERS' for r, ok in zip(rows, ready))
    # A duplicate/mixed-polarity association cannot silently collapse via a dict.
    keys = [r.get('concept', {}).get('key') for r in rows]
    if any(not isinstance(k, str) or not k for k in keys) or len(set(keys)) != len(keys):
        return {**result, 'v2_clean': False, 'semantic_ready_candidate': False, 'reason_codes': ['duplicate_or_conflicting_identity']}
    if result['v2_clean']:
        return {**result, 'category': 'READY' if all(ready) else 'BLOCKED',
            'reason_codes': [] if all(ready) else ['embedding_v2_missing_or_incompatible']}
    try:
        code = preview_check(snapshot)
    except Exception:
        code = 'preview_unavailable'
    if code:
        return {**result, 'reason_codes': [code]}
    return {**result, 'category': 'MIGRATABLE', 'requires_owner_confirmation': True}


def summarize_readiness(rows):
    counts = Counter(r['category'] for r in rows)
    return {'categories': {k: counts[k] for k in ('READY', 'MIGRATABLE', 'EMPTY', 'BLOCKED')},
        'enabled_semantic_requesters': sum(r['requester_eligible'] for r in rows),
        'semantic_ready_candidate_owners': sum(r['semantic_ready_candidate'] for r in rows),
        'v2_clean_users': sum(r['v2_clean'] for r in rows),
        'needs_owner_confirmation': sum(r['requires_owner_confirmation'] for r in rows)}
