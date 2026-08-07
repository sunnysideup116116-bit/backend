"""Preview-bound confirmation manager with one active action per user."""

from __future__ import annotations

import hashlib
import json
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
    origin_run_id: str
    request_fingerprint: str
    preview_fingerprint: str


class ConfirmationManager:
    """Owns one preview-bound pending confirmation per user with CAS execution."""

    def __init__(self, collection: Any) -> None:
        self._coll = collection

    def create_confirmation(
        self, *, user_id: str, agent_name: str, tool_name: str,
        arguments: dict[str, Any], ttl_seconds: int = 900,
        payload: dict[str, Any] | None = None,
        origin_run_id: str,
        preview: str,
    ) -> str:
        if not origin_run_id.strip() or not preview.strip():
            raise ValueError("confirmation requires an origin run and visible preview")
        confirmation_id = uuid.uuid4().hex
        now = time.time()
        safe_payload = payload or {}
        request_fingerprint = self._request_fingerprint(
            tool_name=tool_name,
            arguments=arguments,
            payload=safe_payload,
            origin_run_id=origin_run_id,
        )
        preview_fingerprint = hashlib.sha256(preview.encode("utf-8")).hexdigest()
        # A plain "confirm" reply must have exactly one visible target.  A new
        # preview therefore supersedes every older pending preview for the user.
        self._coll.update_many(
            {"user_id": user_id, "status": "pending"},
            {"$set": {"status": "superseded", "superseded_at": now}},
        )
        self._coll.insert_one({
            "_id": confirmation_id,
            "user_id": user_id,
            "agent_name": agent_name,
            "tool_name": tool_name,
            "arguments": arguments,
            "payload": safe_payload,
            "status": "pending",
            "created_at": now,
            "expires_at": now + ttl_seconds,
            "origin_run_id": origin_run_id,
            "request_fingerprint": request_fingerprint,
            "preview_fingerprint": preview_fingerprint,
        })
        return confirmation_id

    @staticmethod
    def _request_fingerprint(
        *, tool_name: str, arguments: dict[str, Any], payload: dict[str, Any], origin_run_id: str,
    ) -> str:
        canonical = json.dumps(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "payload": payload,
                "origin_run_id": origin_run_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def list_active(self, *, user_id: str) -> list[dict[str, Any]]:
        now = time.time()
        try:
            cursor = self._coll.find({
                "user_id": user_id,
                "status": "pending",
                "expires_at": {"$gt": now},
            })
            return list(cursor)
        except Exception:
            # Confirmation lookup is safety-critical.  On storage failure, an
            # empty projection prevents both stale execution and planner leaks.
            return []

    def planner_projection(self, *, user_id: str) -> list[dict[str, Any]]:
        """Return bounded metadata only; never expose IDs, args, or payloads."""
        now = time.time()
        actives = self.list_active(user_id=user_id)
        if not actives:
            return []
        latest = max(actives, key=lambda rec: float(rec.get("created_at", 0) or 0))
        tool_name = str(latest.get("tool_name") or "")
        return [{
            "has_pending": True,
            "domain": tool_name.split(".", 1)[0] if "." in tool_name else "action",
            "expires_in_seconds": max(
                0, min(900, int(float(latest.get("expires_at", now) or now) - now)),
            ),
        }]

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
        """Execute the one active, preview-bound confirmation for the user."""
        actives = self.list_active(user_id=user_id)
        if not actives:
            return []
        actives.sort(key=lambda rec: rec.get("created_at", 0.0), reverse=True)
        rec = actives[0]
        cid = rec["_id"]
        tool_name = rec["tool_name"]
        arguments = rec["arguments"]
        payload = dict(rec.get("payload") or {})
        origin_run_id = str(rec.get("origin_run_id") or "")
        stored_fingerprint = str(rec.get("request_fingerprint") or "")
        preview_fingerprint = str(rec.get("preview_fingerprint") or "")
        expected_fingerprint = self._request_fingerprint(
            tool_name=tool_name,
            arguments=arguments,
            payload=payload,
            origin_run_id=origin_run_id,
        )
        if (
            not origin_run_id
            or not preview_fingerprint
            or not stored_fingerprint
            or stored_fingerprint != expected_fingerprint
        ):
            self._coll.update_one(
                {"_id": cid, "user_id": user_id, "status": "pending"},
                {"$set": {"status": "failed", "error_code": "confirmation_unbound"}},
            )
            return [{
                "confirmation_id": cid,
                "ok": False,
                "tool_name": tool_name,
                "error_code": "confirmation_unbound",
            }]
        # Atomically claim the confirmation (CAS pending -> executing).
        # If modified_count == 0 another worker already took it or it was
        # cancelled; skip to avoid double-execution under concurrency.
        claim = self._coll.update_one(
            {
                "_id": cid,
                "user_id": user_id,
                "status": "pending",
                "request_fingerprint": stored_fingerprint,
            },
            {"$set": {"status": "executing"}},
        )
        if getattr(claim, "modified_count", 0) == 0:
            return []
        results: list[dict[str, Any]] = []
        try:
            tool_result = executor(
                tool_name, arguments, user_id,
                {
                    **payload,
                    "_confirmation_id": cid,
                },
            )
        except Exception as exc:
            self._coll.update_one(
                {"_id": cid, "user_id": user_id, "status": "executing"},
                {"$set": {"status": "failed", "error_code": "executor_exception"}},
            )
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
            {
                "_id": cid,
                "user_id": user_id,
                "status": "executing",
                "request_fingerprint": stored_fingerprint,
            },
            {"$set": {"status": new_status, "result": data, "error_code": error_code}},
        )
        results.append({
            "confirmation_id": cid,
            "ok": ok,
            "tool_name": tool_name,
            "data": data,
            "error_code": error_code,
        })
        return results
