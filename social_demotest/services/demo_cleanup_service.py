"""Centralized, guarded destructive operations for the local Demo tools."""

from __future__ import annotations

import logging
from typing import Any

import requests

from config import DEMO_DESTRUCTIVE_TOOLS_ENABLED
from database import MONGO_INIT_ERROR, db


LOGGER = logging.getLogger(__name__)
MATCHMAKER_URL = "http://127.0.0.1:9001"


class DemoCleanupError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 502) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def ensure_destructive_tools_enabled() -> None:
    if not DEMO_DESTRUCTIVE_TOOLS_ENABLED:
        raise DemoCleanupError("demo_tools_disabled", status_code=403)


def clear_graph() -> dict[str, Any]:
    ensure_destructive_tools_enabled()
    try:
        response = requests.post(f"{MATCHMAKER_URL}/api/clear_graph", timeout=10)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        LOGGER.exception("demo graph cleanup request failed: %s", type(exc).__name__)
        raise DemoCleanupError("graph_unavailable", status_code=503) from exc
    except ValueError as exc:
        LOGGER.exception("demo graph cleanup returned invalid JSON")
        raise DemoCleanupError("graph_invalid_response", status_code=502) from exc
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise DemoCleanupError("graph_cleanup_failed", status_code=502)
    return {"status": "cleared"}


def graph_health() -> dict[str, str]:
    try:
        response = requests.get(f"{MATCHMAKER_URL}/api/graph/health", timeout=3)
        payload = response.json()
    except (requests.RequestException, ValueError):
        return {"status": "unavailable"}
    status = payload.get("status") if isinstance(payload, dict) else None
    return {"status": status if status in {"available", "unavailable", "not_configured"} else "unavailable"}


def clear_runtime_fallbacks() -> dict[str, str]:
    from services.ayue_agent.v3 import calendar_drafts, calendar_references, scheduler

    calendar_drafts.clear_runtime_state()
    calendar_references.clear_runtime_state()
    scheduler.clear_demo_runtime_state()
    return {"status": "cleared"}


def clear_mongo_database() -> dict[str, Any]:
    try:
        names = sorted(name for name in db.list_collection_names() if not name.startswith("system."))
    except Exception as exc:
        LOGGER.exception("demo Mongo collection discovery failed: %s", type(exc).__name__)
        raise DemoCleanupError("mongo_unavailable", status_code=503) from exc
    deleted: dict[str, int] = {}
    for name in names:
        try:
            result = db[name].delete_many({})
        except Exception as exc:
            LOGGER.exception("demo Mongo cleanup failed collection=%s error=%s", name, type(exc).__name__)
            raise DemoCleanupError("mongo_cleanup_failed", status_code=500) from exc
        deleted[name] = int(getattr(result, "deleted_count", 0) or 0)
    return {
        "status": "cleared",
        "collections": len(names),
        "deleted_documents": sum(deleted.values()),
    }


def clear_all_demo_state() -> dict[str, Any]:
    """Clear Graph first, then all configured app data; no cross-store rollback."""
    if MONGO_INIT_ERROR is not None:
        raise DemoCleanupError("mongo_unavailable", status_code=503)
    try:
        db.command("ping")
    except Exception as exc:
        LOGGER.exception("demo Mongo preflight failed: %s", type(exc).__name__)
        raise DemoCleanupError("mongo_unavailable", status_code=503) from exc
    graph = clear_graph()
    runtime = clear_runtime_fallbacks()
    mongo = clear_mongo_database()
    return {"status": "success", "graph": graph, "runtime": runtime, "mongo": mongo}
