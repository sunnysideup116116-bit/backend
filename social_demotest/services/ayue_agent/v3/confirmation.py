"""Multi-confirmation manager: independent CAS per confirmation, no cross-invalidation."""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable

from pydantic import BaseModel


class ConfirmationRecord(BaseModel):
    confirmation_id: str
    user_id: str
    agent_name: str
    tool_name: str
    arguments: dict[str, Any]
    status: str = "pending"
    created_at: float
    expires_at: float


class ConfirmationManager:
    """Manages multiple independent pending confirmations per user.

    Each confirmation executes independently via CAS. Stale confirmations report
    `stale_revision` without overwriting terminal state; they do not invalidate
    sibling confirmations.
    """

    def __init__(self, collection: Any) -> None:
        self._coll = collection

    def create_confirmation(
        self, *, user_id: str, agent_name: str, tool_name: str,
        arguments: dict[str, Any], ttl_seconds: int = 900,
        payload: dict[str, Any] | None = None,
    ) -> str:
        confirmation_id = uuid.uuid4().hex
        now = time.time()
        self._coll.insert_one({
            "_id": confirmation_id,
            "user_id": user_id,
            "agent_name": agent_name,
            "tool_name": tool_name,
            "arguments": arguments,
            "payload": payload or {},
            "status": "pending",
            "created_at": now,
            "expires_at": now + ttl_seconds,
        })
        return confirmation_id

    def list_active(self, *, user_id: str) -> list[dict[str, Any]]:
        now = time.time()
        cursor = self._coll.find({
            "user_id": user_id,
            "status": "pending",
            "expires_at": {"$gt": now},
        })
        return list(cursor)

    def cancel_all(self, *, user_id: str) -> int:
        result = self._coll.update_many(
            {"user_id": user_id, "status": "pending"},
            {"$set": {"status": "cancelled"}},
        )
        return getattr(result, "modified_count", 0)

    def execute_confirmed(
        self, *, user_id: str,
        executor: Callable[[str, dict[str, Any], str], Any],
    ) -> list[dict[str, Any]]:
        """Execute the oldest active confirmation for the user.

        One confirm reply maps to exactly one confirmation (the per-turn
        one-write rule), with one exception: all pending
        ``calendar.create_my_event`` confirmations are merged into a single
        batch and executed together, so the owner can create several events
        (possibly split across sub-tasks by the planner) with one "yes".

        The executor may return either a tuple ``(ok, reply, error_code)``
        (write_executors contract) or an object with ``.ok`` / ``.data`` /
        ``.error_code``.  CAS failures (ok=False, error_code=stale_revision)
        are reported without touching other pending confirmations.
        """
        actives = self.list_active(user_id=user_id)
        if not actives:
            return []
        actives.sort(key=lambda rec: rec.get("created_at", 0.0))
        rec = actives[0]
        cid = rec["_id"]
        tool_name = rec["tool_name"]
        arguments = rec["arguments"]
        # Atomically claim the confirmation (CAS pending -> executing).
        # If modified_count == 0 another worker already took it or it was
        # cancelled; skip to avoid double-execution under concurrency.
        claim = self._coll.update_one(
            {"_id": cid, "status": "pending"},
            {"$set": {"status": "executing"}},
        )
        if getattr(claim, "modified_count", 0) == 0:
            return []
        merged_batch: list[dict[str, Any]] = list(rec.get("batch") or [])
        merged_siblings: list[str] = []
        if tool_name.startswith("calendar."):
            # Merge every other pending calendar write confirmation into this
            # batch so one confirmation applies all requested changes
            # (create/update/cancel may be mixed across sub-tasks).
            merged_batch.insert(0, {
                "tool": tool_name,
                "arguments": dict(arguments),
                "data": dict(rec.get("payload") or {}),
            })
            for other in actives[1:]:
                other_tool = str(other.get("tool_name") or "")
                if not other_tool.startswith("calendar."):
                    continue
                merged_batch.append({
                    "tool": other_tool,
                    "arguments": dict(other.get("arguments") or {}),
                    "data": dict(other.get("payload") or {}),
                })
                merged_siblings.append(other["_id"])
        results: list[dict[str, Any]] = []
        try:
            tool_result = executor(
                tool_name, arguments, user_id,
                {
                    **(rec.get("payload") or {}),
                    "batch": merged_batch,
                    "_confirmation_id": cid,
                },
            )
        except Exception as exc:
            self._coll.update_one({"_id": cid}, {"$set": {"status": "failed", "error": str(exc)}})
            results.append({"confirmation_id": cid, "ok": False, "error_code": "executor_exception"})
            return results
        if isinstance(tool_result, tuple):
            ok = bool(tool_result[0])
            data = {"reply": str(tool_result[1] or "")} if len(tool_result) > 1 else {}
            error_code = tool_result[2] if len(tool_result) > 2 else None
        else:
            ok = bool(getattr(tool_result, "ok", False))
            data = getattr(tool_result, "data", {})
            error_code = getattr(tool_result, "error_code", None)
        new_status = "completed" if ok else "failed"
        self._coll.update_one(
            {"_id": cid, "status": "executing"},
            {"$set": {"status": new_status, "result": data, "error_code": error_code}},
        )
        if ok and merged_siblings:
            # The merged siblings were executed as part of this batch.
            self._coll.update_many(
                {"_id": {"$in": merged_siblings}, "status": "pending"},
                {"$set": {"status": "completed", "merged_into": cid}},
            )
        results.append({
            "confirmation_id": cid,
            "ok": ok,
            "tool_name": tool_name,
            "data": data,
            "error_code": error_code,
        })
        return results