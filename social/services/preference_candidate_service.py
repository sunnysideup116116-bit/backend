"""Bounded internal adapter for exact durable-preference candidate retrieval."""

from __future__ import annotations

from typing import Any

import requests

from matchmaker_agent.concept_identity import (
    canonical_query_provenance,
    canonicalize_concept,
)


AGENT_URL = "http://127.0.0.1:9001"


class PreferenceCandidateLookupError(RuntimeError):
    pass


def _bounded_count(value: Any, fallback: int) -> int:
    try:
        return max(0, min(int(value), 100))
    except (TypeError, ValueError):
        return max(0, min(int(fallback), 100))


def retrieve_preference_candidate_ids(
    requester_user_id: str,
    topic: str,
    *,
    excluded_user_ids: set[str] | list[str] | tuple[str, ...],
    limit: int,
) -> dict[str, Any]:
    identity = canonicalize_concept(topic)
    if not identity:
        return {
            "candidate_ids": [], "canonical_key": "", "normalized_topic": "",
            "retrieval_source": "graph_exact",
        }
    safe_limit = max(1, min(int(limit or 1), 100))
    excluded = list(dict.fromkeys(
        str(value).strip()[:128]
        for value in excluded_user_ids
        if str(value or "").strip()
    ))[:200]
    try:
        response = requests.post(
            f"{AGENT_URL}/api/preferences/candidates",
            json={
                "requester_user_id": requester_user_id,
                "topic": identity.label,
                **{field: identity.as_dict()[field] for field in (
                    "canonical_key", "canonicalization_version", "semantic_text", "semantic_input_hash",
                )},
                "excluded_user_ids": excluded,
                "limit": safe_limit,
            },
            timeout=(1, 5),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise PreferenceCandidateLookupError("preference_graph_unavailable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "success"
        or payload.get("canonical_key") != identity.key
        or not isinstance(payload.get("candidate_ids"), list)
    ):
        raise PreferenceCandidateLookupError(str(
            payload.get("error_code") if isinstance(payload, dict) else "preference_graph_invalid_response"
        )[:80])
    candidate_ids = []
    for value in payload["candidate_ids"][:safe_limit]:
        candidate_id = str(value or "").strip()[:128]
        if (
            candidate_id and candidate_id != requester_user_id
            and candidate_id not in excluded and candidate_id not in candidate_ids
        ):
            candidate_ids.append(candidate_id)
    return {
        "candidate_ids": candidate_ids,
        "canonical_key": identity.key,
        "normalized_topic": identity.label,
        "query_provenance": canonical_query_provenance(topic, identity),
        "retrieval_source": "graph_exact",
        "candidate_count_before_filter": _bounded_count(
            payload.get("candidate_count_before_filter"), len(candidate_ids),
        ),
        "candidate_count_after_filter": _bounded_count(
            payload.get("candidate_count_after_filter"), len(candidate_ids),
        ),
    }
