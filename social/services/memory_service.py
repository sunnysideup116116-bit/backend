import re
import time
import requests

from database import db, profiles_coll
from services.language_service import normalize_zh_tw

AGENT_URL = "http://127.0.0.1:9001"
MEMORY_PREFIX_RE = re.compile(r"^(?:喜歡|不喜歡|避免|需要|偏好|討厭)\s*[：:、，,]?\s*")
MEMORY_OUTBOX = db["profile_memory_outbox"]


class MemoryWriteError(RuntimeError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


def _queue_memory_retry(user_id: str, proposals: list[dict], surface: str, message_id: str | None,
                        match_id: str | None, error_code: str) -> None:
    """Keep validated proposals for retry without storing raw chat text."""
    document = {
        "user_id": user_id, "memories": proposals[:3], "surface": surface, "match_id": match_id,
        "message_id": message_id, "status": "pending", "last_error_code": error_code,
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
    clean = dict(item or {})
    label = normalize_zh_tw(str(clean.get("label", "")), max_length=40)
    while label and MEMORY_PREFIX_RE.match(label):
        label = MEMORY_PREFIX_RE.sub("", label, count=1).strip()
    clean["label"] = label
    return clean


def memory_summary(items: list[dict]) -> str:
    labels = {"dislike": "不喜歡", "avoid": "避免", "require": "需要", "like": "喜歡"}
    return "、".join(
        labels.get(item.get("stance"), "喜歡") + normalize_memory_item(item).get("label", "")
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
    now = time.time()
    synced = float(profile.get("profile_memory_synced_at") or 0)
    retry_at = float(profile.get("profile_memory_retry_at") or 0)
    if not force and (now - synced < 300 or retry_at > now):
        return profile
    revision = profile.get("profile_memory_revision")
    guard = {"user_id": user_id, "profile_memory_revision": revision}
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
        response = requests.get(f"{AGENT_URL}/api/memory/{user_id}",
                                params={"limit": 8, "durable_only": "true", "query": query[:120]},
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
    updated = profiles_coll.update_one({"user_id": user_id, "profile_memory_revision": base.get("profile_memory_revision")}, {"$set": {
        "profile_memory_preview": compact,
        "profile_memory_summary": memory_summary(compact),
        "profile_memory_synced_at": time.time() if snapshot["available"] else 0,
        "profile_memory_retry_at": 0,
    }, "$inc": {"profile_memory_revision": 1}})
    if not updated.modified_count:
        latest = profiles_coll.find_one({"user_id": user_id}) or {}
        return list(latest.get("profile_memory_preview") or [])[:12]
    return compact

def apply_profile_memory_proposals(user_id: str, proposals: list[dict], surface: str, message_id: str | None, match_id: str | None = None) -> list[dict]:
    """Write validated profile-memory proposals and surface graph failures explicitly."""
    if not proposals:
        return []
    try:
        response = requests.post(f"{AGENT_URL}/api/memory/apply", json={
            "user_id": user_id, "memories": proposals, "surface": surface,
            "match_id": match_id, "message_id": message_id,
        }, timeout=30)
    except requests.RequestException:
        error_code = "memory_agent_unavailable"
        _queue_memory_retry(user_id, proposals, surface, message_id, match_id, error_code)
        raise MemoryWriteError(error_code)

    if response.status_code == 404:
        error_code = "memory_apply_endpoint_not_found"
        _queue_memory_retry(user_id, proposals, surface, message_id, match_id, error_code)
        raise MemoryWriteError(error_code)
    try:
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        error_code = "memory_agent_invalid_response"
        _queue_memory_retry(user_id, proposals, surface, message_id, match_id, error_code)
        raise MemoryWriteError(error_code) from exc
    if payload.get("status") == "error":
        error_code = str(payload.get("error_code") or "graph_write_failed")[:80]
        _queue_memory_retry(user_id, proposals, surface, message_id, match_id, error_code)
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
    try:
        response = requests.post(f"{AGENT_URL}/api/memory/action", json={
            "user_id": user_id, "key": key, "action": action, "value": value,
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
