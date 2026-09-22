"""Bounded semantic fallback for explicit durable-preference searches.

This module is read-only with respect to Neo4j.  It never writes Concept
identity, aliases, preference edges, profile embeddings, or durable memory.
"""

from __future__ import annotations

import math
import os
import re
import time
from typing import Any

import requests
from urllib3.util import Timeout

from config import GOOGLE_EMBEDDING_MODEL
from matchmaker_agent.concept_identity import canonicalize_concept
from services.ai_service import get_embeddings


AGENT_SEMANTIC_URL = "http://127.0.0.1:9001/api/preferences/semantic-candidates"
AGENT_READINESS_URL = "http://127.0.0.1:9001/api/preferences/semantic-readiness"
SEMANTIC_DIMENSIONS = 768

_HARD_NEIGHBOR_LIMIT = 32
_HARD_CONCEPT_LIMIT = 12
_HARD_PER_CONCEPT_LIMIT = 20
_HARD_CANDIDATE_LIMIT = 50
_HARD_EVIDENCE_LIMIT = 5
_HARD_TRIGGER_THRESHOLD = 5
_HARD_TIMEOUT_SECONDS = 5.0
_HARD_TOTAL_TIMEOUT_SECONDS = 10.0
SEMANTIC_ERROR_CODES = frozenset({
    "semantic_graph_unavailable", "semantic_graph_invalid_response",
    "semantic_readiness_unconfirmed", "semantic_index_unavailable",
    "semantic_query_embedding_unavailable", "semantic_query_embedding_invalid",
    "semantic_retrieval_timeout",
})


class PreferenceSemanticRetrievalError(RuntimeError):
    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and code in SEMANTIC_ERROR_CODES else "semantic_graph_invalid_response"
        super().__init__(self.code)


def _bounded_int(name: str, default: int, hard_max: int) -> int:
    try:
        value = int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, hard_max))


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(minimum, min(value, maximum))


def preference_semantic_mode() -> str:
    mode = os.getenv("MATCH_PREFERENCE_SEMANTIC_MODE", "off").strip().lower()
    return mode if mode in {"off", "shadow", "active"} else "off"


def semantic_embedding_space_confirmed() -> bool:
    return os.getenv(
        "MATCH_PREFERENCE_SEMANTIC_EMBEDDING_SPACE_CONFIRMED", "off",
    ).strip().lower() in {"1", "true", "on"}


def qualified_exact_trigger_threshold() -> int:
    """Canary default: semantic fallback only when qualified exact count is zero."""
    return _bounded_int(
        "MATCH_PREFERENCE_SEMANTIC_QUALIFIED_EXACT_THRESHOLD", 1,
        _HARD_TRIGGER_THRESHOLD,
    )


def semantic_config() -> dict[str, Any]:
    return {
        "mode": preference_semantic_mode(),
        "embedding_space_confirmed": semantic_embedding_space_confirmed(),
        "qualified_exact_threshold": qualified_exact_trigger_threshold(),
        "neighbor_limit": _bounded_int(
            "MATCH_PREFERENCE_SEMANTIC_NEIGHBOR_LIMIT", 24, _HARD_NEIGHBOR_LIMIT,
        ),
        "concept_limit": _bounded_int(
            "MATCH_PREFERENCE_SEMANTIC_CONCEPT_LIMIT", 8, _HARD_CONCEPT_LIMIT,
        ),
        "per_concept_limit": _bounded_int(
            "MATCH_PREFERENCE_SEMANTIC_PER_CONCEPT_LIMIT", 10,
            _HARD_PER_CONCEPT_LIMIT,
        ),
        "candidate_limit": _bounded_int(
            "MATCH_PREFERENCE_SEMANTIC_CANDIDATE_LIMIT", 40,
            _HARD_CANDIDATE_LIMIT,
        ),
        "evidence_limit": _bounded_int(
            "MATCH_PREFERENCE_SEMANTIC_EVIDENCE_LIMIT", 3, _HARD_EVIDENCE_LIMIT,
        ),
        "min_similarity": _bounded_float(
            "MATCH_PREFERENCE_SEMANTIC_MIN_SIMILARITY", 0.82, 0.75, 1.0,
        ),
        "timeout_seconds": _bounded_float(
            "MATCH_PREFERENCE_SEMANTIC_TIMEOUT_SECONDS", 3.0, 0.25,
            _HARD_TIMEOUT_SECONDS,
        ),
        "total_timeout_seconds": _bounded_float(
            "MATCH_PREFERENCE_SEMANTIC_TOTAL_TIMEOUT_SECONDS", 6.0, 1.0,
            _HARD_TOTAL_TIMEOUT_SECONDS,
        ),
    }


def _unit_embedding(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != SEMANTIC_DIMENSIONS:
        return None
    try:
        vector = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in vector):
        return None
    magnitude = math.sqrt(sum(item * item for item in vector))
    if not math.isfinite(magnitude) or not magnitude:
        return None
    return [item / magnitude for item in vector]


def preference_semantic_readiness() -> dict[str, Any]:
    """Combine the read-only Graph audit with local provider configuration."""
    config = semantic_config()
    try:
        response = requests.get(
            AGENT_READINESS_URL,
            timeout=(1.0, config["timeout_seconds"]),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        return {
            "status": "error",
            "error_code": "semantic_readiness_unavailable",
            "error_category": type(exc).__name__,
            "configured_model": GOOGLE_EMBEDDING_MODEL,
            "configured_task": "semantic_similarity",
            "historical_embedding_fingerprint": "unknown",
            "active_enable_ready": False,
        }
    if not isinstance(payload, dict):
        payload = {}
    retrieval_ready = bool(payload.get("retrieval_ready"))
    historical = str(
        payload.get("historical_embedding_fingerprint") or "unknown"
    )[:40]
    return {
        "status": "success" if payload.get("status") == "success" else "error",
        "index": {
            key: payload.get("index", {}).get(key)
            for key in ("exists", "state", "dimension", "population_percent", "schema_valid")
        } if isinstance(payload.get("index"), dict) else {},
        "embedding_coverage": {
            key: payload.get("embedding_coverage", {}).get(key)
            for key in ("preference_concept_count", "embedded_preference_concept_count", "coverage_ratio")
        } if isinstance(payload.get("embedding_coverage"), dict) else {},
        "retrieval_ready": retrieval_ready,
        "configured_model": GOOGLE_EMBEDDING_MODEL,
        "configured_task": "semantic_similarity",
        "configured_dimensions": SEMANTIC_DIMENSIONS,
        "operator_embedding_space_confirmation": bool(
            config["embedding_space_confirmed"]
        ),
        "historical_embedding_fingerprint": historical,
        "active_enable_ready": bool(
            payload.get("status") == "success"
            and payload.get("active_enable_ready") is True
            and historical == "verified"
            and retrieval_ready and config["embedding_space_confirmed"]
        ),
    }


def _post_semantic(payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    try:
        response = requests.post(
            AGENT_SEMANTIC_URL,
            json=payload,
            timeout=Timeout(total=timeout_seconds, connect=min(1.0, timeout_seconds), read=timeout_seconds),
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise PreferenceSemanticRetrievalError(
            "semantic_graph_unavailable",
        ) from exc
    if not isinstance(result, dict):
        raise PreferenceSemanticRetrievalError("semantic_graph_invalid_response")
    return result


def retrieve_semantic_preference_candidates(
    requester_user_id: str,
    topic: str,
    *,
    excluded_user_ids: set[str] | list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Return deterministic, deduplicated candidates from bounded Concept ANN."""
    identity = canonicalize_concept(topic)
    if not identity:
        return {
            "candidate_ids": [], "evidence_by_candidate": {},
            "canonical_key": "", "semantic_concepts_considered": [],
            "retrieval_source": "graph_semantic",
        }
    config = semantic_config()
    deadline = time.monotonic() + config["total_timeout_seconds"]

    def remaining_timeout() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0.25:
            raise PreferenceSemanticRetrievalError("semantic_retrieval_timeout")
        return min(config["timeout_seconds"], remaining)

    excluded = sorted({
        str(value).strip()[:128]
        for value in excluded_user_ids
        if str(value or "").strip()
    })[:200]
    payload = {
        "requester_user_id": str(requester_user_id)[:128],
        "topic": identity.label,
        **{field: identity.as_dict()[field] for field in (
            "canonical_key", "canonicalization_version", "semantic_text", "semantic_input_hash",
        )},
        "embedding_model": GOOGLE_EMBEDDING_MODEL,
        "excluded_user_ids": excluded,
        "neighbor_limit": config["neighbor_limit"],
        "concept_limit": config["concept_limit"],
        "per_concept_limit": config["per_concept_limit"],
        "candidate_limit": config["candidate_limit"],
        "evidence_limit": config["evidence_limit"],
        "min_similarity": config["min_similarity"],
    }
    result = _post_semantic(payload, remaining_timeout())
    remaining_timeout()
    if result.get("canonical_key") != identity.key:
        raise PreferenceSemanticRetrievalError("semantic_graph_invalid_response")
    if result.get("status") == "query_embedding_required":
        try:
            vectors = get_embeddings(
                [identity.label],
                task_type="semantic_similarity",
                output_dimensionality=SEMANTIC_DIMENSIONS,
                request_timeout_seconds=remaining_timeout(),
            )
        except PreferenceSemanticRetrievalError:
            raise
        except Exception as exc:
            raise PreferenceSemanticRetrievalError(
                "semantic_query_embedding_unavailable",
            ) from exc
        query_embedding = _unit_embedding(vectors[0] if vectors else None)
        if query_embedding is None:
            raise PreferenceSemanticRetrievalError(
                "semantic_query_embedding_invalid",
            )
        result = _post_semantic(
            {**payload, "query_embedding": query_embedding},
            remaining_timeout(),
        )
        remaining_timeout()
    if result.get("status") != "success":
        raise PreferenceSemanticRetrievalError(str(
            result.get("error_code") or "semantic_graph_invalid_response"
        ))
    if result.get("canonical_key") != identity.key:
        raise PreferenceSemanticRetrievalError("semantic_graph_invalid_response")
    if not isinstance(result.get("candidates"), list):
        raise PreferenceSemanticRetrievalError("semantic_graph_invalid_response")

    by_candidate: dict[str, list[dict[str, Any]]] = {}
    for row in list(result.get("candidates") or [])[:config["candidate_limit"]]:
        if not isinstance(row, dict):
            continue
        candidate_id = str(row.get("candidate_id") or "").strip()[:128]
        if (
            not candidate_id or candidate_id == requester_user_id
            or candidate_id in excluded
        ):
            continue
        evidence = by_candidate.setdefault(candidate_id, [])
        for item in list(row.get("evidence") or [])[:config["evidence_limit"]]:
            if not isinstance(item, dict):
                continue
            concept_key = str(item.get("concept_key") or "").strip()[:100]
            try:
                similarity = float(item.get("similarity", 0.0))
            except (TypeError, ValueError):
                continue
            if (
                re.fullmatch(r"[a-z][a-z0-9_]{1,50}", concept_key)
                and concept_key != identity.key
                and math.isfinite(similarity)
                and config["min_similarity"] <= similarity <= 1.0
            ):
                evidence.append({
                    "kind": "semantic_related",
                    "concept_key": concept_key,
                    "similarity": round(similarity, 4),
                })

    best_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for candidate_id, items in by_candidate.items():
        best_by_key: dict[str, dict[str, Any]] = {}
        for item in items:
            current = best_by_key.get(item["concept_key"])
            if current is None or item["similarity"] > current["similarity"]:
                best_by_key[item["concept_key"]] = item
        bounded = sorted(
            best_by_key.values(),
            key=lambda item: (-item["similarity"], item["concept_key"]),
        )[:config["evidence_limit"]]
        if bounded:
            best_by_candidate[candidate_id] = bounded

    candidate_ids = sorted(
        best_by_candidate,
        key=lambda candidate_id: (
            -best_by_candidate[candidate_id][0]["similarity"], candidate_id,
        ),
    )[:config["candidate_limit"]]
    evidence_by_candidate = {
        candidate_id: best_by_candidate[candidate_id]
        for candidate_id in candidate_ids
    }

    considered: dict[str, float] = {}
    for item in list(result.get("semantic_concepts_considered") or [
        evidence
        for values in evidence_by_candidate.values()
        for evidence in values
    ])[:config["neighbor_limit"]]:
        if not isinstance(item, dict):
            continue
        concept_key = str(item.get("concept_key") or "").strip()[:100]
        try:
            similarity = float(item.get("similarity", 0.0))
        except (TypeError, ValueError):
            continue
        if (re.fullmatch(r"[a-z][a-z0-9_]{1,50}", concept_key)
                and concept_key != identity.key and math.isfinite(similarity)
                and config["min_similarity"] <= similarity <= 1.0):
            considered[concept_key] = max(considered.get(concept_key, 0.0), round(similarity, 4))
    concepts = [
        {"concept_key": key, "similarity": score}
        for key, score in sorted(
            considered.items(), key=lambda item: (-item[1], item[0]),
        )
    ][:config["concept_limit"]]
    return {
        "candidate_ids": candidate_ids,
        "evidence_by_candidate": evidence_by_candidate,
        "canonical_key": identity.key,
        "normalized_topic": identity.label,
        "semantic_concepts_considered": concepts,
        "candidate_count": len(candidate_ids),
        "retrieval_source": "graph_semantic",
        "embedding_source": str(result.get("embedding_source") or "")[:24],
    }


def semantic_observation_summary(
    requester_user_id: str,
    topic: str,
    *,
    excluded_user_ids: set[str] | list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Separate operator path for shadow observation; never returns user IDs."""
    result = retrieve_semantic_preference_candidates(
        requester_user_id,
        topic,
        excluded_user_ids=excluded_user_ids,
    )
    return {
        "status": "success",
        "canonical_key": result.get("canonical_key", ""),
        "candidate_count": int(result.get("candidate_count", 0) or 0),
        "semantic_concepts_considered": list(
            result.get("semantic_concepts_considered") or []
        ),
        "retrieval_source": result.get("retrieval_source", "graph_semantic"),
        "embedding_source": result.get("embedding_source", ""),
    }
