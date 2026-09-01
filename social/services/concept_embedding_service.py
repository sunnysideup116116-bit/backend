"""Incrementally embed graph Concepts without blocking user-facing requests."""

from __future__ import annotations

import math
import os
import re
import threading
from typing import Any

import requests
from fastapi import HTTPException

from services.ai_service import get_embeddings
from services.event_opportunity_service import run_requested_event_opportunity_scan


AGENT_PENDING_CONCEPTS_URL = "http://127.0.0.1:9001/api/concepts/missing-embeddings"
AGENT_PROJECT_CONCEPTS_URL = "http://127.0.0.1:9001/api/concepts/embeddings/project"
CONCEPT_EMBEDDING_DIMENSIONS = 768
DEFAULT_BATCH_SIZE = 20

_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()
_run_lock = threading.Lock()


def _category_kind(category: str) -> str:
    return {
        "activity": "activity",
        "lifestyle": "interest",
        "habit": "interest",
        "personality": "partner_trait",
        "relationship": "partner_trait",
        "feedback": "partner_trait",
        "value": "value",
    }.get(str(category or "").lower(), "unknown")


def _mongo_kind_overrides(keys: list[str]) -> dict[str, str]:
    if not keys:
        return {}
    from database import db

    rows = db["preference_facts"].find(
        {"concept_key": {"$in": keys}, "active": True},
        {"_id": 0, "concept_key": 1, "category": 1},
    )
    return {
        str(row.get("concept_key") or ""): _category_kind(str(row.get("category") or ""))
        for row in rows
        if str(row.get("concept_key") or "")
    }


def _resolved_kind(item: dict[str, Any], mongo_kinds: dict[str, str]) -> str:
    suggested = str(item.get("suggested_kind") or "unknown")
    if suggested in {"activity", "vibe"}:
        return suggested
    return mongo_kinds.get(str(item.get("key") or ""), suggested)


def _unit_vector(vector: list[float]) -> list[float]:
    values = [float(value) for value in vector]
    magnitude = math.sqrt(sum(value * value for value in values))
    if not magnitude:
        raise ValueError("zero_embedding")
    return [value / magnitude for value in values]


def _retry_after_seconds(detail: str) -> float:
    match = re.search(r"(?:retry in|seconds['\"\s:]+)(\d+(?:\.\d+)?)", detail, re.I)
    if match:
        return max(5.0, min(float(match.group(1)) + 1.0, 300.0))
    return 60.0 if "429" in detail else 20.0


def refresh_semantic_event_links() -> dict[str, Any]:
    response = requests.post(
        AGENT_PROJECT_CONCEPTS_URL,
        json={"concepts": []},
        timeout=(3, 30),
    )
    response.raise_for_status()
    return response.json()


def process_pending_concept_embeddings(batch_size: int = DEFAULT_BATCH_SIZE) -> dict[str, Any]:
    """Embed one bounded batch; callers may safely retry after the returned delay."""
    safe_batch = max(1, min(int(batch_size or DEFAULT_BATCH_SIZE), 20))
    if not _run_lock.acquire(blocking=False):
        return {"status": "already_running", "retry_after": 5.0}
    try:
        response = requests.get(
            AGENT_PENDING_CONCEPTS_URL,
            params={"limit": safe_batch},
            timeout=(3, 20),
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            return {"status": "agent_unavailable", "retry_after": 20.0}
        concepts = [item for item in payload.get("concepts", []) if isinstance(item, dict)]
        if not concepts:
            return {"status": "idle", "embedded_count": 0, "pending_count": 0}

        mongo_kinds = _mongo_kind_overrides([
            str(item.get("key") or "") for item in concepts if item.get("key")
        ])
        labels = [" ".join(str(item.get("label") or "").split())[:60] for item in concepts]
        vectors = get_embeddings(
            labels,
            task_type="semantic_similarity",
            output_dimensionality=CONCEPT_EMBEDDING_DIMENSIONS,
        )
        if len(vectors) != len(concepts):
            raise ValueError("embedding_count_mismatch")

        projected = []
        for item, label, vector in zip(concepts, labels, vectors):
            projected.append({
                "key": str(item.get("key") or "")[:100],
                "label": label,
                "kind": _resolved_kind(item, mongo_kinds),
                "embedding": _unit_vector(vector),
            })
        write = requests.post(
            AGENT_PROJECT_CONCEPTS_URL,
            json={
                "concepts": projected,
            },
            timeout=(3, 45),
        )
        write.raise_for_status()
        result = write.json()
        if result.get("status") != "success":
            return {"status": "projection_failed", "retry_after": 20.0, **result}
        return result
    except HTTPException as exc:
        detail = " ".join(str(exc.detail or "embedding_failed").split())[:500]
        return {
            "status": "rate_limited" if "429" in detail else "embedding_failed",
            "error_code": "embedding_http_error",
            "message": detail,
            "retry_after": _retry_after_seconds(detail),
        }
    except requests.RequestException as exc:
        return {
            "status": "agent_unavailable",
            "error_code": type(exc).__name__,
            "retry_after": 20.0,
        }
    except Exception as exc:
        print(f"[CONCEPT_EMBEDDING] batch failed error={type(exc).__name__}: {exc}")
        return {
            "status": "error",
            "error_code": type(exc).__name__,
            "retry_after": 20.0,
        }
    finally:
        _run_lock.release()


def _worker_loop() -> None:
    delay = 5.0
    while not _stop_event.wait(delay):
        result = process_pending_concept_embeddings()
        status = str(result.get("status") or "error")
        if status == "success":
            pending = int(result.get("pending_count", 0) or 0)
            delay = 20.0 if pending else 60.0
            print(
                f"[CONCEPT_EMBEDDING] embedded={result.get('embedded_count', 0)} "
                f"pending={pending} relevance={result.get('relevance_count', 0)}"
            )
            if not pending:
                scan = run_requested_event_opportunity_scan()
                if scan.get("status") not in {"not_requested", "disabled"}:
                    print(
                        f"[EVENT_OPPORTUNITY_SCAN] status={scan.get('status')} "
                        f"created={scan.get('created_count', 0)} "
                        f"scanned={scan.get('scanned_count', 0)}"
                    )
        elif status == "idle":
            delay = 60.0
            scan = run_requested_event_opportunity_scan()
            if scan.get("status") not in {"not_requested", "disabled"}:
                print(
                    f"[EVENT_OPPORTUNITY_SCAN] status={scan.get('status')} "
                    f"created={scan.get('created_count', 0)} "
                    f"scanned={scan.get('scanned_count', 0)}"
                )
        else:
            delay = max(5.0, float(result.get("retry_after", 20.0) or 20.0))
            print(f"[CONCEPT_EMBEDDING] paused status={status} retry_after={delay:.0f}s")


def start_concept_embedding_worker() -> None:
    global _worker_thread
    if os.getenv("CONCEPT_EMBEDDING_WORKER_ENABLED", "on").strip().lower() != "on":
        return
    if _worker_thread and _worker_thread.is_alive():
        return
    _stop_event.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop, name="concept-embedding-worker", daemon=True,
    )
    _worker_thread.start()


def stop_concept_embedding_worker() -> None:
    _stop_event.set()
