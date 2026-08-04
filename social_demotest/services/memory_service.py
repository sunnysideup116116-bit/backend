import json
import re
import time
import requests
from pathlib import Path

from database import db, profiles_coll
from services.language_service import normalize_zh_tw
from services.profile_skills import memory_candidate_allowed, profile_skills_mode_for_user

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


def _agent_graph_config():
    from dotenv import dotenv_values
    env = dotenv_values(Path(__file__).resolve().parents[2] / "matchmaker_agent" / ".env")
    return env.get("NEO4J_URI"), (env.get("NEO4J_USERNAME"), env.get("NEO4J_PASSWORD")), env.get("NEO4J_DATABASE", "neo4j")


def get_user_graph_memories(user_id: str, limit: int = 20) -> list[dict]:
    """Return active graph preferences, always scoped to their owner."""
    try:
        response = requests.get(f"{AGENT_URL}/api/memory/{user_id}", params={"limit": limit}, timeout=12)
        response.raise_for_status()
        items = response.json().get("memories", [])
    except Exception:
        try:
            from neo4j import GraphDatabase
            uri, auth, database = _agent_graph_config()
            with GraphDatabase.driver(uri, auth=auth) as driver:
                with driver.session(database=database) as session:
                    rows = session.run("""
                        MATCH (u:User {id:$user_id})-[r:HAS_PREFERENCE]->(t:Trait)
                        WHERE coalesce(r.active,true)=true
                        RETURN coalesce(t.key,toLower(replace(t.name,' ','_'))) AS key,
                               coalesce(r.display_label_zh_tw,t.name) AS label,
                               coalesce(r.stance,'like') AS stance, t.category AS category,
                               coalesce(r.confidence,0.7) AS confidence,
                               coalesce(r.last_seen_at,0) AS last_seen_at
                        ORDER BY confidence DESC,last_seen_at DESC LIMIT $limit
                    """, user_id=user_id, limit=max(1, min(limit, 30)))
                    items = [dict(row) for row in rows]
        except Exception:
            items = []
    return [{**normalize_memory_item(item), "owner_user_id": user_id}
            for item in items if normalize_memory_item(item).get("label")]


def _observe_direct(user_id: str, text: str, surface: str, message_id: str | None = None):
    from neo4j import GraphDatabase
    from services.ai_service import generate_chat_completion
    prompt = f'''只從使用者本人的第一人稱句子抽取穩定交友偏好。忽略轉述、玩笑、假設、近期行程、配對狀態、其他使用者、帳號和敏感資訊。
只回 JSON：{{"memories":[{{"key":"英文snake_case","label":"繁中短標籤","stance":"like|dislike|require|avoid","category":"habit|lifestyle|personality|relationship|activity","confidence":0.0}}]}}。
只有信心 >=0.90 才輸出。句子：{text}'''
    data = json.loads(generate_chat_completion(prompt, temperature=0, json_output=True).content)
    items, clean, now = data.get("memories", [])[:3], [], time.time()
    uri, auth, database = _agent_graph_config()
    with GraphDatabase.driver(uri, auth=auth) as driver:
        with driver.session(database=database) as session:
            if message_id:
                observed = session.run("""
                    MERGE (o:MemoryObservation {message_id:$message_id})
                    ON CREATE SET o.owner_user_id=$user_id,o.created_at=$now
                    RETURN o.created_at=$now AS created
                """, message_id=message_id, user_id=user_id, now=now).single()
                if not observed or not observed["created"]:
                    return []
            for item in items:
                key = str(item.get("key", "")).strip().lower().replace(" ", "_")
                label = normalize_memory_item(item).get("label", "")[:40]
                stance = item.get("stance")
                confidence = float(item.get("confidence", 0))
                if not key or not label or stance not in {"like", "dislike", "require", "avoid"} or confidence < 0.90:
                    continue
                category = str(item.get("category", "lifestyle"))[:30]
                session.run("""
                    MERGE (u:User {id:$user_id}) MERGE (t:Trait {key:$key})
                    ON CREATE SET t.name=$label,t.category=$category
                    MERGE (u)-[r:HAS_PREFERENCE]->(t) ON CREATE SET r.first_seen_at=$now,r.evidence_count=0
                    SET r.stance=$stance,
                        r.type=CASE WHEN $stance IN ['dislike','avoid'] THEN 'DISLIKES_TRAIT' ELSE 'LIKES_TRAIT' END,
                        r.confidence=CASE WHEN coalesce(r.confidence,0)>$confidence THEN r.confidence ELSE $confidence END,
                        r.evidence_count=coalesce(r.evidence_count,0)+1,r.last_seen_at=$now,r.active=true,
                        r.source=$surface,r.display_label_zh_tw=$label
                """, user_id=user_id, key=key, label=label, category=category, stance=stance,
                     confidence=confidence, now=now, surface=surface).consume()
                clean.append({"key": key, "label": label, "stance": stance, "category": category,
                              "confidence": confidence, "last_seen_at": now})
    return clean


def _action_direct(user_id: str, key: str, action: str, value: str | None):
    from neo4j import GraphDatabase
    uri, auth, database = _agent_graph_config()
    with GraphDatabase.driver(uri, auth=auth) as driver:
        with driver.session(database=database) as session:
            row = session.run("""
                MATCH (u:User {id:$user_id})-[r:HAS_PREFERENCE]->(t:Trait {key:$key})
                SET r.active=$active,r.last_seen_at=$now,
                    r.display_label_zh_tw=CASE WHEN $value IS NULL OR $value='' THEN coalesce(r.display_label_zh_tw,t.name) ELSE $value END
                RETURN t.key AS key,coalesce(r.display_label_zh_tw,t.name) AS label,r.stance AS stance,
                       t.category AS category,r.confidence AS confidence,r.last_seen_at AS last_seen_at
            """, user_id=user_id, key=key, active=action != 'disable', now=time.time(),
                 value=normalize_zh_tw(value, max_length=40) if value else value).single()
            return {"status": "success", "memory": dict(row) if row else None}


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



def _apply_direct_proposals(user_id: str, proposals: list[dict], surface: str, message_id: str | None, match_id: str | None = None) -> list[dict]:
    """Fallback deterministic graph writer for a validated Profile Router decision."""
    from neo4j import GraphDatabase
    now = time.time()
    uri, auth, database = _agent_graph_config()
    with GraphDatabase.driver(uri, auth=auth) as driver:
        with driver.session(database=database) as session:
            if message_id:
                observed = session.run("""
                    MERGE (o:MemoryObservation {message_id:$message_id})
                    ON CREATE SET o.owner_user_id=$user_id,o.created_at=$now
                    RETURN o.created_at=$now AS created
                """, message_id=message_id, user_id=user_id, now=now).single()
                if not observed or not observed["created"]:
                    return []
            clean = []
            for item in proposals[:3]:
                key = str(item.get("key", "")).strip().lower()
                label = normalize_memory_item(item).get("label", "")
                stance = str(item.get("stance", ""))
                if not key or not label or stance not in {"like", "dislike", "require", "avoid"}:
                    continue
                confidence = float(item.get("confidence", 0))
                category = str(item.get("category", "lifestyle"))[:30]
                session.run("""
                    MERGE (u:User {id:$user_id}) MERGE (t:Trait {key:$key})
                    ON CREATE SET t.name=$label,t.category=$category
                    MERGE (u)-[r:HAS_PREFERENCE]->(t) ON CREATE SET r.first_seen_at=$now,r.evidence_count=0
                    SET r.stance=$stance,r.type=CASE WHEN $stance IN ['dislike','avoid'] THEN 'DISLIKES_TRAIT' ELSE 'LIKES_TRAIT' END,
                        r.confidence=CASE WHEN coalesce(r.confidence,0)>$confidence THEN r.confidence ELSE $confidence END,
                        r.evidence_count=coalesce(r.evidence_count,0)+1,r.last_seen_at=$now,r.active=true,
                        r.source=$surface,r.match_id=$match_id,r.display_label_zh_tw=$label
                """, user_id=user_id, key=key, label=label, stance=stance, confidence=confidence,
                     category=category, surface=surface, match_id=match_id, now=now).consume()
                clean.append({"key": key, "label": label, "stance": stance, "category": category,
                              "confidence": confidence, "last_seen_at": now})
    return clean


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
        try:
            learned = _apply_direct_proposals(user_id, proposals, surface, message_id, match_id)
        except Exception as exc:
            error_code = f"direct_graph_{type(exc).__name__}"
            _queue_memory_retry(user_id, proposals, surface, message_id, match_id, error_code)
            raise MemoryWriteError(error_code) from exc
    else:
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
def observe_user_memory(user_id: str, text: str, surface: str, match_id: str | None = None, message_id: str | None = None):
    """Persist only durable, owner-scoped preferences; raw chat text is never stored."""
    if profile_skills_mode_for_user(user_id) != "off" and not message_id:
        return []  # Legacy callers are intentionally suppressed once the skill owns observation.
    try:
        response = requests.post(f"{AGENT_URL}/api/memory/observe", json={
            "user_id": user_id, "text": text, "surface": surface,
            "match_id": match_id, "message_id": message_id,
        }, timeout=45)
        learned = _observe_direct(user_id, text, surface, message_id) if response.status_code == 404 else response.json().get("memories", [])
        if response.status_code != 404:
            response.raise_for_status()
        learned = [normalize_memory_item(item) for item in learned if normalize_memory_item(item).get("label")]
        if not learned:
            return []
        _sync_memory_projection(user_id, learned)
        events = [{"type": "memory_learned", "message": f"我記住了：{item['label']}。記錯的話可以在設定裡撤銷。",
                   "memory": item, "created_at": time.time()} for item in learned]
        profiles_coll.update_one({"user_id": user_id}, {"$push": {"memory_notices": {"$each": events}}}, upsert=True)
        return learned
    except Exception as exc:
        print(f"[MEMORY][observe] skipped user={user_id} error={exc}")
        return []


def observe_profile_memory(user_id: str, text: str, surface: str, message_id: str | None, match_id: str | None = None):
    """Memory skill entry point; shadow mode records decisions but never writes graph state."""
    mode = profile_skills_mode_for_user(user_id)
    allowed = memory_candidate_allowed(text)
    if mode == "off":
        return {"status": "disabled"}
    if mode == "shadow":
        print(f"[PROFILE_SKILL][memory] shadow user={user_id} allowed={allowed}")
        return {"status": "shadow", "allowed": allowed}
    if not allowed:
        return {"status": "skipped", "reason": "not_durable_or_not_first_person"}
    return {"status": "observed", "memories": observe_user_memory(user_id, text, surface, match_id, message_id)}


def apply_memory_action(user_id: str, key: str, action: str, value: str | None = None):
    response = requests.post(f"{AGENT_URL}/api/memory/action", json={"user_id": user_id, "key": key, "action": action, "value": value}, timeout=30)
    result = _action_direct(user_id, key, action, value) if response.status_code == 404 else response.json()
    if response.status_code != 404:
        response.raise_for_status()
    _sync_memory_projection(user_id, [])
    return result

