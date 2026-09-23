"""Durable registration bootstrap. Only canonical Appwrite form data is seeded.

No LLM/HTTP runs in registration requests. Existing memory and owner corrections
always win over registration data; failed jobs remain visible in the outbox.
"""
import hashlib
import json
import re
import time
from urllib.parse import urlparse

import requests
from matchmaker_agent.concept_identity import (
    PreferenceTextError, durable_memory_limit, normalize_preference_text,
    stored_concept_identity,
    MAX_PREFERENCE_EVIDENCE_CHARS, MAX_REGISTRATION_INTEREST_CHARS,
    MAX_PROFILE_SOURCE_CHARS, normalize_fresh_preference_text,
)

from database import db

OUTBOX = db["profile_memory_outbox"]
VERSION = "registration-bootstrap-v1"
INTEREST_FRAME = "我在註冊表單填寫的平常興趣是："


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def enqueue_registration_bootstrap(user_id, *, name_revision=None):
    """Idempotent enqueue; a queue outage must not fail account registration."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,35}", str(user_id or "")):
        return False
    identity_only = name_revision is not None
    message_id = ("registration-identity:" if identity_only else "registration-interest:") + _digest(user_id)
    if identity_only:
        # A fresh rename revision is required for A -> B -> A as well.
        message_id += ":" + str(time.time_ns())
    now = time.time()
    try:
        OUTBOX.update_one({"_id": message_id}, {"$setOnInsert": {
            "message_id": message_id, "user_id": user_id,
            "job_kind": "registration_identity" if identity_only else "registration_bootstrap",
            "surface": "registration_interest", "status": "pending", "memories": [],
            "created_at": now, "updated_at": now, "next_attempt_at": now,
        }}, upsert=True)
        return True
    except Exception as exc:
        print(f"[registration-graph] enqueue_failed error={type(exc).__name__}")
        return False


def read_registration_profile(user_id):
    """None is confirmed missing. Outages raise, never fall back to stale Mongo."""
    from services import public_nickname_service as names
    base = names._nickname_endpoint()
    parsed = urlparse(base)
    if (not names._PROJECT_ID or not names._API_KEY or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not (parsed.scheme == "https" or
                    (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,35}", str(user_id or ""))):
        raise RuntimeError("registration_profile_unavailable")
    response = requests.get(
        f"{base}/databases/dating_db/collections/user_profiles/documents/{user_id}",
        headers={"X-Appwrite-Project": names._PROJECT_ID, "X-Appwrite-Key": names._API_KEY},
        params={"queries[]": json.dumps({"method": "select", "values": ["name", "interest"]})},
        timeout=(2, 5), allow_redirects=False,
    )
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise RuntimeError("registration_profile_unavailable")
    profile = response.json()
    if not isinstance(profile, dict) or not isinstance(profile.get("interest", ""), str):
        raise RuntimeError("registration_profile_invalid")
    interest = str(profile.get("interest") or "").strip()
    if interest:
        normalize_fresh_preference_text(interest, max_length=MAX_REGISTRATION_INTEREST_CHARS)
    return {"name": names.safe_proposal_nickname(profile.get("name"), user_id),
            "interest": interest}


def registration_evidence(evidence, interest):
    """Map validated framed evidence back to the original form field only."""
    raw = str(evidence or "").strip()
    if len(raw) > MAX_PREFERENCE_EVIDENCE_CHARS or len(str(interest or "")) > MAX_REGISTRATION_INTEREST_CHARS:
        return ""
    if raw and raw in interest:
        return raw
    span = normalize_fresh_preference_text(raw, max_length=MAX_PREFERENCE_EVIDENCE_CHARS)
    source = normalize_fresh_preference_text(interest, max_length=MAX_REGISTRATION_INTEREST_CHARS)
    framed = normalize_fresh_preference_text(INTEREST_FRAME + interest, max_length=MAX_PROFILE_SOURCE_CHARS)
    if len(span) > MAX_PREFERENCE_EVIDENCE_CHARS or len(source) > MAX_REGISTRATION_INTEREST_CHARS:
        return ""
    if not span or not source or not framed.endswith(source):
        return ""
    start = framed.find(span)
    if start < 0:
        return ""
    overlap_start = max(start, len(framed) - len(source))
    overlap_end = min(start + len(span), len(framed))
    if overlap_start >= overlap_end:
        return ""
    owner_span = framed[overlap_start:overlap_end]
    # Punctuation normalization can change characters, but provenance must
    # retain actual owner text, not the generated framing or a rewritten quote.
    return owner_span if owner_span in interest else interest


def registration_memories(interest):
    from services.profile_skills import analyze_profile_message
    from services.memory_service import MemoryWriteError
    if not interest or interest in {"無特別興趣", "沒有特別興趣"}:
        return []
    normalize_fresh_preference_text(interest, max_length=MAX_REGISTRATION_INTEREST_CHARS)
    decision = analyze_profile_message(INTEREST_FRAME + interest)
    for code in decision.get("memory_codes", []):
        if code in {"memory_limit_exceeded", "memory_contract_limit_rejected", "preference_text_too_long"}:
            raise MemoryWriteError(code, retryable=False)
    reason = (decision.get("recent_context") or {}).get("reason_code", "")
    if not decision.get("contract") and reason not in {"blocked_input", "protected_attribute"}:
        raise RuntimeError("registration_extraction_unavailable")
    result = []
    for item in decision.get("memories", [])[:durable_memory_limit()]:
        evidence = registration_evidence(item.get("evidence_span"), interest)
        if (item.get("stance") == "like"
                and item.get("category") in {"activity", "habit", "lifestyle"}
                and evidence and float(item.get("confidence") or 0) >= .9):
            result.append({**item, "evidence_span": evidence})
    return result


def process_registration_job(record):
    from services.memory_service import (
        AGENT_URL, MemoryWriteError, apply_profile_memory_proposals,
        validate_memory_proposals,
    )
    observed_at = time.time()
    try:
        profile = read_registration_profile(record["user_id"])
        if profile and profile.get("interest"):
            normalize_fresh_preference_text(profile["interest"], max_length=MAX_REGISTRATION_INTEREST_CHARS)
    except PreferenceTextError as exc:
        raise MemoryWriteError(exc.code, retryable=False) from None
    if profile is None:
        # Appwrite creation can be briefly delayed; bounded worker retries.
        raise RuntimeError("registration_profile_missing")
    memories = record.get("prepared_memories")
    if memories is not None:
        if any(not stored_concept_identity(item) for item in memories):
            raise MemoryWriteError("legacy_unverified_memory_proposal", retryable=False)
        memories = validate_memory_proposals(memories)
    response = requests.post(AGENT_URL + "/api/users/registration-projection", json={
        "user_id": record["user_id"], "name": profile["name"], "observed_at": observed_at,
    }, timeout=(2, 15))
    response.raise_for_status()
    projected = response.json()
    if projected.get("version") != VERSION or projected.get("status") != "success":
        raise RuntimeError("registration_projection_version_mismatch")
    if record["job_kind"] == "registration_identity" or not projected.get("registration_seed_allowed"):
        return
    source_hash = _digest(profile["interest"])
    if record.get("source_hash") and record["source_hash"] != source_hash:
        raise RuntimeError("registration_source_changed")
    if memories is None:
        memories = validate_memory_proposals(registration_memories(profile["interest"]))
        result = OUTBOX.update_one({"_id": record["_id"], "lease_token": record["lease_token"]},
                                  {"$set": {"prepared_memories": memories, "source_hash": source_hash}})
        if not result.matched_count:
            raise RuntimeError("registration_lease_lost")
    if memories:
        apply_profile_memory_proposals(record["user_id"], memories, "registration_interest", record["message_id"])
