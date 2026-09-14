"""Owner-only, person-scoped relationship memories for Ayue surfaces."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from typing import Any

from pymongo import ReturnDocument
from bson.objectid import ObjectId

from database import db, matches_coll, messages_coll, profiles_coll
from services.ai_service import generate_chat_completion
from services.chat_service import save_system_message_once
from services.match_state_service import verified_accepted_match_query
from services.risk_block_service import risk_block_service


RELATIONSHIP_MEMORIES = db["relationship_memories"]
RELATIONSHIP_MEMORY_OUTBOX = db["relationship_memory_outbox"]
RELATIONSHIP_MEMORY_SHADOW_RUNS = db["relationship_memory_shadow_runs"]
RELATIONSHIP_MEMORY_SUPPRESSIONS = db["relationship_memory_suppressions"]

_STOP_EVENT = threading.Event()
_THREAD: threading.Thread | None = None
_NO_MEMORY_RE = re.compile(
    r"(?:這(?:段|個)?不要記|不要記(?:這段|這個)?|別記|不用記|不必記|"
    r"不想聊(?:這個)?|不方便說|先跳過|略過|不回答)"
)
_SUBJECTIVE_RE = re.compile(r"^(?:我|本人)(?:覺得|感覺|在意|希望|想要|不喜歡|喜歡|需要|擔心)")
_INTERNAL_ID_RE = re.compile(r"(?:@?seed_user_[\w-]+|@?demo_user|@?user[_-]?\d+)", re.IGNORECASE)


def relationship_memory_mode() -> str:
    mode = os.getenv("AYUE_RELATIONSHIP_MEMORY_MODE", "shadow").strip().lower()
    return mode if mode in {"off", "shadow", "on"} else "off"


def ensure_indexes() -> None:
    try:
        RELATIONSHIP_MEMORIES.create_index(
            [("owner_user_id", 1), ("other_user_id", 1), ("status", 1), ("updated_at", -1)]
        )
        RELATIONSHIP_MEMORIES.create_index(
            [("owner_user_id", 1), ("other_user_id", 1), ("topic_key", 1)],
            unique=True,
        )
        RELATIONSHIP_MEMORY_OUTBOX.create_index([("status", 1), ("next_attempt_at", 1)])
        RELATIONSHIP_MEMORY_OUTBOX.create_index("source_message_id", unique=True)
        RELATIONSHIP_MEMORY_SHADOW_RUNS.create_index("source_message_id", unique=True)
        RELATIONSHIP_MEMORY_SHADOW_RUNS.create_index("created_at", expireAfterSeconds=30 * 86400)
        RELATIONSHIP_MEMORY_SUPPRESSIONS.create_index("source_message_id", unique=True)
    except Exception as exc:
        print(f"Relationship memory index setup skipped: {type(exc).__name__}")


def _record_id(owner_user_id: str, other_user_id: str, topic_key: str) -> str:
    raw = f"{owner_user_id}\x00{other_user_id}\x00{topic_key}".encode()
    return "relationship-memory:" + hashlib.sha256(raw).hexdigest()


def _safe_text(value: Any, limit: int) -> str:
    text = _INTERNAL_ID_RE.sub("對方", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _subjective_statement(value: Any) -> str:
    statement = _safe_text(value, 300)
    if statement and not _SUBJECTIVE_RE.search(statement):
        statement = "本人覺得「" + statement.strip("。") + "」"
    return statement[:300]


def _message_id_query(source_message_id: str) -> dict[str, Any]:
    values: list[Any] = [source_message_id]
    try:
        values.append(ObjectId(source_message_id))
    except Exception:
        pass
    return {"_id": {"$in": values}}


def _active_match(owner_user_id: str, other_user_id: str) -> dict | None:
    try:
        match = matches_coll.find_one(verified_accepted_match_query(owner_user_id, other_user_id))
        if not match or risk_block_service.is_pair_blocked(owner_user_id, other_user_id):
            return None
        return match
    except Exception:
        return None


def list_relationship_memories(
    owner_user_id: str, other_user_id: str | None = None, *, include_deleted: bool = False,
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"owner_user_id": owner_user_id}
    if other_user_id:
        query["other_user_id"] = other_user_id
    if not include_deleted:
        query["status"] = "active"
    rows = list(RELATIONSHIP_MEMORIES.find(query).sort("updated_at", -1).limit(100))
    return [
        {
            "memory_id": str(row.get("_id") or ""),
            "other_user_id": str(row.get("other_user_id") or ""),
            "relationship_id": str(row.get("relationship_id") or ""),
            "topic": _safe_text(row.get("topic"), 60),
            "statement": _safe_text(row.get("statement"), 300),
            "version": int(row.get("version", 1) or 1),
            "updated_at": float(row.get("updated_at", 0) or 0),
            "status": str(row.get("status") or "active"),
        }
        for row in rows
    ]


def relationship_memory_context(owner_user_id: str, other_user_id: str) -> list[dict[str, Any]]:
    """Return a minimal owner-only projection only while the relationship is valid."""
    if not _active_match(owner_user_id, other_user_id):
        return []
    return [
        {
            "topic": item["topic"],
            "owner_view": item["statement"],
            "updated_at": item["updated_at"],
            "source": "owner_private_relationship_memory",
        }
        for item in list_relationship_memories(owner_user_id, other_user_id)[:8]
    ]


def update_relationship_memory(
    owner_user_id: str,
    memory_id: str,
    *,
    expected_version: int,
    statement: str | None = None,
    delete: bool = False,
) -> dict[str, Any] | None:
    current = RELATIONSHIP_MEMORIES.find_one({"_id": memory_id, "owner_user_id": owner_user_id})
    if not current or int(current.get("version", 1) or 1) != int(expected_version):
        return None
    now = time.time()
    if delete:
        update = {
            "$set": {"status": "deleted", "updated_at": now, "version": expected_version + 1},
            "$unset": {"statement": "", "evidence_span": "", "revisions": ""},
        }
        source_id = str(current.get("source_message_id") or "")
        if source_id:
            RELATIONSHIP_MEMORY_SUPPRESSIONS.update_one(
                {"source_message_id": source_id},
                {"$setOnInsert": {"owner_user_id": owner_user_id, "created_at": now}},
                upsert=True,
            )
    else:
        normalized = _subjective_statement(statement)
        if not normalized:
            return None
        update = {
            "$set": {"statement": normalized, "updated_at": now, "version": expected_version + 1},
            "$push": {"revisions": {
                "$each": [{"statement": current.get("statement", ""), "replaced_at": now}],
                "$slice": -12,
            }},
        }
    row = RELATIONSHIP_MEMORIES.find_one_and_update(
        {"_id": memory_id, "owner_user_id": owner_user_id, "version": expected_version},
        update,
        return_document=ReturnDocument.AFTER,
    )
    if not row:
        return None
    return {
        "memory_id": str(row.get("_id") or ""),
        "other_user_id": str(row.get("other_user_id") or ""),
        "relationship_id": str(row.get("relationship_id") or ""),
        "topic": _safe_text(row.get("topic"), 60),
        "statement": _safe_text(row.get("statement"), 300),
        "version": int(row.get("version", 1) or 1),
        "updated_at": float(row.get("updated_at", 0) or 0),
        "status": str(row.get("status") or "active"),
    }


def enqueue_relationship_memory_extraction(
    owner_user_id: str,
    other_user_id: str,
    relationship_id: str,
    source_message_id: str,
    *,
    date_event_id: str | None = None,
) -> bool:
    if not source_message_id or relationship_memory_mode() == "off":
        return False
    source = messages_coll.find_one(
        {**_message_id_query(source_message_id), "sender_id": owner_user_id},
        {"content": 1, "room_id": 1},
    )
    if not source:
        return False
    content = _safe_text(source.get("content"), 2000)
    if _NO_MEMORY_RE.search(content):
        RELATIONSHIP_MEMORY_SUPPRESSIONS.update_one(
            {"source_message_id": source_message_id},
            {"$setOnInsert": {"owner_user_id": owner_user_id, "created_at": time.time()}},
            upsert=True,
        )
        return False
    if str(source.get("room_id") or "") != f"mediator_private::{owner_user_id}::{other_user_id}":
        return False
    result = RELATIONSHIP_MEMORY_OUTBOX.update_one(
        {"source_message_id": source_message_id},
        {"$setOnInsert": {
            "owner_user_id": owner_user_id,
            "other_user_id": other_user_id,
            "relationship_id": relationship_id,
            "date_event_id": date_event_id,
            "status": "pending",
            "attempts": 0,
            "next_attempt_at": time.time(),
            "created_at": time.time(),
        }},
        upsert=True,
    )
    return bool(getattr(result, "upserted_id", None))


def _extract(job: dict[str, Any], source: dict[str, Any]) -> dict[str, Any] | None:
    owner = str(job.get("owner_user_id") or "")
    other = str(job.get("other_user_id") or "")
    existing = list_relationship_memories(owner, other)[:8]
    slots = [{"slot": i + 1, "topic": row["topic"], "statement": row["statement"]} for i, row in enumerate(existing)]
    content = _safe_text(source.get("content"), 2000)
    prompt = f"""
你只判斷本人是否明確表達對特定對象的感受、在意事項、期待或界線。無關話題、模糊短答、一般知識、工具要求或對對方人格的猜測一律 none。
摘要必須表達為本人的主觀看法，不得把感受寫成對方的客觀事實。evidence_span 必須是訊息中的原文子字串。
現有記憶：{json.dumps(slots, ensure_ascii=False)}
本人訊息：{content}
只輸出 JSON：{{"action":"create|update|none","existing_slot":null,"topic":"","statement":"","evidence_span":"","confidence":0.0}}
"""
    try:
        raw = generate_chat_completion(prompt, temperature=0, json_output=True)
        payload = json.loads(str(getattr(raw, "content", raw)))
    except Exception:
        return None
    action = str(payload.get("action") or "none")
    evidence = _safe_text(payload.get("evidence_span"), 300)
    statement = _safe_text(payload.get("statement"), 300)
    topic = _safe_text(payload.get("topic"), 60)
    try:
        confidence = float(payload.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        confidence = 0
    if action not in {"create", "update"} or confidence < 0.9 or not evidence or evidence not in content:
        return {"action": "none", "reason": "not_explicit"}
    if not topic or not statement:
        return {"action": "none", "reason": "invalid_output"}
    statement = _subjective_statement(statement)
    slot = payload.get("existing_slot")
    if action == "update":
        try:
            bound = existing[int(slot) - 1]
        except (TypeError, ValueError, IndexError):
            return {"action": "none", "reason": "stale_slot"}
        topic = bound["topic"]
    return {"action": action, "topic": topic, "statement": statement, "evidence_span": evidence}


def process_relationship_memory_outbox_once(*, now: float | None = None) -> dict[str, int]:
    current = time.time() if now is None else float(now)
    stats = {"scanned": 0, "saved": 0, "shadowed": 0, "skipped": 0, "retried": 0}
    mode = relationship_memory_mode()
    if mode == "off":
        return stats
    token = uuid.uuid4().hex
    job = RELATIONSHIP_MEMORY_OUTBOX.find_one_and_update(
        {"$or": [
            {"status": "pending", "next_attempt_at": {"$lte": current}},
            {"status": "processing", "lease_until": {"$lte": current}},
        ]},
        {"$set": {"status": "processing", "lease_token": token, "lease_until": current + 90}},
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if not job:
        return stats
    stats["scanned"] = 1
    source_id = str(job.get("source_message_id") or "")
    try:
        if RELATIONSHIP_MEMORY_SUPPRESSIONS.find_one({"source_message_id": source_id}):
            raise ValueError("suppressed")
        owner, other = str(job.get("owner_user_id") or ""), str(job.get("other_user_id") or "")
        match = _active_match(owner, other)
        source = messages_coll.find_one({**_message_id_query(source_id), "sender_id": owner})
        if not match or not source or str(match.get("_id")) != str(job.get("relationship_id") or ""):
            raise ValueError("invalid_scope")
        decision = _extract(job, source)
        if decision is None:
            raise RuntimeError("provider_error")
        if decision.get("action") == "none":
            stats["skipped"] = 1
        elif mode == "shadow":
            RELATIONSHIP_MEMORY_SHADOW_RUNS.update_one(
                {"source_message_id": source_id},
                {"$setOnInsert": {"outcome": decision["action"], "created_at": current}},
                upsert=True,
            )
            stats["shadowed"] = 1
        else:
            topic_key = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "-", decision["topic"].lower()).strip("-")[:60]
            if not topic_key:
                topic_key = hashlib.sha256(decision["topic"].encode()).hexdigest()[:24]
            memory_id = _record_id(owner, other, topic_key)
            previous = RELATIONSHIP_MEMORIES.find_one({"_id": memory_id}) or {}
            version = int(previous.get("version", 0) or 0) + 1
            update: dict[str, Any] = {
                "$set": {
                    "owner_user_id": owner, "other_user_id": other,
                    "relationship_id": str(match.get("_id")), "topic_key": topic_key,
                    "topic": decision["topic"], "statement": decision["statement"],
                    "evidence_span": decision["evidence_span"], "source_message_id": source_id,
                    "source_at": float(source.get("timestamp", current) or current),
                    "date_event_id": job.get("date_event_id"), "version": version,
                    "status": "active", "updated_at": current,
                },
                "$setOnInsert": {"created_at": current},
            }
            if previous.get("statement"):
                update["$push"] = {"revisions": {
                    "$each": [{"statement": previous["statement"], "replaced_at": current}],
                    "$slice": -12,
                }}
            RELATIONSHIP_MEMORIES.update_one({"_id": memory_id}, update, upsert=True)
            save_system_message_once(
                f"mediator_private::{owner}::{other}",
                "我記下你對這段關係的想法了；你可以到「阿月記住的事」查看或撤銷。",
                metadata={"event_type": "relationship_memory_saved", "notification_eligible": False},
                event_key=f"relationship-memory-saved:{source_id}",
            )
            stats["saved"] = 1
        RELATIONSHIP_MEMORY_OUTBOX.update_one(
            {"_id": job["_id"], "lease_token": token},
            {"$set": {"status": "completed", "completed_at": current}, "$unset": {"lease_token": "", "lease_until": ""}},
        )
    except ValueError as exc:
        RELATIONSHIP_MEMORY_OUTBOX.update_one(
            {"_id": job["_id"], "lease_token": token},
            {"$set": {"status": "skipped", "outcome": str(exc), "completed_at": current}, "$unset": {"lease_token": "", "lease_until": ""}},
        )
        stats["skipped"] = 1
    except Exception:
        attempts = int(job.get("attempts", 0) or 0) + 1
        RELATIONSHIP_MEMORY_OUTBOX.update_one(
            {"_id": job["_id"], "lease_token": token},
            {"$set": {"status": "pending" if attempts < 3 else "failed", "attempts": attempts, "next_attempt_at": current + min(900, 60 * attempts)}, "$unset": {"lease_token": "", "lease_until": ""}},
        )
        stats["retried"] = 1
    return stats


def _worker_loop() -> None:
    while not _STOP_EVENT.wait(10):
        try:
            stats = process_relationship_memory_outbox_once()
            if stats.get("scanned"):
                print(
                    "[RELATIONSHIP_MEMORY] "
                    + " ".join(f"{key}={int(value)}" for key, value in stats.items())
                )
        except Exception as exc:
            print(f"Relationship memory worker skipped: {type(exc).__name__}")


def start_relationship_memory_worker() -> None:
    global _THREAD
    if relationship_memory_mode() == "off":
        return
    if _THREAD and _THREAD.is_alive():
        return
    _STOP_EVENT.clear()
    _THREAD = threading.Thread(target=_worker_loop, name="relationship-memory", daemon=True)
    _THREAD.start()


def stop_relationship_memory_worker() -> None:
    _STOP_EVENT.set()
