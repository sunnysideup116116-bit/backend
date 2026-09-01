"""Incremental Event relevance orchestration.

Concept vectors and derived Event links live in Neo4j. This service deliberately
processes one small embedding batch at a time instead of rebuilding all vectors.
"""

from __future__ import annotations

from typing import Any

from services.concept_embedding_service import (
    process_pending_concept_embeddings,
    refresh_semantic_event_links,
)


def project_event_relevance(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Refresh links for reusable vectors; missing vectors remain background work."""
    if not events:
        return {"status": "empty", "event_count": 0, "link_count": 0}
    try:
        result = refresh_semantic_event_links()
    except Exception as exc:
        return {
            "status": "queued", "event_count": len(events), "link_count": 0,
            "error_code": type(exc).__name__,
        }
    return {"event_count": len(events), **result}


def rebuild_all_event_relevance(limit: int = 20) -> dict[str, Any]:
    """Process one bounded Concept batch, then refresh graph links."""
    result = process_pending_concept_embeddings(batch_size=min(int(limit or 20), 20))
    status = str(result.get("status") or "error")
    if status == "idle":
        try:
            result = refresh_semantic_event_links()
            status = str(result.get("status") or "error")
        except Exception as exc:
            return {"status": "error", "error_code": type(exc).__name__, "link_count": 0}
    if status in {"rate_limited", "embedding_failed", "agent_unavailable", "projection_failed"}:
        return {**result, "status": "deferred"}
    return result
