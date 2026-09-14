"""Pi multi-operation queue tools.

The queue stores explicit natural-language goals and execution progress only;
it is not a conversational reference or hidden target-selection state machine.
"""
from __future__ import annotations

from typing import Any

from services.ayue_agent.shared.confirmation import SURFACE_PUBLIC


TOOL_NAMES = frozenset({"workflow.queue_operations", "workflow.manage_operations"})
PROMPT = """【多項工作】
明確的 2–4 項需求才用 workflow.queue_operations 保存自然語言目標；單項不可建立佇列。
一句以「先…然後／再／接著」提出兩項以上工作時，第一輪只能呼叫 workflow.queue_operations，不能同批先呼叫任何業務工具。
看到 queue 成功 observation 後，才處理 current_operation；其餘項目不能提前準備或授權。
佇列不解析「他／第二間」，也不提前授權寫入；每次最多一個待處理互動。
只有使用者明確要求時才繼續、暫停、取消全部或更正目前項目。"""

SCHEMAS = (
    {
        "name": "workflow.queue_operations",
        "description": "保存使用者明確提出的 2–4 項自然語言目標，並從第一項開始逐項處理。單一需求不可使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array", "minItems": 2, "maxItems": 4,
                    "items": {
                        "oneOf": [
                            {"type": "string", "minLength": 1, "maxLength": 600},
                            {
                                "type": "object",
                                "properties": {
                                    "request": {"type": "string", "maxLength": 600},
                                    "description": {"type": "string", "maxLength": 600},
                                    "kind": {"type": "string", "maxLength": 40},
                                },
                                "anyOf": [{"required": ["request"]}, {"required": ["description"]}],
                                "additionalProperties": False,
                            },
                        ],
                    },
                },
            },
            "required": ["operations"],
            "additionalProperties": False,
        },
    },
    {
        "name": "workflow.manage_operations",
        "description": "依使用者明確要求繼續、暫停、取消全部或更正目前的 Pi 多項工作。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["continue", "pause", "cancel_all", "correct_current"],
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
)


def _queue(runtime: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    operations = arguments.get("operations")
    if not isinstance(operations, list) or not 2 <= len(operations) <= 4:
        return runtime.project(name="workflow.queue_operations", status="failed", error_code="tool_schema_invalid")
    safe_operations: list[dict[str, str]] = []
    for item in operations:
        if isinstance(item, str):
            request = item.strip()[:600]
            if not request:
                return runtime.project(name="workflow.queue_operations", status="failed", error_code="tool_schema_invalid")
            safe_operations.append({"request": request, "kind": "task"})
            continue
        if not isinstance(item, dict):
            return runtime.project(name="workflow.queue_operations", status="failed", error_code="tool_schema_invalid")
        request = str(item.get("request") or item.get("description") or "").strip()[:600]
        kind = str(item.get("kind") or "task").strip()[:40]
        if not request or set(item) - {"request", "description", "kind"}:
            return runtime.project(name="workflow.queue_operations", status="failed", error_code="tool_schema_invalid")
        safe_operations.append({"request": request, "kind": kind})
    try:
        batch = runtime.operation_batches.create_batch(
            user_id=runtime.turn.user_id,
            room_id=runtime.turn.room_id,
            surface=SURFACE_PUBLIC,
            operations=safe_operations,
            source_message=str(runtime.turn.message or "")[:1600],
            source_sent_at=runtime.turn.clock.local_iso,
            timezone=runtime.turn.clock.timezone,
            source_clock=runtime.turn.clock.model_dump(mode="json"),
            context_messages=list(runtime.turn.recent_messages or []),
            origin_run_id=runtime.run_id,
            source_engine="pi",
        )
    except Exception:
        return runtime.project(
            name="workflow.queue_operations", status="failed",
            result={"batch_status": "unavailable"}, error_code="operation_batch_storage_unavailable",
        )
    current = runtime.operation_batches.current_item(batch) or {}
    object.__setattr__(runtime.turn, "_operation_batch_id", str(batch.get("_id") or ""))
    object.__setattr__(runtime.turn, "_operation_batch_revision", int(batch.get("revision", 0) or 0))
    object.__setattr__(runtime.turn, "_operation_item_id", str(current.get("item_id") or ""))
    object.__setattr__(runtime.turn, "_operation_batch_context", runtime.operation_batches.prompt_context(batch))
    return runtime.project(name="workflow.queue_operations", status="ok", result={
        "batch_status": "active",
        "current_operation": {
            "position": 1, "total": len(safe_operations),
            "kind": str(current.get("kind") or "task"),
            "request": str(current.get("request") or "")[:600],
        },
    })


def _manage(runtime: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    action = str(arguments.get("action") or "")
    if action not in {"continue", "pause", "cancel_all", "correct_current"} or set(arguments) != {"action"}:
        return runtime.project(name="workflow.manage_operations", status="failed", error_code="tool_schema_invalid")
    lookup_status, pending = runtime.operation_batches.pending_lookup(
        user_id=runtime.turn.user_id, room_id=runtime.turn.room_id, surface=SURFACE_PUBLIC,
    )
    if lookup_status == "unavailable":
        return runtime.project(name="workflow.manage_operations", status="failed",
                               result={"batch_status": "unavailable"},
                               error_code="operation_batch_storage_unavailable")
    batch = pending[0] if pending else None
    if not batch:
        return runtime.project(name="workflow.manage_operations", status="failed",
                               result={"batch_status": "not_found"}, error_code="operation_batch_not_found")
    if str(batch.get("source_engine") or "dag") != "pi":
        return runtime.project(name="workflow.manage_operations", status="failed",
                               result={"batch_status": "engine_mismatch"},
                               error_code="operation_batch_engine_mismatch")
    batch_id = str(batch.get("_id") or "")
    try:
        if action == "cancel_all":
            updated = runtime.operation_batches.cancel_all(batch_id)
        elif action == "pause":
            updated = runtime.operation_batches.pause(batch_id, reason="user_requested")
        elif action == "correct_current":
            updated = runtime.operation_batches.append_correction(
                batch_id, str(runtime.turn.message or ""),
                sent_at=runtime.turn.clock.local_iso,
                clock_context=runtime.turn.clock.model_dump(mode="json"),
            )
        else:
            updated = runtime.operation_batches.activate(batch_id)
    except Exception:
        updated = None
    if not updated:
        return runtime.project(name="workflow.manage_operations", status="failed",
                               result={"batch_status": "unavailable"},
                               error_code="operation_batch_storage_unavailable")
    current = runtime.operation_batches.current_item(updated) or {}
    if updated.get("status") == "active":
        object.__setattr__(runtime.turn, "_operation_batch_id", batch_id)
        object.__setattr__(runtime.turn, "_operation_batch_revision", int(updated.get("revision", 0) or 0))
        object.__setattr__(runtime.turn, "_operation_item_id", str(current.get("item_id") or ""))
        object.__setattr__(runtime.turn, "_operation_batch_context", runtime.operation_batches.prompt_context(updated))
    return runtime.project(name="workflow.manage_operations", status="ok", result={
        "batch_status": str(updated.get("status") or "unknown"),
        "action": action,
        "current_operation": {
            "position": int(updated.get("current_index", 0) or 0) + 1,
            "request": str(current.get("request") or "")[:600],
        } if current else None,
    })


def handle(runtime: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return _queue(runtime, arguments) if name == "workflow.queue_operations" else _manage(runtime, arguments)
