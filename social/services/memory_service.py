import re
import time
import requests

from database import db, profiles_coll
from services.language_service import normalize_zh_tw
from matchmaker_agent.concept_identity import (
    PreferenceTextError, canonicalize_concept, display_preference_label,
    durable_memory_limit, has_mixed_preference_polarity, normalize_preference_text,
    split_compound_concept_label, split_explicit_preference_enumeration,
    stored_concept_identity,
    MAX_PREFERENCE_EVIDENCE_CHARS,
    MAX_OWNER_MEMORY_QUERY_CHARS,
    normalize_fresh_preference_text,
)

AGENT_URL = "http://127.0.0.1:9001"
MEMORY_PREFIX_RE = re.compile(r"^(?:喜歡|不喜歡|避免|需要|偏好|討厭)\s*[：:、，,]?\s*")
MEMORY_OUTBOX = db["profile_memory_outbox"]


class MemoryWriteError(RuntimeError):
    def __init__(self, error_code: str, *, retryable: bool = True):
        super().__init__(error_code)
        self.error_code = error_code
        self.retryable = retryable


def validate_memory_proposals(proposals: list[dict]) -> list[dict]:
    """Preflight the entire write batch; never persist its valid prefix."""
    try:
        if len(proposals) > durable_memory_limit():
            raise PreferenceTextError("too_many_preferences")
        prepared = []
        for item in proposals:
            if not isinstance(item, dict):
                raise PreferenceTextError("invalid_preference")
            identity = stored_concept_identity(item) if item.get("canonicalization_version") == "v2" else None
            if item.get("canonicalization_version") == "v2" and not identity:
                raise PreferenceTextError("invalid_preference_identity")
            label = (identity.semantic_text if identity else normalize_fresh_preference_text(
                item.get("semantic_text") or item.get("label")))
            if (has_mixed_preference_polarity(label)
                    or split_explicit_preference_enumeration(label)
                    or split_compound_concept_label(label)):
                raise PreferenceTextError("invalid_atomic_preference")
            identity = identity or canonicalize_concept(label, item.get("key"))
            if not identity or item.get("stance") not in {"like", "dislike", "avoid", "require"}:
                raise PreferenceTextError("invalid_preference")
            if len(str(item.get("evidence_span") or "")) > MAX_PREFERENCE_EVIDENCE_CHARS:
                raise PreferenceTextError("evidence_too_long")
            prepared.append({**item, **identity.as_dict()})
        return prepared
    except PreferenceTextError as exc:
        raise MemoryWriteError(exc.code, retryable=False) from None


def _queue_memory_retry(user_id: str, proposals: list[dict], surface: str, message_id: str | None,
                        match_id: str | None, error_code: str, *, source_created_at=None) -> None:
    """Keep validated proposals for retry without storing raw chat text."""
    proposals = validate_memory_proposals(proposals)
    document = {
        "user_id": user_id, "memories": proposals, "surface": surface, "match_id": match_id,
        "message_id": message_id, "status": "pending", "last_error_code": error_code,
        "source_created_at": source_created_at,
        "updated_at": time.time(), "next_attempt_at": time.time() + 30,
    }
    try:
        if message_id:
            MEMORY_OUTBOX.update_one({"message_id": message_id}, {"$set": document, "$setOnInsert": {"created_at": time.time()}}, upsert=True)
        else:
            MEMORY_OUTBOX.insert_one({**document, "created_at": time.time()})
    except Exception as exc:
        print(f"[MEMORY][outbox] enqueue skipped error={type(exc).__name__}")


def normalize_memory_item(item: dict) -> dict:
    """Read projection: preserve full v2 source and never re-key legacy data."""
    clean = dict(item or {})
    if clean.get("canonicalization_version") == "v2":
        # Semantic source is not display text and must not undergo OpenCC/UI
        # rewriting or prefix removal on a cache refresh.
        identity = stored_concept_identity(clean)
        label = identity.semantic_text if identity else ""
        if not identity:
            clean["fidelity_status"] = "invalid"
    else:
        label = normalize_zh_tw(str(clean.get("label", "")))
        while label and MEMORY_PREFIX_RE.match(label):
            label = MEMORY_PREFIX_RE.sub("", label, count=1).strip()
        clean.setdefault("canonicalization_version", "legacy_unknown")
        clean.setdefault("fidelity_status", "legacy_unknown")
    clean["label"] = label
    clean["display_label"] = display_preference_label(label)
    return clean


def memory_summary(items: list[dict]) -> str:
    labels = {"dislike": "不喜歡", "avoid": "避免", "require": "需要", "like": "喜歡"}
    return "、".join(
        labels.get(item.get("stance"), "喜歡") + normalize_memory_item(item).get("display_label", "")
        for item in items[:8] if normalize_memory_item(item).get("label")
    )[:300]


def get_user_graph_memories(user_id: str, limit: int = 20) -> list[dict]:
    """Return active graph preferences through the canonical matchmaker API."""
    try:
        response = requests.get(f"{AGENT_URL}/api/memory/{user_id}", params={"limit": limit}, timeout=12)
        response.raise_for_status()
        items = response.json().get("memories", [])
    except Exception:
        items = []
    return [{**normalize_memory_item(item), "owner_user_id": user_id}
            for item in items if normalize_memory_item(item).get("label")]


def get_graph_memory_snapshot(user_id: str, limit: int = 20) -> dict:
    """Return a status-aware projection for cache refresh without changing list callers."""
    try:
        response = requests.get(
            f"{AGENT_URL}/api/memory/{user_id}",
            params={"limit": limit, "durable_only": "true"}, timeout=(1, 2),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return {"available": False, "items": [], "error_code": "invalid_graph_response"}
        if payload.get("status") != "success":
            return {"available": False, "items": [], "error_code": str(
                payload.get("error_code") or "graph_read_failed"
            )[:80]}
        raw_items = payload.get("memories")
        if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
            return {"available": False, "items": [], "error_code": "invalid_graph_response"}
    except (requests.RequestException, ValueError):
        return {"available": False, "items": [], "error_code": "memory_agent_unavailable"}
    items = [
        {**normalize_memory_item(item), "owner_user_id": user_id}
        for item in raw_items if item.get("stance") in {"like", "dislike", "avoid", "require"}
        and normalize_memory_item(item).get("label")
    ]
    return {"available": True, "items": items, "error_code": None}


def refresh_owner_memory_profile(user_id: str, profile: dict, *, force=False) -> dict:
    """Refresh stale nonempty projections, with CAS against concurrent mutations.

    The caller passes its already-loaded owner profile. A successful empty
    Graph response clears stale data; outages keep the cache and retry later.
    """
    if not isinstance(profile, dict) or profile.get("user_id") != user_id:
        return profile
    if profile.get("preference_bootstrap_pending"):
        return {**profile, "profile_memory_preview": [], "profile_memory_summary": ""}
    now = time.time()
    synced = float(profile.get("profile_memory_synced_at") or 0)
    retry_at = float(profile.get("profile_memory_retry_at") or 0)
    if not force and (now - synced < 300 or retry_at > now):
        return profile
    revision = profile.get("profile_memory_revision")
    guard = {"user_id": user_id, "profile_memory_revision": revision,
             "preference_bootstrap_pending": {"$exists": False}}
    try:
        snapshot = get_graph_memory_snapshot(user_id, limit=12)
        if not snapshot["available"]:
            if not force:
                profiles_coll.update_one(guard, {"$set": {"profile_memory_retry_at": now + 30}})
            latest = profiles_coll.find_one({"user_id": user_id})
            return latest if isinstance(latest, dict) else profile
        items = snapshot["items"][:12]
        values = {"profile_memory_preview": items, "profile_memory_summary": memory_summary(items),
                  "profile_memory_synced_at": now, "profile_memory_retry_at": 0}
        updated = profiles_coll.update_one(guard, {"$set": values, "$inc": {"profile_memory_revision": 1}})
        if updated.modified_count:
            return {**profile, **values, "profile_memory_revision": int(revision or 0) + 1}
        # A newer action/refresh won. Never inject the stale remote result.
        latest = profiles_coll.find_one({"user_id": user_id})
        return latest if isinstance(latest, dict) else profile
    except Exception:
        return profile


def _invalidate_memory_projection(user_id, key=None):
    update = {"$set": {"profile_memory_synced_at": 0, "profile_memory_retry_at": 0},
              "$inc": {"profile_memory_revision": 1}}
    if key:
        update["$pull"] = {"profile_memory_preview": {"key": key}}
    profiles_coll.update_one({"user_id": user_id}, update)


def search_owner_memory(user_id: str, query: str, profile: dict) -> dict:
    """Query Graph before limiting results; never replace the general cache."""
    from services.owner_memory_projection import preference_wording
    try:
        query = normalize_preference_text(query, max_length=MAX_OWNER_MEMORY_QUERY_CHARS)
    except PreferenceTextError:
        return {"preferences": [], "summary": "", "status": "invalid_query",
                "error_code": "invalid_query", "source": "none", "truncated": False}
    try:
        response = requests.get(f"{AGENT_URL}/api/memory/{user_id}",
                                params={"limit": 8, "durable_only": "true", "query": query},
                                timeout=(1, 3))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "success" or not isinstance(payload.get("memories"), list):
            raise ValueError("invalid_memory_response")
        words = preference_wording(payload["memories"], owner_id=user_id)
        return {"preferences": words, "summary": "、".join(words)[:600],
                "status": "available", "source": "graph", "truncated": bool(payload.get("truncated", False))}
    except (requests.RequestException, ValueError, TypeError):
        words = preference_wording(profile.get("profile_memory_preview"), owner_id=user_id)
        return {"preferences": words, "summary": "、".join(words)[:600],
                "status": "unavailable", "source": "cache", "truncated": True}


def _sync_memory_projection(user_id: str, learned: list[dict]) -> list[dict]:
    base = profiles_coll.find_one({"user_id": user_id}) or {}
    if base.get("preference_bootstrap_pending"):
        return []
    snapshot = get_graph_memory_snapshot(user_id, limit=12)
    if snapshot["available"]:
        source = snapshot["items"]
    else:
        # A failed Graph read must not replace the entire cache with only the
        # newly learned batch (or wipe it after an action).
        merged = {str(item.get("key") or item.get("label")): normalize_memory_item(item)
                  for item in list(base.get("profile_memory_preview") or []) + list(learned)
                  if isinstance(item, dict) and item.get("stance") in {"like", "dislike", "avoid", "require"}}
        source = list(merged.values())
    compact = sorted(source,
                     key=lambda x: x.get("last_seen_at", 0), reverse=True)[:12]
    updated = profiles_coll.update_one({"user_id": user_id, "profile_memory_revision": base.get("profile_memory_revision"),
                                       "preference_bootstrap_pending": {"$exists": False}}, {"$set": {
        "profile_memory_preview": compact,
        "profile_memory_summary": memory_summary(compact),
        "profile_memory_synced_at": time.time() if snapshot["available"] else 0,
        "profile_memory_retry_at": 0,
    }, "$inc": {"profile_memory_revision": 1}})
    if not updated.modified_count:
        latest = profiles_coll.find_one({"user_id": user_id}) or {}
        return list(latest.get("profile_memory_preview") or [])[:12]
    return compact

def apply_profile_memory_proposals(user_id: str, proposals: list[dict], surface: str, message_id: str | None, match_id: str | None = None, *, source_created_at=None) -> list[dict]:
    """Write validated profile-memory proposals and surface graph failures explicitly."""
    if not proposals:
        return []
    proposals = validate_memory_proposals(proposals)
    if source_created_at is None:
        # Explicit/manual fresh operations use entry time. Async callers pass
        # original saved source time and outbox retries retain it unchanged.
        source_created_at = time.time()
    try:
        # Old Matchmaker servers do not expose this route: rolling-version
        # mismatch fails before an old writer can truncate and persist v2 input.
        response = requests.post(f"{AGENT_URL}/api/v2/memory/apply", json={
            "user_id": user_id, "memories": proposals, "surface": surface,
            "match_id": match_id, "message_id": message_id,
            "source_created_at": source_created_at,
        }, timeout=30)
    except requests.RequestException:
        error_code = "memory_agent_unavailable"
        _queue_memory_retry(user_id, proposals, surface, message_id, match_id, error_code, source_created_at=source_created_at)
        raise MemoryWriteError(error_code)

    if response.status_code == 404:
        error_code = "memory_apply_endpoint_not_found"
        _queue_memory_retry(user_id, proposals, surface, message_id, match_id, error_code, source_created_at=source_created_at)
        raise MemoryWriteError(error_code)
    if response.status_code in {400, 409, 413, 422}:
        raise MemoryWriteError("memory_validation_rejected", retryable=False)
    try:
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        error_code = "memory_agent_invalid_response"
        _queue_memory_retry(user_id, proposals, surface, message_id, match_id, error_code, source_created_at=source_created_at)
        raise MemoryWriteError(error_code) from exc
    if payload.get("status") == "error":
        error_code = str(payload.get("error_code") or "graph_write_failed")[:80]
        if payload.get("retryable") is False or error_code in {
            "preference_text_too_long", "invalid_atomic_preference", "invalid_preference",
            "too_many_preferences", "evidence_too_long", "legacy_unverified_memory_proposal",
        }:
            raise MemoryWriteError(error_code, retryable=False)
        _queue_memory_retry(user_id, proposals, surface, message_id, match_id, error_code, source_created_at=source_created_at)
        raise MemoryWriteError(error_code)
    learned = payload.get("memories", [])

    learned = [normalize_memory_item(item) for item in learned if normalize_memory_item(item).get("label")]
    if not learned:
        return []
    _invalidate_memory_projection(user_id)
    _sync_memory_projection(user_id, learned)
    if message_id:
        MEMORY_OUTBOX.update_one({"message_id": message_id}, {"$set": {"status": "applied", "updated_at": time.time()}, "$unset": {"last_error_code": ""}})
    notices = [{"type": "memory_learned", "message": f"我記住了：{item['label']}。記錯的話可以在設定裡撤銷。",
                "memory": item, "created_at": time.time()} for item in learned]
    profiles_coll.update_one({"user_id": user_id}, {"$push": {"memory_notices": {"$each": notices}}}, upsert=True)
    return learned
def apply_memory_action(user_id: str, key: str, action: str, value: str | None = None):
    source_created_at = time.time()
    if action == "correct":
        try:
            value = normalize_fresh_preference_text(value)
        except PreferenceTextError as exc:
            raise MemoryWriteError(exc.code, retryable=False) from None
    try:
        response = requests.post(f"{AGENT_URL}/api/v2/memory/action", json={
            "user_id": user_id, "key": key, "action": action, "value": value,
            "source_created_at": source_created_at,
        }, timeout=30)
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise MemoryWriteError("memory_action_unavailable") from exc
    if result.get("status") not in {"success", "expired"}:
        raise MemoryWriteError(str(
            result.get("error_code") or result.get("status") or "memory_action_failed"
        )[:80])
    _invalidate_memory_projection(user_id, key if action in {"disable", "correct"} else None)
    _sync_memory_projection(user_id, [])
    return result
