"""Current source/vector/owner proof. Never use historical embeddings or aliases."""
import math
import re

from neo4j import Query
from matchmaker_agent.concept_identity import stored_concept_identity, verified_legacy_identity
from matchmaker_agent.related_interest_contract import validated_evidence


def verified_vector(record, fingerprint, *, require_provenance=True):
    identity = stored_concept_identity(record)
    if not identity or identity.canonicalization_version != "v2":
        return None
    vector = record.get("vector", record.get("embedding_v2"))
    fp = record.get("fingerprint", record.get("embedding_v2_fingerprint"))
    source = record.get("source_hash", record.get("embedding_v2_source_hash"))
    provenance = record.get("provenance", record.get("embedding_v2_provenance_fingerprint"))
    if fp != fingerprint or source != identity.semantic_input_hash:
        return None
    if require_provenance and (not isinstance(provenance, str) or re.fullmatch(r"[0-9a-f]{64}", provenance) is None):
        return None
    if not isinstance(vector, list) or len(vector) != 768:
        return None
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in vector):
        return None
    norm2 = sum(v*v for v in vector)
    if not math.isfinite(norm2) or abs(norm2 - 1.0) > 1e-5:
        return None
    return identity


def recheck_owner_evidence(session, candidate, evidence, query_key, fingerprint):
    """Bounded indexed reads of current active PREFERS, not a retrieval receipt."""
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 5:
        return False
    packets = [validated_evidence(e, query_key=query_key) for e in evidence]
    if not all(packets) or len({p['concept_key'] for p in packets}) != len(packets):
        return False
    for packet in packets:
        rows = list(session.run(Query("""
            MATCH (c:Concept {key:$key})
            OPTIONAL MATCH (u:User {id:$owner})-[r:PREFERS|AVOIDS]->(c)
            WHERE coalesce(r.active,true)=true
            RETURN c.key AS key,c.semantic_text AS semantic_text,
              c.canonicalization_version AS canonicalization_version,
              c.semantic_input_hash AS semantic_input_hash,c.fidelity_status AS fidelity_status,
              c.embedding_v2 AS vector,c.embedding_v2_fingerprint AS fingerprint,
              c.embedding_v2_source_hash AS source_hash,c.embedding_v2_provenance_fingerprint AS provenance,
              type(r) AS polarity LIMIT 3
        """, timeout=2), key=packet['concept_key'], owner=candidate))
        # Missing, duplicate, mixed polarity, or removed ownership is unknown,
        # not proof. Shared Concept nodes do not convey another owner's edge.
        if len(rows) != 1 or rows[0].get('polarity') != 'PREFERS':
            return False
        identity = verified_vector(dict(rows[0]), fingerprint)
        if not identity or identity.semantic_text != packet['candidate_preference']:
            return False
    return True


def pair_preferences_safe(session, requester, candidate, *, query_key, requester_prefers_query=False):
    """Fresh bounded conflict view; preserve indeterminate-legacy fail-closed policy."""
    views = []
    for owner in (requester, candidate):
        rows = list(session.run(Query("""
            MATCH (u:User {id:$owner})-[r:PREFERS|AVOIDS]->(c:Concept)
            WHERE coalesce(r.active,true)=true
            RETURN type(r) AS polarity,c.key AS key,c.semantic_text AS semantic_text,
              c.canonicalization_version AS canonicalization_version,c.semantic_input_hash AS semantic_input_hash,
              c.fidelity_status AS fidelity_status,r.legacy_evidence_scope AS legacy_evidence_scope,
              r.legacy_fidelity_status AS legacy_fidelity_status,r.legacy_semantic_text AS legacy_semantic_text,
              r.legacy_semantic_input_hash AS legacy_semantic_input_hash LIMIT 101
        """, timeout=2), owner=owner))
        if len(rows) > 100:
            return False
        known = {"PREFERS": set(), "AVOIDS": set()}
        raw = {"PREFERS": set(), "AVOIDS": set()}
        unknown = {"PREFERS": False, "AVOIDS": False}
        for row in rows:
            polarity = row.get("polarity")
            if polarity not in known:
                return False
            identity = stored_concept_identity(dict(row)) or verified_legacy_identity(dict(row))
            if identity:
                known[polarity].add(identity.key)
            else:
                unknown[polarity] = True
            if row.get("key"):
                raw[polarity].add(row["key"])
        if known["PREFERS"] & known["AVOIDS"] or raw["PREFERS"] & raw["AVOIDS"]:
            return False
        views.append((known, raw, unknown))
    for left, right in (views, views[::-1]):
        known, raw, unknown = left
        other, other_raw, _ = right
        if known["AVOIDS"] & other["PREFERS"] or raw["AVOIDS"] & other_raw["PREFERS"]:
            return False
        if unknown["AVOIDS"] and other["PREFERS"] or unknown["PREFERS"] and other["AVOIDS"]:
            return False
    return not requester_prefers_query or query_key in views[0][0]["PREFERS"]
