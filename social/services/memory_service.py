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
        "updated_at": time.time(),
    }
    try:
        if message_id:
            MEMORY_OUTBOX.update_one({"message_id": message_id}, {"$set": document, "$setOnInsert": {"created_at": time.time()}}, upsert=True)
        else:
            MEMORY_OUTBOX.insert_one({**document, "created_at": time.time()})
    except Exception as exc:
        print(f"[MEMORY][outbox] skipped user={user_id} error={type(exc).__name__}")


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


def _sync_memory_projection(user_id: str, learned: list[dict]) -> list[dict]:
    graph_items = get_user_graph_memories(user_id, limit=12)
    compact = sorted((graph_items or [normalize_memory_item(item) for item in learned]),
                     key=lambda x: x.get("last_seen_at", 0), reverse=True)[:12]
    profiles_coll.update_one({"user_id": user_id}, {"$set": {
        "profile_memory_preview": compact,
        "profile_memory_summary": memory_summary(compact),
        "profile_memory_synced_at": time.time(),
    }}, upsert=True)
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
    _sync_memory_projection(user_id, learned)
    if message_id:
        MEMORY_OUTBOX.update_one({"message_id": message_id}, {"$set": {"status": "applied", "updated_at": time.time()}, "$unset": {"last_error_code": ""}})
    notices = [{"type": "memory_learned", "message": f"我記住了：{item['label']}。記錯的話可以在設定裡撤銷。",
                "memory": item, "created_at": time.time()} for item in learned]
    profiles_coll.update_one({"user_id": user_id}, {"$push": {"memory_notices": {"$each": notices}}}, upsert=True)
    return learned
def apply_memory_action(user_id: str, key: str, action: str, value: str | None = None):
    response = requests.post(f"{AGENT_URL}/api/memory/action", json={"user_id": user_id, "key": key, "action": action, "value": value}, timeout=30)
    response.raise_for_status()
    result = response.json()
    _sync_memory_projection(user_id, [])
    return result

