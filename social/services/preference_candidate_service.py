"""Bounded internal adapter for exact durable-preference candidate retrieval."""

from __future__ import annotations

from typing import Any

import requests

from matchmaker_agent.concept_identity import (
    PreferenceTextError,
    canonical_query_provenance,
    canonicalize_concept,
)


AGENT_URL = "http://127.0.0.1:9001"


class PreferenceCandidateLookupError(RuntimeError):
    pass


class ActivityAvoidanceSnapshotError(RuntimeError):
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
    v2_only: bool = False,
    additional_topics: list[str] | tuple[str, ...] = (),
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
    additional_identities = []
    if v2_only:
        additional_keys = {identity.key}
        for value in list(additional_topics or ())[:4]:
            try:
                additional_identity = canonicalize_concept(value)
            except PreferenceTextError:
                continue
            if additional_identity and additional_identity.key not in additional_keys:
                additional_keys.add(additional_identity.key)
                additional_identities.append(additional_identity)
    try:
        request_payload = {
            "requester_user_id": requester_user_id,
            "topic": identity.label,
            **{field: identity.as_dict()[field] for field in (
                "canonical_key", "canonicalization_version", "semantic_text", "semantic_input_hash",
            )},
            "excluded_user_ids": excluded,
            "limit": safe_limit,
        }
        if v2_only:
            request_payload["v2_only"] = True
            request_payload["additional_topics"] = [item.label for item in additional_identities]
        response = requests.post(
            f"{AGENT_URL}/api/preferences/candidates",
            json=request_payload,
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
        or (v2_only and payload.get("v2_only") is not True)
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
    result = {
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
    if v2_only:
        raw_groups = payload.get("additional_candidates")
        if not isinstance(raw_groups, list):
            raise PreferenceCandidateLookupError("preference_graph_invalid_response")
        expected_keys = {item.key for item in additional_identities}
        groups_by_key: dict[str, list[str]] = {}
        for group in raw_groups[:4]:
            if not isinstance(group, dict):
                continue
            key = str(group.get("canonical_key") or "").strip()[:100]
            raw_ids = group.get("candidate_ids")
            if key not in expected_keys or not isinstance(raw_ids, list):
                continue
            ids = groups_by_key.setdefault(key, [])
            for value in raw_ids[:safe_limit]:
                candidate_id = str(value or "").strip()[:128]
                if (candidate_id and candidate_id != requester_user_id
                        and candidate_id not in excluded and candidate_id not in ids):
                    ids.append(candidate_id)
                    if len(ids) >= safe_limit:
                        break
        if expected_keys.difference(groups_by_key):
            raise PreferenceCandidateLookupError("preference_graph_invalid_response")
        result["v2_only"] = True
        result["additional_candidates"] = [
            {"canonical_key": item.key, "candidate_ids": groups_by_key.get(item.key, [])}
            for item in additional_identities
        ]
    return result


def retrieve_activity_avoidance_snapshot(
    candidate_ids: list[str] | tuple[str, ...],
    topic_family: list[str] | tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    """Read a complete bounded v2 AVOIDS snapshot for activity qualification.

    The result must account for every requested candidate. An absent, malformed,
    or unavailable graph result raises instead of silently meaning "no AVOID".
    """
    safe_ids = list(dict.fromkeys(
        str(value or "").strip()[:128] for value in candidate_ids
        if str(value or "").strip()
    ))
    if (not safe_ids or len(safe_ids) > 100
            or len(safe_ids) != len(candidate_ids)):
        raise ActivityAvoidanceSnapshotError("activity_safety_candidate_ids_invalid")
    identities = []
    seen_keys = set()
    for value in list(topic_family or ()):
        try:
            identity = canonicalize_concept(value)
        except PreferenceTextError as exc:
            raise ActivityAvoidanceSnapshotError("activity_safety_topic_invalid") from exc
        if identity and identity.key not in seen_keys:
            seen_keys.add(identity.key)
            identities.append(identity)
    if not identities or len(identities) > 5:
        raise ActivityAvoidanceSnapshotError("activity_safety_topic_invalid")
    topic_keys = {identity.key for identity in identities}
    request_payload = {
        "candidate_ids": safe_ids,
        "topic_family": [identity.label for identity in identities],
    }
    try:
        response = requests.post(
            f"{AGENT_URL}/api/preferences/activity-avoidances",
            json=request_payload,
            timeout=(1, 5),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ActivityAvoidanceSnapshotError("activity_safety_unavailable") from exc
    if not isinstance(payload, dict) or payload.get("status") != "success":
        code = payload.get("error_code") if isinstance(payload, dict) else None
        raise ActivityAvoidanceSnapshotError(str(code or "activity_safety_invalid_response")[:80])
    raw_checks = payload.get("candidate_checks")
    if not isinstance(raw_checks, list) or len(raw_checks) != len(safe_ids):
        raise ActivityAvoidanceSnapshotError("activity_safety_invalid_response")
    checks: dict[str, dict[str, Any]] = {}
    for item in raw_checks:
        if not isinstance(item, dict):
            raise ActivityAvoidanceSnapshotError("activity_safety_invalid_response")
        candidate_id = str(item.get("candidate_id") or "").strip()[:128]
        verification_status = str(item.get("verification_status") or "")
        raw_avoid_keys = item.get("avoid_concept_keys")
        if (candidate_id not in safe_ids or candidate_id in checks
                or verification_status not in {"verified", "unverified"}
                or not isinstance(raw_avoid_keys, list)
                or len(raw_avoid_keys) > len(topic_keys)):
            raise ActivityAvoidanceSnapshotError("activity_safety_invalid_response")
        avoid_keys = []
        for value in raw_avoid_keys:
            key = str(value or "").strip()[:100]
            if key not in topic_keys or key in avoid_keys:
                raise ActivityAvoidanceSnapshotError("activity_safety_invalid_response")
            avoid_keys.append(key)
        checks[candidate_id] = {
            "verification_status": verification_status,
            "avoid_concept_keys": sorted(avoid_keys),
        }
    if set(checks) != set(safe_ids):
        raise ActivityAvoidanceSnapshotError("activity_safety_invalid_response")
    return checks
