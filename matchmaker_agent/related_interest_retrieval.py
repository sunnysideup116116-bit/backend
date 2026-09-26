"""Read-only versioned Concept ANN -> relation validation -> bounded owner expansion."""
import math
import time
from neo4j import Query
from matchmaker_agent.related_interest_canary import canary_cohort, canary_requester_enabled, canary_pair_enabled

try:
    from .concept_identity import stored_concept_identity
    from .related_interest_contract import POLICY, INDEX_NAME, DIMENSIONS, embedding_fingerprint, unit_vector, enabled
    from .related_interest_validator import validate_concepts
except ImportError:
    from concept_identity import stored_concept_identity
    from related_interest_contract import POLICY, INDEX_NAME, DIMENSIONS, embedding_fingerprint, unit_vector, enabled
    from related_interest_validator import validate_concepts


def index_metadata(session):
    row = session.run(Query("""
        SHOW VECTOR INDEXES YIELD name, state, populationPercent, options, labelsOrTypes, properties
        WHERE name = $index_name
        RETURN name, state, populationPercent, options, labelsOrTypes, properties
    """, timeout=3), index_name=INDEX_NAME).single()
    row = dict(row) if row else {}
    config = (row.get("options") or {}).get("indexConfig") or {}
    key_index = session.run(Query("""
        SHOW INDEXES YIELD state, type, labelsOrTypes, properties
        WHERE state='ONLINE' AND type='RANGE'
        RETURN sum(CASE WHEN labelsOrTypes=['Concept'] AND properties=['key'] THEN 1 ELSE 0 END) AS count,
          sum(CASE WHEN labelsOrTypes=['User'] AND properties=['id'] THEN 1 ELSE 0 END) AS user_count
    """, timeout=3)).single()
    key_ready = bool(key_index and int(key_index.get("count", 0) or 0) > 0)
    user_ready = bool(key_index and int(key_index.get("user_count", 0) or 0) > 0)
    return {"exists": bool(row), "state": row.get("state", "MISSING"),
        "dimension": config.get("vector.dimensions"), "similarity": config.get("vector.similarity_function"),
        "key_index_ready": key_ready,
        "user_id_index_ready": user_ready,
        "ready": bool(key_ready and user_ready and row.get("state") == "ONLINE" and row.get("labelsOrTypes") == ["Concept"]
            and row.get("properties") == ["embedding_v2"] and config.get("vector.dimensions") == DIMENSIONS
            and str(config.get("vector.similarity_function", "")).lower() == "cosine")}


def readiness(session, model):
    index = index_metadata(session)
    fingerprint = embedding_fingerprint(model)
    counts = session.run(Query("""
        MATCH (c:Concept) WHERE EXISTS { MATCH (c)<-[:PREFERS]-(:User) }
        RETURN count(c) AS total,
          sum(CASE WHEN c.canonicalization_version='v2' AND c.semantic_text IS NOT NULL
            AND c.semantic_input_hash IS NOT NULL AND c.fidelity_status='complete' THEN 1 ELSE 0 END) AS complete_v2,
          sum(CASE WHEN c.canonicalization_version='v2' AND c.embedding_v2 IS NOT NULL
            AND c.fidelity_status='complete'
            AND size(c.embedding_v2)=768 AND c.embedding_v2_fingerprint=$fingerprint
            AND c.embedding_v2_source_hash=c.semantic_input_hash
            AND all(x IN c.embedding_v2 WHERE x >= -1.0 AND x <= 1.0)
            AND abs(reduce(s=0.0, x IN c.embedding_v2 | s+x*x)-1.0) <= 0.00001
            THEN 1 ELSE 0 END) AS compatible
    """, timeout=3), fingerprint=fingerprint).single()
    counts = dict(counts) if counts else {}
    return {"index": index, "counts": {k: int(counts.get(k, 0) or 0) for k in ("total", "complete_v2", "compatible")},
        "embedding_fingerprint": fingerprint, "provider_revision": "unreported",
        "historical_embedding_fingerprint": "unknown", "runtime_enabled": enabled(),
        "activation_approved": False}


class _DeadlineSession:
    """Every Graph operation spends the same caller-owned request budget."""
    def __init__(self, session, deadline, clock):
        self.session, self.deadline, self.clock = session, deadline, clock

    def run(self, query, **params):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError("semantic_retrieval_timeout")
        result = self.session.run(Query(str(query), timeout=min(3.0, remaining)), **params)
        if self.clock() >= self.deadline:
            raise TimeoutError("semantic_retrieval_timeout")
        return result


def retrieve(session, req, identity, client, validator_model, embedding_model, *, clock=time.monotonic, deadline=None):
    """Do not use old embedding/index, labels, AVOIDS owners, or fuzzy fallback."""
    result = {"status": "error", "canonical_key": identity.key, "candidates": [],
        "retrieval_source": "graph_semantic", "policy_version": POLICY}
    if not canary_requester_enabled(req.requester_user_id):
        return {**result, "error_code": "semantic_policy_disabled"}
    allowed_owners = sorted(canary_cohort())
    deadline = min(deadline, clock()+27.0) if deadline is not None else clock()+min(27.0, req.request_budget_seconds)
    session = _DeadlineSession(session, deadline, clock)
    expected = embedding_fingerprint(embedding_model)
    if req.embedding_fingerprint != expected or embedding_fingerprint(req.embedding_model) != expected:
        return {**result, "error_code": "semantic_readiness_unconfirmed"}
    if not index_metadata(session)["ready"]:
        return {**result, "error_code": "semantic_index_unavailable"}
    query_vector = unit_vector(req.query_embedding)
    embedding_source = "request"
    if req.query_embedding is None:
        row = session.run(Query("""
            MATCH (c:Concept {key:$key}) RETURN c.key AS key, c.semantic_text AS semantic_text,
            c.canonicalization_version AS canonicalization_version, c.semantic_input_hash AS semantic_input_hash,
            c.fidelity_status AS fidelity_status,
            c.embedding_v2 AS vector, c.embedding_v2_fingerprint AS fingerprint,
            c.embedding_v2_source_hash AS source_hash
        """, timeout=3), key=identity.key).single()
        source = stored_concept_identity(dict(row)) if row else None
        if (source and source.key == identity.key and row.get("fingerprint") == expected
                and row.get("source_hash") == source.semantic_input_hash):
            query_vector = unit_vector(row.get("vector")); embedding_source = "concept_v2"
    if query_vector is None:
        if req.query_embedding is not None:
            return {**result, "error_code": "semantic_query_embedding_invalid"}
        return {**result, "status": "query_embedding_required"}
    if not canary_requester_enabled(req.requester_user_id):
        return {**result, "error_code": "semantic_policy_disabled"}
    hits = list(session.run(Query("""
        CALL db.index.vector.queryNodes($index_name, $neighbor_limit, $vector)
        YIELD node AS c, score
        WHERE score >= $min_similarity AND c.key <> $query_key
          AND c.canonicalization_version='v2' AND c.fidelity_status='complete' AND c.embedding_v2_fingerprint=$fingerprint
          AND c.embedding_v2_source_hash=c.semantic_input_hash
          AND EXISTS { MATCH (c)<-[:PREFERS]-(owner:User) WHERE owner.id IN $allowed_owner_ids }
        WITH c, score ORDER BY score DESC, c.key ASC LIMIT $concept_limit
        RETURN c.key AS key, c.semantic_text AS semantic_text,
          c.canonicalization_version AS canonicalization_version, c.semantic_input_hash AS semantic_input_hash,
          c.fidelity_status AS fidelity_status,
          c.embedding_v2 AS vector, c.embedding_v2_fingerprint AS fingerprint,
          c.embedding_v2_source_hash AS source_hash, score AS similarity
    """, timeout=3), index_name=INDEX_NAME, neighbor_limit=req.neighbor_limit,
        vector=query_vector, min_similarity=req.min_similarity, query_key=identity.key,
        fingerprint=expected, concept_limit=req.concept_limit, allowed_owner_ids=allowed_owners))
    concepts = []
    seen = set()
    for row in hits[:req.concept_limit]:
        source = stored_concept_identity(dict(row))
        score = row.get("similarity")
        if (not source or source.key in seen or source.key == identity.key
                or row.get("fingerprint") != expected or row.get("source_hash") != source.semantic_input_hash
                or unit_vector(row.get("vector")) is None
                or not isinstance(score, (float, int)) or not math.isfinite(score)
                or not req.min_similarity <= score <= 1):
            continue
        seen.add(source.key)
        concepts.append({"concept_key": source.key, "semantic_text": source.semantic_text,
            "semantic_input_hash": source.semantic_input_hash, "similarity": float(score)})
    # The only LLM boundary occurs BEFORE any Concept -> User expansion.
    if not canary_requester_enabled(req.requester_user_id):
        return {**result, "error_code": "semantic_policy_disabled"}
    accepted, counts = validate_concepts(identity.semantic_text, concepts, client, validator_model,
        deadline=min(deadline, clock()+18.0), clock=clock,
        is_enabled=lambda: canary_requester_enabled(req.requester_user_id))
    result["validator_counts"] = counts
    if not canary_requester_enabled(req.requester_user_id):
        return {**result, "error_code": "semantic_policy_disabled"}
    if not accepted:
        if counts["error"]:
            return {**result, "error_code": "semantic_validator_unavailable"}
        return {**result, "status": "success", "semantic_concepts_considered": [], "candidate_count": 0}
    evidence = {c["concept_key"]: {
        "kind": "semantic_related", "basis_type": "related_interest", "policy_version": POLICY,
        "query_preference": identity.semantic_text, "candidate_preference": c["semantic_text"],
        "concept_key": c["concept_key"], "relation": c["relation"],
        "semantic_score": round(c["similarity"], 4), "similarity": round(c["similarity"], 4),
        "validator_status": "accepted"} for c in accepted}
    rows = session.run(Query("""
        UNWIND $concepts AS hit
        MATCH (c:Concept {key:hit.concept_key})
        WHERE c.canonicalization_version='v2' AND c.fidelity_status='complete' AND c.semantic_input_hash=hit.semantic_input_hash
          AND c.embedding_v2_fingerprint=$fingerprint AND c.embedding_v2_source_hash=c.semantic_input_hash
        WITH c, hit.similarity AS score
        CALL {
          WITH c UNWIND $allowed_owner_ids AS allowed_owner_id
          MATCH (candidate:User {id:allowed_owner_id})-[:PREFERS]->(c)
          WITH candidate LIMIT $per_concept_limit
          WITH candidate WHERE candidate.id <> $requester_user_id AND NOT candidate.id IN $excluded_user_ids
          RETURN candidate
        }
        WITH candidate, c, score ORDER BY candidate.id, score DESC, c.key
        WITH candidate, collect({concept_key:c.key, similarity:score})[0..$evidence_limit] AS evidence
        WITH candidate, evidence, evidence[0].similarity AS best_score
        ORDER BY best_score DESC, candidate.id LIMIT $candidate_limit
        RETURN candidate.id AS candidate_id, evidence
    """, timeout=3), concepts=accepted, fingerprint=expected,
        requester_user_id=req.requester_user_id, excluded_user_ids=req.excluded_user_ids,
        allowed_owner_ids=sorted(canary_cohort()),
        per_concept_limit=req.per_concept_limit, evidence_limit=req.evidence_limit, candidate_limit=req.candidate_limit)
    by_user = {}
    for row in rows:
        if clock() >= deadline:
            raise TimeoutError("semantic_retrieval_timeout")
        uid = row.get("candidate_id")
        if not canary_pair_enabled(req.requester_user_id, uid) or uid in req.excluded_user_ids:
            continue
        found = by_user.setdefault(uid, {})
        for item in list(row.get("evidence") or [])[:req.evidence_limit]:
            key = item.get("concept_key")
            if key in evidence:
                found[key] = evidence[key]
    candidates = [{"candidate_id": uid, "evidence": sorted(items.values(),
        key=lambda e: (-e["similarity"], e["concept_key"]))[:req.evidence_limit]} for uid, items in by_user.items() if items]
    candidates.sort(key=lambda c: (-c["evidence"][0]["similarity"], c["candidate_id"]))
    return {**result, "status": "success", "candidates": candidates[:req.candidate_limit],
        "candidate_count": min(len(candidates), req.candidate_limit), "embedding_source": embedding_source,
        "semantic_concepts_considered": [{"concept_key": c["concept_key"], "similarity": round(c["similarity"], 4)} for c in accepted]}
