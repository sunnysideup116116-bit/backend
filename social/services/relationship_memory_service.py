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
from pymongo.errors import DuplicateKeyError
from bson.objectid import ObjectId

from database import db, matches_coll, messages_coll, mongo_client, profiles_coll
from services.ai_service import generate_chat_completion
from services.match_state_service import verified_accepted_match_query
from services.risk_block_service import risk_block_service


RELATIONSHIP_MEMORIES = db["relationship_memories"]
RELATIONSHIP_MEMORY_OUTBOX = db["relationship_memory_outbox"]
RELATIONSHIP_MEMORY_SHADOW_RUNS = db["relationship_memory_shadow_runs"]
RELATIONSHIP_MEMORY_SUPPRESSIONS = db["relationship_memory_suppressions"]

_STOP_EVENT = threading.Event()
_THREAD: threading.Thread | None = None
_NO_MEMORY_RE = re.compile(
    r"^\s*(?:阿月[，,\s]*)?(?:這(?:段|個)?不要記|不要記(?:這段|這個)?|"
    r"別記|不用記|不必記|(?:我)?不想聊(?:這個)?|(?:我)?不方便說|"
    r"(?:我)?先跳過|(?:我)?略過|(?:我)?不回答)(?:[。！!，,\s]|$)"
)
_SUBJECTIVE_RE = re.compile(
    r"(?:^|[，。！？!?\s])(?:我|本人)[^。！？!?]{0,40}?"
    r"(?:覺得|感覺|在意|希望|期待|想(?:要)?|不喜歡|喜歡|需要|擔心)",
)
_INTERNAL_ID_RE = re.compile(r"(?:@?seed_user_[\w-]+|@?demo_user|@?user[_-]?\d+)", re.IGNORECASE)
_CATEGORY_TOPICS = {
    "impression": "相處感受",
    "preference": "關係偏好",
    "boundary": "相處界線",
    "future_intent": "未來期待",
}
_MEMORY_CATEGORIES = frozenset(_CATEGORY_TOPICS)
_MERGE_ACTIONS = frozenset({"add", "merge", "replace", "retract", "no_change"})


class _MemoryVersionConflict(RuntimeError):
    pass


class MemoryEnqueueResult(dict):
    """Structured enqueue result with bool compatibility for old callers.

    Existing integrations treated ``enqueue_relationship_memory_extraction``
    as a boolean.  Keeping truthiness for the two accepted queue states lets
    those integrations survive while the Private runtime can distinguish an
    already-queued job, a normal skip, and a durable write failure.
    """

    _SUCCESS = frozenset({"queued", "already_queued", "already_saved"})

    def __init__(self, status: str, *, reason: str = "", **fields: Any):
        super().__init__(status=status, reason=reason, **fields)

    def __bool__(self) -> bool:
        return str(self.get("status") or "") in self._SUCCESS

    @property
    def status(self) -> str:
        return str(self.get("status") or "")


def _enqueue_result(status: str, reason: str = "", **fields: Any) -> MemoryEnqueueResult:
    return MemoryEnqueueResult(status, reason=reason, **fields)


def _existing_enqueue_result(existing: dict[str, Any]) -> MemoryEnqueueResult:
    status = str(existing.get("status") or "")
    outcome = str(existing.get("outcome") or "")
    common = {
        "outbox_status": status,
        "outcome": outcome or None,
    }
    if status in {"pending", "processing"}:
        return _enqueue_result("already_queued", "source_already_present", **common)
    if status == "completed" and outcome == "saved":
        return _enqueue_result("already_saved", "source_already_saved", **common)
    if status == "failed":
        return _enqueue_result("failed", "previous_job_failed", **common)
    return _enqueue_result("skipped", outcome or "completed_outcome_unknown", **common)


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


def _facet_id(
    owner_user_id: str,
    other_user_id: str,
    category: str,
    source_message_id: str,
    evidence_span: str,
) -> str:
    raw = "\x00".join((
        owner_user_id, other_user_id, category, source_message_id, evidence_span,
    )).encode()
    return "relationship-facet:" + hashlib.sha256(raw).hexdigest()[:32]


def _safe_text(value: Any, limit: int) -> str:
    text = _INTERNAL_ID_RE.sub("對方", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _subjective_statement(value: Any) -> str:
    statement = _safe_text(value, 300)
    if (
        statement
        and not _SUBJECTIVE_RE.search(statement)
        and not re.match(r"^(?:我|本人)", statement)
    ):
        statement = "我覺得" + statement.strip("。")
    return statement[:300]


def _statement_matches_evidence(statement: str, evidence: str) -> bool:
    def comparable(value: str) -> str:
        text = re.sub(r"[\s「」『』\"'，,。.!！?？]", "", value)
        for _ in range(2):
            text = re.sub(
                r"^(?:本人|我)(?:覺得|感覺|希望|期待|想要|想)?",
                "",
                text,
            )
        return text

    left = comparable(statement)
    right = comparable(evidence)
    return bool(left and right and left == right)


def _distill_candidate_statement(
    *, evidence: str, category: str, suggested: str = "",
) -> str | None:
    prompt = f"""
把使用者對目前對象的原話整理成一則可長期閱讀的關係記憶。
只保留一個核心觀點，使用自然的第一人稱，最多 45 個中文字；不得逐字複製原句、不得加引號、不得寫成對方的客觀事實。
類別：{category}
原文證據：{evidence}
Agent 建議：{suggested}
只輸出 JSON：{{"statement":""}}
"""
    try:
        raw = generate_chat_completion(prompt, temperature=0, json_output=True)
        payload = json.loads(str(getattr(raw, "content", raw)))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    statement = _subjective_statement(_safe_text(payload.get("statement"), 120))
    if not statement or _statement_matches_evidence(statement, evidence):
        return None
    return statement[:120]


def _row_facets(row: dict[str, Any]) -> list[dict[str, Any]]:
    facets: list[dict[str, Any]] = []
    for item in row.get("facets") or []:
        if not isinstance(item, dict):
            continue
        text = _safe_text(item.get("text"), 300)
        facet_id = _safe_text(item.get("facet_id"), 100)
        if not text or not facet_id:
            continue
        facets.append({
            "facet_id": facet_id,
            "text": text,
            "source_message_id": _safe_text(item.get("source_message_id"), 100),
            "evidence_span": _safe_text(item.get("evidence_span"), 300),
            "source_at": float(item.get("source_at", row.get("source_at", 0)) or 0),
            "updated_at": float(item.get("updated_at", row.get("updated_at", 0)) or 0),
        })
    if facets:
        return facets
    statement = _safe_text(row.get("statement"), 300)
    if not statement:
        return []
    source_id = _safe_text(row.get("source_message_id"), 100)
    return [{
        "facet_id": _facet_id(
            str(row.get("owner_user_id") or ""),
            str(row.get("other_user_id") or ""),
            str(row.get("category") or "legacy"),
            source_id or str(row.get("_id") or "legacy"),
            _safe_text(row.get("evidence_span"), 300) or statement,
        ),
        "text": statement,
        "source_message_id": source_id,
        "evidence_span": _safe_text(row.get("evidence_span"), 300),
        "source_at": float(row.get("source_at", row.get("updated_at", 0)) or 0),
        "updated_at": float(row.get("updated_at", 0) or 0),
    }]


def _render_facets(facets: list[dict[str, Any]]) -> str:
    return "、".join(
        text for text in (_safe_text(item.get("text"), 300) for item in facets)
        if text
    )


def _project_memory(row: dict[str, Any]) -> dict[str, Any]:
    facets = _row_facets(row)
    return {
        "memory_id": str(row.get("_id") or ""),
        "other_user_id": str(row.get("other_user_id") or ""),
        "relationship_id": str(row.get("relationship_id") or ""),
        "category": _safe_text(row.get("category"), 40),
        "topic": _safe_text(row.get("topic"), 60),
        "statement": _render_facets(facets) or _safe_text(row.get("statement"), 2000),
        "schema_version": 2 if row.get("facets") else int(row.get("schema_version", 1) or 1),
        "facets": [
            {
                "facet_id": item["facet_id"],
                "text": item["text"],
                "updated_at": item["updated_at"],
            }
            for item in facets
        ],
        "version": int(row.get("version", 1) or 1),
        "updated_at": float(row.get("updated_at", 0) or 0),
        "status": str(row.get("status") or "active"),
    }


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
    return [_project_memory(row) for row in rows]


def relationship_memory_context(owner_user_id: str, other_user_id: str) -> list[dict[str, Any]]:
    """Return a minimal owner-only projection only while the relationship is valid."""
    if not _active_match(owner_user_id, other_user_id):
        return []
    return [
        {
            "topic": item["topic"],
            "owner_view": item["statement"],
            "views": [
                {
                    "text": facet["text"],
                    "updated_at": facet["updated_at"],
                }
                for facet in item["facets"]
            ],
            "updated_at": item["updated_at"],
            "source": "owner_private_relationship_memory",
        }
        for item in list_relationship_memories(owner_user_id, other_user_id)[:8]
    ]


def relationship_memory_access_allowed(owner_user_id: str, other_user_id: str) -> bool:
    """Revalidate the accepted, unblocked pair before any owner-memory read."""
    return _active_match(owner_user_id, other_user_id) is not None


def update_relationship_memory(
    owner_user_id: str,
    memory_id: str,
    *,
    expected_version: int,
    statement: str | None = None,
    delete: bool = False,
    facet_id: str | None = None,
) -> dict[str, Any] | None:
    current = RELATIONSHIP_MEMORIES.find_one({"_id": memory_id, "owner_user_id": owner_user_id})
    if not current or int(current.get("version", 1) or 1) != int(expected_version):
        return None
    now = time.time()
    suppression_source_ids: set[str] = set()
    target_facet_id = _safe_text(facet_id, 100)
    current_facets = _row_facets(current)
    if target_facet_id:
        target = next(
            (item for item in current_facets if item["facet_id"] == target_facet_id),
            None,
        )
        if target is None:
            return None
        if delete:
            next_facets = [
                item for item in current_facets if item["facet_id"] != target_facet_id
            ]
            source_id = str(target.get("source_message_id") or "")
            if source_id:
                suppression_source_ids.add(source_id)
        else:
            normalized = _subjective_statement(statement)
            if not normalized:
                return None
            next_facets = [
                {
                    **item,
                    "text": normalized,
                    "updated_at": now,
                }
                if item["facet_id"] == target_facet_id else item
                for item in current_facets
            ]
        update = {
            "$set": {
                "schema_version": 2,
                "facets": next_facets,
                "statement": _render_facets(next_facets),
                "updated_at": now,
                "version": expected_version + 1,
                "status": "active" if next_facets else "deleted",
            },
            "$push": {"revisions": {
                "$each": [{
                    "statement": current.get("statement", ""),
                    "facets": current_facets,
                    "replaced_at": now,
                }],
                "$slice": -12,
            }},
        }
    elif delete:
        update = {
            "$set": {"status": "deleted", "updated_at": now, "version": expected_version + 1},
            "$unset": {
                "statement": "",
                "facets": "",
                "evidence_span": "",
                "revisions": "",
            },
        }
        source_ids = {
            str(item.get("source_message_id") or "") for item in current_facets
        }
        source_ids.add(str(current.get("source_message_id") or ""))
        suppression_source_ids.update(source_ids - {""})
    else:
        normalized = _subjective_statement(statement)
        if not normalized:
            return None
        replacement = [{
            "facet_id": _facet_id(
                owner_user_id,
                str(current.get("other_user_id") or ""),
                str(current.get("category") or "manual"),
                f"manual:{expected_version + 1}",
                normalized,
            ),
            "text": normalized,
            "source_message_id": "",
            "evidence_span": "",
            "source_at": now,
            "updated_at": now,
        }]
        update = {
            "$set": {
                "schema_version": 2,
                "facets": replacement,
                "statement": normalized,
                "updated_at": now,
                "version": expected_version + 1,
            },
            "$push": {"revisions": {
                "$each": [{
                    "statement": current.get("statement", ""),
                    "facets": current_facets,
                    "replaced_at": now,
                }],
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
    for source_id in suppression_source_ids:
        RELATIONSHIP_MEMORY_SUPPRESSIONS.update_one(
            {"source_message_id": source_id},
            {"$setOnInsert": {"owner_user_id": owner_user_id, "created_at": now}},
            upsert=True,
        )
    return _project_memory(row)


def _memory_evidence_allowed(content: str, evidence: str) -> bool:
    """Verify provenance only; the context-aware Private agent owns semantics."""
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    span = re.sub(r"\s+", " ", str(evidence or "")).strip()
    return bool(text and span and span in text)


def enqueue_relationship_memory_extraction_result(
    owner_user_id: str,
    other_user_id: str,
    relationship_id: str,
    source_message_id: str,
    *,
    date_event_id: str | None = None,
    category: str | None = None,
    evidence_span: str | None = None,
    statement: str | None = None,
    candidates: list[dict[str, Any]] | None = None,
) -> MemoryEnqueueResult:
    """Durably enqueue one owner-scoped relationship-memory candidate.

    This is intentionally safe to call from the synchronous Private tool
    callback.  The callback only records the source message; the outbox worker
    remains the authority that extracts and saves a memory.  The unique source
    id makes retries idempotent, including a retry after a later reply
    validation or provider failure.
    """
    if not source_message_id or relationship_memory_mode() == "off":
        return _enqueue_result(
            "skipped", "memory_disabled" if relationship_memory_mode() == "off" else "missing_source_message_id",
        )
    try:
        source = messages_coll.find_one(
            {**_message_id_query(source_message_id), "sender_id": owner_user_id},
            {"content": 1, "room_id": 1, "timestamp": 1},
        )
    except Exception as exc:
        return _enqueue_result("failed", type(exc).__name__)
    if not source:
        return _enqueue_result("skipped", "source_message_not_found")
    content = _safe_text(source.get("content"), 2000)
    raw_candidates = list(candidates or [])
    if not raw_candidates and any(
        value is not None for value in (category, evidence_span, statement)
    ):
        raw_candidates = [{
            "category": category,
            "evidence_span": evidence_span,
            "statement": statement,
        }]
    safe_candidates: list[dict[str, str]] = []
    for item in raw_candidates[:8]:
        if not isinstance(item, dict):
            return _enqueue_result("skipped", "invalid_candidate")
        safe_category = _safe_text(item.get("category"), 40)
        safe_evidence = _safe_text(item.get("evidence_span"), 300)
        safe_statement = _subjective_statement(_safe_text(item.get("statement"), 120))
        if safe_category not in _MEMORY_CATEGORIES:
            return _enqueue_result("skipped", "invalid_category")
        if not _memory_evidence_allowed(content, safe_evidence):
            return _enqueue_result("skipped", "not_explicit")
        if not safe_statement:
            return _enqueue_result("skipped", "missing_statement")
        candidate_value = {
            "category": safe_category,
            "evidence_span": safe_evidence,
            "statement": safe_statement,
        }
        if candidate_value not in safe_candidates:
            safe_candidates.append(candidate_value)
    if _NO_MEMORY_RE.search(content):
        try:
            RELATIONSHIP_MEMORY_SUPPRESSIONS.update_one(
                {"source_message_id": source_message_id},
                {"$setOnInsert": {"owner_user_id": owner_user_id, "created_at": time.time()}},
                upsert=True,
            )
        except Exception as exc:
            return _enqueue_result("failed", type(exc).__name__)
        return _enqueue_result("skipped", "suppressed")
    if str(source.get("room_id") or "") != f"mediator_private::{owner_user_id}::{other_user_id}":
        return _enqueue_result("skipped", "invalid_scope")
    try:
        match = _active_match(owner_user_id, other_user_id)
    except Exception:
        match = None
    if not match or str(match.get("_id") or "") != str(relationship_id or ""):
        return _enqueue_result("skipped", "invalid_scope")

    try:
        existing = RELATIONSHIP_MEMORY_OUTBOX.find_one(
            {"source_message_id": source_message_id},
            {"status": 1, "outcome": 1, "attempts": 1},
        )
    except Exception as exc:
        return _enqueue_result("failed", type(exc).__name__)
    if existing:
        return _existing_enqueue_result(existing)

    now = time.time()
    document: dict[str, Any] = {
        "owner_user_id": owner_user_id,
        "other_user_id": other_user_id,
        "relationship_id": relationship_id,
        "date_event_id": date_event_id,
        "source_message_id": source_message_id,
        "status": "pending",
        "outcome": None,
        "attempts": 0,
        "next_attempt_at": now,
        "created_at": now,
        "write_status": "pending",
        "notification_status": "pending",
    }
    if safe_candidates:
        document["candidates"] = safe_candidates
        document["candidate"] = safe_candidates[0]
    try:
        result = RELATIONSHIP_MEMORY_OUTBOX.update_one(
            {"source_message_id": source_message_id},
            {"$setOnInsert": document},
            upsert=True,
        )
    except DuplicateKeyError:
        try:
            existing = RELATIONSHIP_MEMORY_OUTBOX.find_one(
                {"source_message_id": source_message_id},
                {"status": 1, "outcome": 1, "attempts": 1},
            )
        except Exception:
            existing = None
        return (
            _existing_enqueue_result(existing)
            if existing else _enqueue_result("already_queued", "source_already_present")
        )
    except Exception as exc:
        return _enqueue_result("failed", type(exc).__name__)
    if getattr(result, "upserted_id", None) is not None:
        return _enqueue_result("queued", "durable_outbox_inserted")
    # A concurrent writer may have inserted the same source between the
    # initial read and update.  Re-read before reporting a hard failure.
    try:
        existing = RELATIONSHIP_MEMORY_OUTBOX.find_one(
            {"source_message_id": source_message_id},
            {"status": 1, "outcome": 1},
        )
    except Exception as exc:
        return _enqueue_result("failed", type(exc).__name__)
    if existing:
        return _existing_enqueue_result(existing)
    # The in-process test collection does not implement Mongo's upsert
    # contract.  Its explicit insert fallback preserves the same durable
    # behavior without changing the production atomic update path.
    if int(getattr(result, "modified_count", 0) or 0) == 0 and hasattr(
        RELATIONSHIP_MEMORY_OUTBOX, "insert_one"
    ):
        try:
            RELATIONSHIP_MEMORY_OUTBOX.insert_one(document)
            return _enqueue_result("queued", "durable_outbox_inserted")
        except DuplicateKeyError:
            return _enqueue_result("already_queued", "source_already_present")
        except Exception as exc:
            return _enqueue_result("failed", type(exc).__name__)
    return _enqueue_result("failed", "outbox_insert_not_confirmed")


def enqueue_relationship_memory_extraction(
    owner_user_id: str,
    other_user_id: str,
    relationship_id: str,
    source_message_id: str,
    *,
    date_event_id: str | None = None,
    category: str | None = None,
    evidence_span: str | None = None,
    statement: str | None = None,
    candidates: list[dict[str, Any]] | None = None,
) -> MemoryEnqueueResult:
    """Compatibility entry point returning a structured, bool-compatible result."""
    result = enqueue_relationship_memory_extraction_result(
        owner_user_id,
        other_user_id,
        relationship_id,
        source_message_id,
        date_event_id=date_event_id,
        category=category,
        evidence_span=evidence_span,
        statement=statement,
        candidates=candidates,
    )
    return result


def _extract(job: dict[str, Any], source: dict[str, Any]) -> dict[str, Any] | None:
    owner = str(job.get("owner_user_id") or "")
    other = str(job.get("other_user_id") or "")
    existing = list_relationship_memories(owner, other)[:8]
    slots = [{"slot": i + 1, "topic": row["topic"], "statement": row["statement"]} for i, row in enumerate(existing)]
    content = _safe_text(source.get("content"), 2000)
    raw_candidates = job.get("candidates")
    if not isinstance(raw_candidates, list):
        legacy_candidate = job.get("candidate")
        raw_candidates = [legacy_candidate] if isinstance(legacy_candidate, dict) else []
    candidates: list[dict[str, str]] = []
    for item in raw_candidates[:8]:
        if not isinstance(item, dict):
            return {"action": "none", "reason": "invalid_output"}
        evidence = _safe_text(item.get("evidence_span"), 300)
        category = _safe_text(item.get("category"), 40)
        statement = _subjective_statement(_safe_text(item.get("statement"), 120))
        if category not in _MEMORY_CATEGORIES or not statement:
            return {"action": "none", "reason": "invalid_output"}
        if not _memory_evidence_allowed(content, evidence):
            return {"action": "none", "reason": "not_explicit"}
        value = {
            "category": category,
            "topic": _CATEGORY_TOPICS[category],
            "statement": statement,
            "evidence_span": evidence,
        }
        if value not in candidates:
            candidates.append(value)
    if candidates:
        return {"action": "merge_batch", "candidates": candidates}
    prompt = f"""
你只判斷本人是否明確表達對特定對象的感受、在意事項、期待或界線。無關話題、模糊短答、一般知識、工具要求或對對方人格的猜測一律 none。
摘要必須表達為本人的主觀看法，不得把感受寫成對方的客觀事實。evidence_span 必須是訊息中的原文子字串。
現有記憶：{json.dumps(slots, ensure_ascii=False)}
Server 候選：[]
本人訊息：{content}
只輸出 JSON：{{"action":"create|update|none","existing_slot":null,"topic":"","statement":"","evidence_span":"","confidence":0.0}}
"""
    try:
        raw = generate_chat_completion(prompt, temperature=0, json_output=True)
        payload = json.loads(str(getattr(raw, "content", raw)))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return {"action": "none", "reason": "invalid_output"}
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
    if not _memory_evidence_allowed(content, evidence):
        return {"action": "none", "reason": "not_explicit"}
    if not topic or not statement:
        return {"action": "none", "reason": "invalid_output"}
    statement = _subjective_statement(statement)
    slot = payload.get("existing_slot")
    if action == "update":
        if isinstance(slot, bool) or (
            isinstance(slot, float) and not slot.is_integer()
        ):
            return {"action": "none", "reason": "invalid_output"}
        if isinstance(slot, str) and not re.fullmatch(r"\s*[1-9]\d*\s*", slot):
            return {"action": "none", "reason": "invalid_output"}
        try:
            slot_number = int(slot)
        except (TypeError, ValueError):
            return {"action": "none", "reason": "invalid_output"}
        if slot_number < 1 or slot_number > len(existing):
            return {"action": "none", "reason": "invalid_output"}
        bound = existing[slot_number - 1]
        topic = bound["topic"]
    return {
        "action": action,
        "topic": topic,
        "statement": statement,
        "evidence_span": evidence,
        "category": None,
    }


def _plan_facet_operations(
    candidates: list[dict[str, str]],
    existing_facets: list[dict[str, Any]],
    *,
    source_content: str,
) -> list[dict[str, Any]] | None:
    if not existing_facets:
        return [
            {"action": "add", "candidate_slot": index + 1, "existing_slot": None,
             "statement": candidate["statement"]}
            for index, candidate in enumerate(candidates)
        ]
    prompt = f"""
你要把本人對同一人的新觀點，局部合併到既有關係記憶。每個新候選都必須判斷一次。
action 只能是 add、merge、replace、retract、no_change。
add：可並存的新面向。merge：同一觀點的重述或補充。replace：本回合明確推翻指定舊觀點。retract：本回合明確撤回指定舊觀點。no_change：不足以改動。
不同面向可同時成立；傳訊息冷淡與外表帥氣不衝突。情境不同（例如訊息冷、見面熱情）要保留限定條件，不要互相覆蓋。
只有 replace／retract 才能移除舊觀點，而且必須由本回合原文清楚支持。不得從帥推導成喜歡，也不得從冷淡推導成不適合交往。
existing_slot 與 candidate_slot 都是一基索引。statement 必須簡短、第一人稱、保留程度／時間／情境；retract 與 no_change 的 statement 留空。
既有觀點：{json.dumps([{"slot": i + 1, "text": item["text"], "source_at": item["source_at"]} for i, item in enumerate(existing_facets)], ensure_ascii=False)}
新候選：{json.dumps([{"slot": i + 1, **item} for i, item in enumerate(candidates)], ensure_ascii=False)}
本回合原文：{source_content}
只輸出 JSON：{{"operations":[{{"action":"add|merge|replace|retract|no_change","candidate_slot":1,"existing_slot":null,"statement":""}}]}}
"""
    try:
        raw = generate_chat_completion(prompt, temperature=0, json_output=True)
        payload = json.loads(str(getattr(raw, "content", raw)))
    except Exception:
        return None
    operations = payload.get("operations") if isinstance(payload, dict) else None
    if not isinstance(operations, list):
        return None
    normalized: list[dict[str, Any]] = []
    seen_candidates: set[int] = set()
    for item in operations[:16]:
        if not isinstance(item, dict):
            return None
        action = str(item.get("action") or "")
        try:
            candidate_slot = int(item.get("candidate_slot"))
        except (TypeError, ValueError):
            return None
        if action not in _MERGE_ACTIONS or not (1 <= candidate_slot <= len(candidates)):
            return None
        existing_slot = item.get("existing_slot")
        if action in {"merge", "replace", "retract"}:
            try:
                existing_slot = int(existing_slot)
            except (TypeError, ValueError):
                return None
            if not (1 <= existing_slot <= len(existing_facets)):
                return None
        else:
            existing_slot = None
        statement = _subjective_statement(_safe_text(item.get("statement"), 120))
        if action in {"add", "merge", "replace"} and not statement:
            statement = candidates[candidate_slot - 1]["statement"]
        normalized.append({
            "action": action,
            "candidate_slot": candidate_slot,
            "existing_slot": existing_slot,
            "statement": statement,
        })
        seen_candidates.add(candidate_slot)
    for candidate_slot in range(1, len(candidates) + 1):
        if candidate_slot not in seen_candidates:
            normalized.append({
                "action": "no_change",
                "candidate_slot": candidate_slot,
                "existing_slot": None,
                "statement": "",
            })
    return normalized


def _apply_facet_operations(
    existing_facets: list[dict[str, Any]],
    candidates: list[dict[str, str]],
    operations: list[dict[str, Any]],
    *,
    owner: str,
    other: str,
    category: str,
    source_id: str,
    source_at: float,
    current: float,
) -> tuple[list[dict[str, Any]], bool, dict[str, int]]:
    snapshot = list(existing_facets)
    next_facets = [dict(item) for item in existing_facets]
    changed = False
    counts = {key: 0 for key in _MERGE_ACTIONS}
    touched_targets: set[str] = set()
    for operation in operations:
        action = operation["action"]
        counts[action] += 1
        candidate = candidates[operation["candidate_slot"] - 1]
        if action == "no_change":
            continue
        target = None
        if operation.get("existing_slot") is not None:
            target = snapshot[operation["existing_slot"] - 1]
            target_id = target["facet_id"]
            if target_id in touched_targets:
                continue
            touched_targets.add(target_id)
            if float(target.get("source_at", 0) or 0) > source_at:
                continue
        if action == "add":
            facet_id = _facet_id(
                owner, other, category, source_id, candidate["evidence_span"],
            )
            if any(item["facet_id"] == facet_id for item in next_facets):
                continue
            next_facets.append({
                "facet_id": facet_id,
                "text": operation["statement"] or candidate["statement"],
                "source_message_id": source_id,
                "evidence_span": candidate["evidence_span"],
                "source_at": source_at,
                "updated_at": current,
            })
            changed = True
        elif action in {"merge", "replace"} and target is not None:
            for index, item in enumerate(next_facets):
                if item["facet_id"] != target["facet_id"]:
                    continue
                next_facets[index] = {
                    **item,
                    "text": operation["statement"] or candidate["statement"],
                    "source_message_id": source_id,
                    "evidence_span": candidate["evidence_span"],
                    "source_at": source_at,
                    "updated_at": current,
                }
                changed = True
                break
        elif action == "retract" and target is not None:
            filtered = [
                item for item in next_facets if item["facet_id"] != target["facet_id"]
            ]
            changed = changed or len(filtered) != len(next_facets)
            next_facets = filtered
    return next_facets, changed, counts


def _finish_memory_job(
    job: dict[str, Any], token: str, *, status: str, outcome: str,
    current: float, extra: dict[str, Any] | None = None,
) -> None:
    values: dict[str, Any] = {
        "status": status,
        "outcome": outcome,
        "completed_at": current,
    }
    if extra:
        values.update(extra)
    RELATIONSHIP_MEMORY_OUTBOX.update_one(
        {"_id": job.get("_id"), "lease_token": token},
        {"$set": values, "$unset": {"lease_token": "", "lease_until": ""}},
    )


def _topic_key(topic: str) -> str:
    value = re.sub(
        r"[^0-9a-z\u4e00-\u9fff]+", "-", str(topic or "").lower(),
    ).strip("-")[:60]
    return value or hashlib.sha256(str(topic or "").encode()).hexdigest()[:24]


def _notify_saved_memory(
    job: dict[str, Any], *, owner: str, other: str, source_id: str,
) -> None:
    """Finish the legacy notification phase without injecting chat copy."""
    if str(job.get("notification_status") or "") == "not_required":
        return
    RELATIONSHIP_MEMORY_OUTBOX.update_one(
        {"_id": job.get("_id"), "lease_token": job.get("lease_token")},
        {"$set": {
            "notification_status": "not_required",
            "notification_completed_at": time.time(),
        }},
    )


def _session_kwargs(session: Any | None) -> dict[str, Any]:
    return {"session": session} if session is not None else {}


def _load_category_state(
    owner: str,
    other: str,
    category: str,
    *,
    session: Any | None = None,
) -> dict[str, Any]:
    topic = _CATEGORY_TOPICS[category]
    topic_key = _topic_key(topic)
    memory_id = _record_id(owner, other, topic_key)
    canonical = RELATIONSHIP_MEMORIES.find_one(
        {"_id": memory_id},
        **_session_kwargs(session),
    )
    rows = list(RELATIONSHIP_MEMORIES.find(
        {
            "owner_user_id": owner,
            "other_user_id": other,
            "status": "active",
            "$or": [{"category": category}, {"topic": topic}],
        },
        **_session_kwargs(session),
    ))
    existing_facets: list[dict[str, Any]] = []
    seen_facet_ids: set[str] = set()
    for row in rows:
        for facet in _row_facets(row):
            if facet["facet_id"] in seen_facet_ids:
                continue
            seen_facet_ids.add(facet["facet_id"])
            existing_facets.append(facet)
    snapshot_rows = {str(row.get("_id")): row for row in rows}
    if canonical is not None:
        snapshot_rows[str(canonical.get("_id"))] = canonical
    snapshot = sorted(
        (
            row_id,
            int(row.get("version", 0) or 0),
            str(row.get("status") or "active"),
        )
        for row_id, row in snapshot_rows.items()
    )
    return {
        "topic": topic,
        "topic_key": topic_key,
        "memory_id": memory_id,
        "canonical": canonical,
        "rows": rows,
        "existing_facets": existing_facets,
        "snapshot": snapshot,
    }


def _prepare_category_write(
    *,
    owner: str,
    other: str,
    category: str,
    candidates: list[dict[str, str]],
    source_content: str,
) -> dict[str, Any]:
    state = _load_category_state(owner, other, category)
    operations = _plan_facet_operations(
        candidates,
        state["existing_facets"],
        source_content=source_content,
    )
    if operations is None:
        raise RuntimeError("merge_output_invalid")
    return {**state, "operations": operations}


def _write_category_facets(
    *,
    owner: str,
    other: str,
    relationship_id: str,
    source_id: str,
    source_content: str,
    source_at: float,
    current: float,
    category: str,
    candidates: list[dict[str, str]],
    prepared: dict[str, Any] | None = None,
    session: Any | None = None,
) -> dict[str, Any]:
    current_state = _load_category_state(
        owner, other, category, session=session,
    )
    if prepared is not None and current_state["snapshot"] != prepared.get("snapshot"):
        raise _MemoryVersionConflict("memory_snapshot_changed")
    state = prepared or current_state
    topic = state["topic"]
    topic_key = state["topic_key"]
    memory_id = state["memory_id"]
    canonical = current_state["canonical"]
    rows = current_state["rows"]
    existing_facets = state["existing_facets"]
    operations = state.get("operations")
    if operations is None:
        operations = _plan_facet_operations(
            candidates,
            existing_facets,
            source_content=source_content,
        )
    if operations is None:
        raise RuntimeError("merge_output_invalid")
    next_facets, changed, counts = _apply_facet_operations(
        existing_facets,
        candidates,
        operations,
        owner=owner,
        other=other,
        category=category,
        source_id=source_id,
        source_at=source_at,
        current=current,
    )
    requires_migration = bool(rows) and (
        canonical is None
        or any(int(row.get("schema_version", 1) or 1) < 2 for row in rows)
        or len(rows) > 1
    )
    if (
        not changed
        and not requires_migration
        and canonical is not None
        and str(canonical.get("status") or "active") == "active"
    ):
        return {
            "memory_id": memory_id,
            "version": int(canonical.get("version", 1) or 1),
            "operation_counts": counts,
        }
    expected_version = int((canonical or {}).get("version", 0) or 0)
    previous_statement = _render_facets(existing_facets)
    next_version = max(1, expected_version + 1)
    values = {
        "owner_user_id": owner,
        "other_user_id": other,
        "relationship_id": relationship_id,
        "topic_key": topic_key,
        "category": category,
        "topic": topic,
        "schema_version": 2,
        "facets": next_facets,
        "statement": _render_facets(next_facets),
        "source_message_id": source_id,
        "source_at": source_at,
        "version": next_version,
        "status": "active" if next_facets else "deleted",
        "updated_at": current,
        "applied_source_message_ids": list(dict.fromkeys([
            *list((canonical or {}).get("applied_source_message_ids") or []),
            source_id,
        ]))[-100:],
    }
    query: dict[str, Any] = {"_id": memory_id}
    if canonical is not None:
        query["version"] = expected_version
    else:
        query["version"] = {"$exists": False}
    update: dict[str, Any] = {
        "$set": values,
        "$setOnInsert": {"created_at": current},
    }
    if previous_statement:
        update["$push"] = {"revisions": {
            "$each": [{
                "statement": previous_statement,
                "facets": existing_facets,
                "replaced_at": current,
            }],
            "$slice": -12,
        }}
    try:
        updated = RELATIONSHIP_MEMORIES.find_one_and_update(
            query,
            update,
        upsert=canonical is None,
            return_document=ReturnDocument.AFTER,
            **_session_kwargs(session),
        )
    except DuplicateKeyError as exc:
        raise _MemoryVersionConflict("memory_create_conflict") from exc
    if not updated:
        raise _MemoryVersionConflict("memory_version_conflict")
    for row in rows:
        row_id = row.get("_id")
        if str(row_id) == memory_id:
            continue
        RELATIONSHIP_MEMORIES.update_one(
            {"_id": row_id, "status": "active"},
            {"$set": {
                "status": "superseded",
                "superseded_by": memory_id,
                "updated_at": current,
            }},
            **_session_kwargs(session),
        )
    return {
        "memory_id": memory_id,
        "version": next_version,
        "operation_counts": counts,
    }


def _commit_memory_batch(
    *,
    job: dict[str, Any],
    token: str,
    owner: str,
    other: str,
    match: dict[str, Any],
    source: dict[str, Any],
    candidates: list[dict[str, str]],
    current: float,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate["category"], []).append(candidate)

    source_content = _safe_text(source.get("content"), 2000)
    prepared = {
        category: _prepare_category_write(
            owner=owner,
            other=other,
            category=category,
            candidates=items,
            source_content=source_content,
        )
        for category, items in grouped.items()
    }

    def commit(session: Any | None = None) -> dict[str, Any]:
        results = [
            _write_category_facets(
                owner=owner,
                other=other,
                relationship_id=str(match.get("_id") or ""),
                source_id=str(job.get("source_message_id") or ""),
                source_content=source_content,
                source_at=float(source.get("timestamp", current) or current),
                current=current,
                category=category,
                candidates=items,
                prepared=prepared[category],
                session=session,
            )
            for category, items in grouped.items()
        ]
        memory_ids = [item["memory_id"] for item in results]
        operation_counts = {key: 0 for key in _MERGE_ACTIONS}
        for item in results:
            for key, value in item["operation_counts"].items():
                operation_counts[key] += int(value or 0)
        RELATIONSHIP_MEMORY_OUTBOX.update_one(
            {"_id": job.get("_id"), "lease_token": token},
            {"$set": {
                "write_status": "saved",
                "memory_id": memory_ids[0] if memory_ids else "",
                "memory_ids": memory_ids,
                "memory_versions": {
                    item["memory_id"]: item["version"] for item in results
                },
                "operation_counts": operation_counts,
            }},
            **_session_kwargs(session),
        )
        return {
            "memory_ids": memory_ids,
            "memory_versions": {
                item["memory_id"]: item["version"] for item in results
            },
            "operation_counts": operation_counts,
        }

    if mongo_client is None or os.getenv("AYUE_TEST_MODE", "").lower() in {"1", "true", "on"}:
        return commit()
    with mongo_client.start_session() as session:
        with session.start_transaction():
            return commit(session)


def process_relationship_memory_outbox_once(*, now: float | None = None) -> dict[str, int]:
    current = time.time() if now is None else float(now)
    stats = {
        "scanned": 0, "saved": 0, "shadowed": 0, "skipped": 0,
        "retried": 0, "failed": 0,
    }
    mode = relationship_memory_mode()
    if mode == "off":
        return stats
    token = uuid.uuid4().hex
    job = RELATIONSHIP_MEMORY_OUTBOX.find_one_and_update(
        {"$or": [
            {"status": "pending", "next_attempt_at": {"$lte": current}},
            {"status": "processing", "lease_until": {"$lte": current}},
        ]},
        {"$set": {
            "status": "processing", "lease_token": token,
            "lease_until": current + 90,
        }},
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if not job:
        return stats
    stats["scanned"] = 1
    source_id = str(job.get("source_message_id") or "")
    owner = str(job.get("owner_user_id") or "")
    other = str(job.get("other_user_id") or "")
    try:
        if RELATIONSHIP_MEMORY_SUPPRESSIONS.find_one({"source_message_id": source_id}):
            _finish_memory_job(
                job, token, status="completed", outcome="suppressed", current=current,
            )
            stats["skipped"] = 1
            return stats

        match = _active_match(owner, other)
        source = messages_coll.find_one(
            {**_message_id_query(source_id), "sender_id": owner},
        )
        expected_room = f"mediator_private::{owner}::{other}"
        if (
            not match
            or not source
            or str(match.get("_id") or "") != str(job.get("relationship_id") or "")
            or str(source.get("room_id") or "") != expected_room
        ):
            _finish_memory_job(
                job, token, status="completed", outcome="invalid_scope", current=current,
            )
            stats["skipped"] = 1
            return stats

        # A previous attempt can have committed the memory and failed while
        # sending its notice. Reuse that write and only retry notification.
        existing_saved = RELATIONSHIP_MEMORIES.find_one({
            "owner_user_id": owner,
            "other_user_id": other,
            "status": "active",
            "$or": [
                {"source_message_id": source_id},
                {"facets.source_message_id": source_id},
                {"applied_source_message_ids": source_id},
            ],
        })
        if str(job.get("write_status") or "") == "saved" or existing_saved:
            memory_id = str(
                job.get("memory_id")
                or ((job.get("memory_ids") or [""])[0])
                or (existing_saved or {}).get("_id")
                or "",
            )
            try:
                _notify_saved_memory(
                    {**job, "_id": job.get("_id"), "lease_token": token},
                    owner=owner, other=other, source_id=source_id,
                )
            except Exception:
                raise RuntimeError("notification_error")
            _finish_memory_job(
                job, token, status="completed", outcome="saved", current=current,
                extra={
                    "write_status": "saved",
                    "memory_id": memory_id,
                    "memory_ids": job.get("memory_ids") or ([memory_id] if memory_id else []),
                },
            )
            stats["saved"] = 1
            return stats

        decision = _extract(job, source)
        if decision is None:
            raise RuntimeError("provider_error")
        if decision.get("action") == "none":
            reason = str(decision.get("reason") or "not_explicit")
            outcome = (
                reason if reason in {"not_explicit", "invalid_output"}
                else "invalid_output"
            )
            _finish_memory_job(
                job, token, status="completed", outcome=outcome, current=current,
            )
            stats["skipped"] = 1
            return stats
        if mode == "shadow":
            RELATIONSHIP_MEMORY_SHADOW_RUNS.update_one(
                {"source_message_id": source_id},
                {"$setOnInsert": {
                    "outcome": "shadowed", "decision": decision,
                    "created_at": current,
                }},
                upsert=True,
            )
            _finish_memory_job(
                job, token, status="completed", outcome="shadowed", current=current,
            )
            stats["shadowed"] = 1
            return stats

        candidates = list(decision.get("candidates") or [])
        if not candidates:
            if decision.get("action") not in {"create", "update"}:
                _finish_memory_job(
                    job, token, status="completed", outcome="invalid_output", current=current,
                )
                stats["skipped"] = 1
                return stats
            candidates = [{
                "category": str(decision.get("category") or "impression"),
                "topic": str(decision.get("topic") or _CATEGORY_TOPICS["impression"]),
                "statement": str(decision.get("statement") or ""),
                "evidence_span": str(decision.get("evidence_span") or ""),
            }]
        committed = _commit_memory_batch(
            job=job,
            token=token,
            owner=owner,
            other=other,
            match=match,
            source=source,
            candidates=candidates,
            current=current,
        )
        try:
            _notify_saved_memory(
                {**job, "_id": job.get("_id"), "lease_token": token,
                 "write_status": "saved",
                 "memory_id": (committed.get("memory_ids") or [""])[0]},
                owner=owner, other=other, source_id=source_id,
            )
        except Exception:
            raise RuntimeError("notification_error")
        _finish_memory_job(
            job, token, status="completed", outcome="saved", current=current,
            extra={
                "write_status": "saved",
                "memory_id": (committed.get("memory_ids") or [""])[0],
                "memory_ids": committed.get("memory_ids") or [],
                "memory_versions": committed.get("memory_versions") or {},
                "operation_counts": committed.get("operation_counts") or {},
            },
        )
        stats["saved"] = 1
    except Exception as exc:
        attempts = int(job.get("attempts", 0) or 0) + 1
        terminal = attempts >= 3
        try:
            RELATIONSHIP_MEMORY_OUTBOX.update_one(
                {"_id": job.get("_id"), "lease_token": token},
                {"$set": {
                    "status": "failed" if terminal else "pending",
                    "attempts": attempts,
                    "next_attempt_at": current + min(900, 60 * attempts),
                    "last_error": type(exc).__name__,
                    **({"outcome": "failed"} if terminal else {}),
                }, "$unset": {"lease_token": "", "lease_until": ""}},
            )
        finally:
            stats["retried"] = 1
            if terminal:
                stats["failed"] = 1
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
