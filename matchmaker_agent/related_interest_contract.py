"""Related-interest v1 policy, distinct from the frozen strict research policy."""
import hashlib
import json
import math
import os
from pathlib import Path

try:
    from .concept_identity import canonicalize_concept, PreferenceTextError
except ImportError:
    from concept_identity import canonicalize_concept, PreferenceTextError

POLICY = "related_interest_v1"
INDEX_NAME = "concept_embedding_v2_index"
DIMENSIONS = 768
RELATIONS = frozenset({"equivalent", "candidate_more_specific", "candidate_more_broad",
    "sibling_related", "role_mismatch", "constraint_conflict", "lexical_ambiguity",
    "unrelated", "unknown"})
ACCEPTED = frozenset({"equivalent", "candidate_more_specific", "sibling_related", "role_mismatch"})


def enabled():
    if os.getenv("MATCH_RELATED_INTEREST_ENABLED", "off").lower().strip() not in {"1", "true", "on"}:
        return False
    # Operator-only shared file: both services observe a kill immediately,
    # without a restart or exposing a public flag-editing endpoint.
    path = Path(os.getenv("MATCH_RELATED_INTEREST_KILL_SWITCH_FILE") or
                Path(__file__).resolve().parents[1] / ".related-interest-disabled")
    try:
        return not path.exists()
    except OSError:
        return False


def embedding_manifest(model):
    name = str(model or "").removeprefix("models/")
    if name not in {"gemini-embedding-2", "gemini-embedding-001", "text-embedding-004"}:
        raise ValueError("semantic_embedding_model_unsupported")
    return {"version": "concept_embedding_v2", "provider": "google",
        "model": name, "task": "semantic_similarity", "dimensions": DIMENSIONS,
        "prefix": "task: sentence similarity | query: " if name == "gemini-embedding-2" else "",
        "source": "complete_identity_v2_semantic_text", "normalization": "l2",
        "provider_revision": "unreported"}


def embedding_fingerprint(model):
    return hashlib.sha256(json.dumps(embedding_manifest(model), sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


def unit_vector(value):
    if not isinstance(value, list) or len(value) != DIMENSIONS:
        return None
    try:
        if any(isinstance(v, bool) for v in value):
            return None
        vector = [float(v) for v in value]
        if not all(math.isfinite(v) for v in vector):
            return None
        norm = math.sqrt(sum(v*v for v in vector))
        return [v/norm for v in vector] if norm > 0 and math.isfinite(norm) else None
    except (ValueError, TypeError, OverflowError):
        return None


def validated_evidence(item, *, query_key=None):
    """Validate one complete internal packet; labels never prove ownership."""
    if not isinstance(item, dict) or item.get("policy_version") != POLICY:
        return None
    if (item.get("basis_type") != "related_interest" or item.get("kind") != "semantic_related"
            or item.get("validator_status") != "accepted" or item.get("relation") not in ACCEPTED):
        return None
    try:
        query = canonicalize_concept(item.get("query_preference"))
        candidate = canonicalize_concept(item.get("candidate_preference"))
        score = item.get("semantic_score")
        if isinstance(score, bool):
            return None
        score = float(score)
        if (not query or not candidate or not math.isfinite(score) or not 0 <= score <= 1
                or query.key == candidate.key or candidate.key != item.get("concept_key")
                or query_key is not None and query.key != query_key):
            return None
    except (PreferenceTextError, TypeError, ValueError):
        return None
    return {"kind": "semantic_related", "basis_type": "related_interest", "policy_version": POLICY,
        "query_preference": query.semantic_text, "candidate_preference": candidate.semantic_text,
        "concept_key": candidate.key, "relation": item["relation"],
        "semantic_score": round(score, 4), "similarity": round(score, 4), "validator_status": "accepted"}


def bounded_counts(value):
    """Only numeric diagnostics, never messages, concept labels or identifiers."""
    result = {}
    for key in ("accepted", "rejected", "error", "attempts", *sorted(RELATIONS)):
        raw = value.get(key) if isinstance(value, dict) else 0
        result[key] = min(24, max(0, raw)) if type(raw) is int else 0
    return result
