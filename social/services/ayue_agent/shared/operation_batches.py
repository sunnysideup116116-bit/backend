"""Persistent natural-language operation batches for public Pi.

The batch store remembers what the user asked and what the server verified.  It
does not resolve pronouns, contacts, calendar events, or any other semantic
reference.  Those decisions remain with the Planner and the existing domain
preflight on the turn where an item is handled.
"""

from __future__ import annotations

import copy
import time
import uuid
from typing import Any


PENDING_BATCH_STATUSES = frozenset({"active", "paused"})
TERMINAL_ITEM_STATUSES = frozenset({"completed", "cancelled"})


def _safe_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _find_rows(collection: Any, query: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in collection.find(query)]
    except Exception:
        return []


class OperationBatchManager:
    """CAS-backed queue whose items contain natural language, not references."""

    def __init__(self, collection: Any) -> None:
        self._coll = collection

    def ensure_indexes(self) -> None:
        self._coll.create_index(
            [("user_id", 1), ("room_id", 1), ("surface", 1), ("status", 1), ("updated_at", -1)],
            name="operation_batch_active_room",
        )
        self._coll.create_index("expires_at", expireAfterSeconds=0, sparse=True)

    def list_pending(self, *, user_id: str, room_id: str, surface: str) -> list[dict[str, Any]]:
        rows = _find_rows(self._coll, {
            "user_id": user_id,
            "room_id": room_id,
            "surface": surface,
            "source_engine": "pi",
            "status": {"$in": list(PENDING_BATCH_STATUSES)},
        })
        rows.sort(
            key=lambda row: (
                1 if row.get("status") == "active" else 0,
                float(row.get("updated_at", 0) or 0),
            ),
            reverse=True,
        )
        return rows

    def pending_lookup(
        self, *, user_id: str, room_id: str, surface: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Distinguish an empty queue from unavailable storage for Pi."""
        query = {
            "user_id": user_id,
            "room_id": room_id,
            "surface": surface,
            "source_engine": "pi",
            "status": {"$in": list(PENDING_BATCH_STATUSES)},
        }
        try:
            rows = [dict(row) for row in self._coll.find(query)]
        except Exception:
            return "unavailable", []
        rows.sort(
            key=lambda row: (
                1 if row.get("status") == "active" else 0,
                float(row.get("updated_at", 0) or 0),
            ),
            reverse=True,
        )
        return ("found" if rows else "empty"), rows

    def latest_pending(self, *, user_id: str, room_id: str, surface: str) -> dict[str, Any] | None:
        rows = self.list_pending(user_id=user_id, room_id=room_id, surface=surface)
        return rows[0] if rows else None

    def get(self, batch_id: str) -> dict[str, Any] | None:
        rows = _find_rows(self._coll, {"_id": str(batch_id or "")})
        return rows[0] if rows else None

    def create_batch(
        self,
        *,
        user_id: str,
        room_id: str,
        surface: str,
        operations: list[dict[str, Any]],
        source_message: str,
        source_sent_at: str,
        timezone: str,
        source_clock: dict[str, Any] | None,
        context_messages: list[dict[str, Any]],
        origin_run_id: str,
        source_engine: str = "pi",
    ) -> dict[str, Any]:
        if not 2 <= len(operations) <= 4:
            raise ValueError("operation batch requires two to four items")
        now = time.time()
        # A new request is a separate batch. Older unfinished work is paused,
        # never merged into the new natural-language request.
        self._coll.update_many(
            {
                "user_id": user_id,
                "room_id": room_id,
                "surface": surface,
                "status": "active",
            },
            {"$set": {"status": "paused", "updated_at": now, "pause_reason": "new_batch"}},
        )
        items: list[dict[str, Any]] = []
        for index, operation in enumerate(operations):
            request = _safe_text(operation.get("request"), 600)
            if not request:
                raise ValueError("queued operation requires natural-language request")
            items.append({
                "item_id": uuid.uuid4().hex,
                "position": index,
                "kind": _safe_text(operation.get("kind"), 40),
                "request": request,
                "source_excerpt": _safe_text(operation.get("source_excerpt") or source_message, 400),
                "corrections": [],
                "status": "active" if index == 0 else "queued",
                "confirmation_id": None,
                "result_summary": "",
            })
        document = {
            "_id": uuid.uuid4().hex,
            "user_id": user_id,
            "room_id": room_id,
            "surface": surface,
            "status": "active",
            "revision": 1,
            "current_index": 0,
            "source_message": _safe_text(source_message, 1600),
            "source_sent_at": _safe_text(source_sent_at, 80),
            "timezone": _safe_text(timezone, 64),
            "source_clock": copy.deepcopy(source_clock or {}),
            "context_messages": [
                {
                    "role": _safe_text(item.get("role"), 12),
                    "content": _safe_text(item.get("content"), 1200),
                    "sent_at": _safe_text(item.get("sent_at"), 80),
                    "timezone": _safe_text(item.get("timezone") or timezone, 64),
                }
                for item in context_messages[-12:]
                if isinstance(item, dict)
                and _safe_text(item.get("role"), 12) in {"user", "assistant"}
                and _safe_text(item.get("content"), 1200)
            ],
            "items": items,
            "origin_run_id": _safe_text(origin_run_id, 80),
            "source_engine": "pi",
            "created_at": now,
            "updated_at": now,
        }
        self._coll.insert_one(document)
        return copy.deepcopy(document)

    @staticmethod
    def current_item(batch: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(batch, dict):
            return None
        items = batch.get("items") or []
        index = int(batch.get("current_index", 0) or 0)
        if not isinstance(items, list) or index < 0 or index >= len(items):
            return None
        item = items[index]
        return dict(item) if isinstance(item, dict) else None

    def _replace(self, batch: dict[str, Any], **updates: Any) -> dict[str, Any] | None:
        revision = int(batch.get("revision", 0) or 0)
        updates.update({"revision": revision + 1, "updated_at": time.time()})
        result = self._coll.update_one(
            {"_id": batch.get("_id"), "revision": revision},
            {"$set": updates},
        )
        return self.get(str(batch.get("_id") or "")) if getattr(result, "modified_count", 0) else None

    def activate(self, batch_id: str) -> dict[str, Any] | None:
        batch = self.get(batch_id)
        if not batch or batch.get("status") not in PENDING_BATCH_STATUSES:
            return None
        return self._replace(batch, status="active", pause_reason="")

    def pause(self, batch_id: str, *, reason: str = "interrupted") -> dict[str, Any] | None:
        batch = self.get(batch_id)
        if not batch or batch.get("status") not in PENDING_BATCH_STATUSES:
            return None
        return self._replace(batch, status="paused", pause_reason=_safe_text(reason, 80))

    def cancel_all(self, batch_id: str) -> dict[str, Any] | None:
        batch = self.get(batch_id)
        if not batch or batch.get("status") not in PENDING_BATCH_STATUSES:
            return None
        items = copy.deepcopy(batch.get("items") or [])
        for item in items:
            if item.get("status") not in TERMINAL_ITEM_STATUSES:
                item["status"] = "cancelled"
                item["confirmation_id"] = None
                item["result_summary"] = "使用者已取消整批剩餘操作。"
        now = time.time()
        return self._replace(
            batch, items=items, status="cancelled", completed_at=now,
            expires_at=now + 30 * 86400,
        )

    def append_correction(
        self, batch_id: str, text: str, *, sent_at: str = "",
        clock_context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        batch = self.get(batch_id)
        item = self.current_item(batch)
        if not batch or not item or item.get("status") in TERMINAL_ITEM_STATUSES:
            return None
        items = copy.deepcopy(batch.get("items") or [])
        index = int(batch.get("current_index", 0) or 0)
        correction = _safe_text(text, 600)
        if correction:
            items[index].setdefault("corrections", []).append({
                "content": correction,
                "sent_at": _safe_text(sent_at, 80),
                "clock": copy.deepcopy(clock_context or {}),
            })
            items[index]["status"] = "active"
            items[index]["confirmation_id"] = None
        return self._replace(batch, items=items, status="active", pause_reason="")

    def bind_confirmation(self, batch_id: str, choice_id: str) -> dict[str, Any] | None:
        batch = self.get(batch_id)
        item = self.current_item(batch)
        if not batch or not item or not choice_id:
            return None
        if (
            str(item.get("confirmation_id") or "") == str(choice_id)
            and item.get("status") == "awaiting_confirmation"
        ):
            return batch
        items = copy.deepcopy(batch.get("items") or [])
        index = int(batch.get("current_index", 0) or 0)
        items[index]["confirmation_id"] = str(choice_id)
        items[index]["status"] = "awaiting_confirmation"
        return self._replace(batch, items=items, status="active")

    def mark_awaiting_input(self, batch_id: str) -> dict[str, Any] | None:
        batch = self.get(batch_id)
        item = self.current_item(batch)
        if not batch or not item or item.get("status") in TERMINAL_ITEM_STATUSES:
            return None
        items = copy.deepcopy(batch.get("items") or [])
        items[int(batch.get("current_index", 0) or 0)]["status"] = "awaiting_input"
        return self._replace(batch, items=items, status="active")

    def resolve_choice(
        self,
        *,
        user_id: str,
        room_id: str,
        surface: str,
        choice_id: str,
        outcome: str,
        result_summary: str,
    ) -> dict[str, Any] | None:
        for batch in self.list_pending(user_id=user_id, room_id=room_id, surface=surface):
            item = self.current_item(batch)
            if not item or str(item.get("confirmation_id") or "") != str(choice_id or ""):
                continue
            items = copy.deepcopy(batch.get("items") or [])
            index = int(batch.get("current_index", 0) or 0)
            if items[index].get("status") in TERMINAL_ITEM_STATUSES:
                return batch
            if outcome not in {"completed", "cancelled", "failed"}:
                raise ValueError("unsupported operation outcome")
            items[index]["status"] = outcome
            items[index]["result_summary"] = _safe_text(result_summary, 600)
            items[index]["confirmation_id"] = str(choice_id)
            if outcome == "failed":
                return self._replace(batch, items=items, status="paused", pause_reason="operation_failed")
            next_index = index + 1
            if next_index >= len(items):
                now = time.time()
                return self._replace(
                    batch, items=items, current_index=next_index,
                    status="completed", completed_at=now,
                    expires_at=now + 30 * 86400,
                )
            items[next_index]["status"] = "active"
            return self._replace(batch, items=items, current_index=next_index, status="active")
        return None

    def prompt_context(self, batch: dict[str, Any], *, auto_resume: bool = False) -> dict[str, Any]:
        current = self.current_item(batch)
        items = batch.get("items") or []
        return {
            "batch_status": str(batch.get("status") or ""),
            "auto_resume": bool(auto_resume),
            "original_request": _safe_text(batch.get("source_message"), 800),
            "original_sent_at": _safe_text(batch.get("source_sent_at"), 80),
            "original_timezone": _safe_text(batch.get("timezone"), 64),
            "original_clock": copy.deepcopy(batch.get("source_clock") or {}),
            "current_position": int(batch.get("current_index", 0) or 0) + 1,
            "total_operations": len(items),
            "current_operation": {
                "kind": _safe_text((current or {}).get("kind"), 40),
                "request": _safe_text((current or {}).get("request"), 500),
                "corrections": [
                    {
                        "content": _safe_text(item.get("content"), 400),
                        "sent_at": _safe_text(item.get("sent_at"), 80),
                    }
                    for item in ((current or {}).get("corrections") or [])[-3:]
                    if isinstance(item, dict)
                ],
                "status": _safe_text((current or {}).get("status"), 40),
            },
            "verified_progress": [
                {
                    "position": int(item.get("position", 0) or 0) + 1,
                    "kind": _safe_text(item.get("kind"), 40),
                    "status": _safe_text(item.get("status"), 40),
                    "result_summary": _safe_text(item.get("result_summary"), 300),
                }
                for item in items
                if isinstance(item, dict) and item.get("status") in TERMINAL_ITEM_STATUSES | {"failed"}
            ],
            "remaining_operations": [
                {
                    "position": int(item.get("position", 0) or 0) + 1,
                    "kind": _safe_text(item.get("kind"), 40),
                    "request": _safe_text(item.get("request"), 400),
                }
                for item in items[int(batch.get("current_index", 0) or 0) + 1:]
                if isinstance(item, dict)
            ],
        }
