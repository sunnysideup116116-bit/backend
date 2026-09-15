"""Owner-scoped durable task state for App Voice protocol v4."""

from __future__ import annotations

import copy
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .capabilities import ACTIONS, ARGUMENT_SCHEMAS


ACTIVE_STATUSES = frozenset({
    "queued", "running", "waiting_device", "waiting_confirmation",
    "waiting_input", "cancel_requested",
})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "expired"})
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES
DEVICE_WAIT_SECONDS = 600
CONFIRMATION_WAIT_SECONDS = 30
LEASE_SECONDS = 120
TERMINAL_TTL_SECONDS = 7 * 86400
UNDO_SECONDS = 30
SAFE_WRITE_RETRY_STAGES = frozenset({
    "confirmation_expired", "context_stale", "needs_input", "not_ready",
    "dependency_failed", "validation_failed", "permission_denied", "failed",
})
FIELD_LABELS = {
    "contact_name": "對象",
    "query": "搜尋內容",
    "domains": "搜尋範圍",
    "title": "標題",
    "date": "日期",
    "start_time": "開始時間",
    "end_time": "結束時間",
    "location": "地點",
    "notes": "備註",
    "caption": "貼文內容",
    "count": "數量",
    "message": "訊息",
    "name": "名稱",
    "enabled": "開啟狀態",
}


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _now(value: float | None = None) -> float:
    return time.time() if value is None else float(value)


def _expiry(instant: float) -> datetime:
    return datetime.fromtimestamp(instant + TERMINAL_TTL_SECONDS, timezone.utc)


def _retryable(row: dict[str, Any]) -> bool:
    if row.get("status") not in {"failed", "waiting_input", "expired"}:
        return False
    if row.get("risk") != "write":
        return True
    return str(row.get("stage") or "") in SAFE_WRITE_RETRY_STAGES


def _editable_fields(row: dict[str, Any]) -> list[dict[str, Any]]:
    schema = ARGUMENT_SCHEMAS.get(str(row.get("capability_id") or "")) or {}
    arguments = row.get("arguments") if isinstance(row.get("arguments"), dict) else {}
    required = set(str(item) for item in schema.get("required") or [])
    result: list[dict[str, Any]] = []

    def append_fields(
        properties: dict[str, Any], values: dict[str, Any], *, prefix: str = "",
    ) -> None:
        for key, field_schema in properties.items():
            if len(result) >= 8 or key == "target_ref" or not isinstance(field_schema, dict):
                continue
            path = f"{prefix}.{key}" if prefix else str(key)
            field_type = str(field_schema.get("type") or "string")
            current = values.get(key)
            if field_type == "object":
                nested_values = current if isinstance(current, dict) else {}
                append_fields(
                    field_schema.get("properties") or {}, nested_values, prefix=path,
                )
                continue
            if field_type not in {"string", "integer", "boolean", "array"}:
                continue
            row_value: dict[str, Any] = {
                "key": path,
                "label": FIELD_LABELS.get(key, key.replace("_", " "))[:40],
                "type": field_type,
                "required": key in required,
            }
            if current is not None and isinstance(current, (str, int, bool, list)):
                row_value["value"] = copy.deepcopy(current)
            if isinstance(field_schema.get("enum"), list):
                row_value["options"] = copy.deepcopy(field_schema["enum"][:20])
            items = field_schema.get("items")
            if field_type == "array" and isinstance(items, dict) and isinstance(items.get("enum"), list):
                row_value["options"] = copy.deepcopy(items["enum"][:20])
            result.append(row_value)

    append_fields(schema.get("properties") or {}, arguments)
    return result


def _interaction(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("status") not in {"waiting_input", "failed", "expired"}:
        return {}
    stage = str(row.get("stage") or "")
    prompt = {
        "confirmation_expired": "確認已逾時，要重新排入還是結束這項任務？",
        "context_stale": "畫面資料已改變，請回到相關畫面後重試。",
        "needs_input": "這項任務需要更新的對象或資料。",
        "not_ready": "前置內容還沒有準備完成。",
        "dependency_failed": "前一項任務未完成，請修正後再重試。",
        "device_result_unknown": "連線中斷後無法確認寫入結果，請先查看 App 現況。",
        "result_unknown": "Server 重啟後無法確認寫入結果，不會自動重做。",
        "permission_denied": "這項功能尚未授權，可先調整語音助理權限。",
    }.get(stage, "任務沒有完成，可以修改內容後重試。")
    fields = _editable_fields(row) if _retryable(row) else []
    options = []
    if _retryable(row):
        options.append({"action": "retry", "label": "重試"})
    if fields:
        options.append({"action": "edit", "label": "修改後重試"})
    options.append({"action": "dismiss", "label": "結束任務"})
    return {
        "kind": "form" if fields else "choice",
        "prompt": prompt,
        "fields": fields,
        "options": options,
    }


def _set_nested(values: dict[str, Any], path: str, value: Any) -> None:
    parts = str(path).split(".")
    current = values
    for part in parts[:-1]:
        nested = current.get(part)
        if not isinstance(nested, dict):
            nested = {}
            current[part] = nested
        current = nested
    current[parts[-1]] = value


@dataclass(frozen=True)
class TaskCreateResult:
    batch: dict[str, Any]
    tasks: list[dict[str, Any]]


class VoiceTaskService:
    def __init__(
        self, batches: Any, tasks: Any, *, enabled: bool = False,
        worker_count: int = 4, per_user_concurrency: int = 3,
        offload_io: bool = False,
    ) -> None:
        self.batches = batches
        self.tasks = tasks
        self.enabled = bool(enabled)
        self.worker_count = max(1, min(int(worker_count or 4), 16))
        self.per_user_concurrency = max(1, min(int(per_user_concurrency or 3), 8))
        self.offload_io = bool(offload_io)
        self._lock = threading.Lock()
        self._worker_slots = threading.BoundedSemaphore(self.worker_count)

    @classmethod
    def from_database(
        cls, *, enabled: bool, worker_count: int = 4,
        per_user_concurrency: int = 3,
    ) -> "VoiceTaskService":
        if not enabled:
            return cls(
                None, None, enabled=False, worker_count=worker_count,
                per_user_concurrency=per_user_concurrency, offload_io=False,
            )
        from database import db
        return cls(
            db["app_voice_task_batches"],
            db["app_voice_tasks"],
            enabled=enabled, worker_count=worker_count,
            per_user_concurrency=per_user_concurrency, offload_io=True,
        )

    def ensure_indexes(self) -> None:
        if not self.enabled:
            return
        try:
            self.batches.create_index(
                [("user_id", 1), ("created_at", -1)], name="voice_task_batches_owner"
            )
            self.batches.create_index("expires_at", expireAfterSeconds=0, sparse=True)
            self.tasks.create_index(
                [("user_id", 1), ("status", 1), ("updated_at", -1)],
                name="voice_tasks_owner_status",
            )
            self.tasks.create_index(
                [("user_id", 1), ("updated_at", -1)],
                name="voice_tasks_owner_updated",
            )
            self.tasks.create_index(
                [("user_id", 1), ("idempotency_key", 1)],
                unique=True, name="voice_tasks_idempotency",
            )
            self.tasks.create_index("action_id", sparse=True)
            self.tasks.create_index(
                [("status", 1), ("lease_until", 1)], name="voice_tasks_lease",
            )
            self.tasks.create_index("expires_at", expireAfterSeconds=0, sparse=True)
        except Exception as exc:
            print(f"[app-voice-task] index setup skipped: {type(exc).__name__}")

    def create_batch(
        self, *, user_id: str, session_id: str, operations: list[dict[str, Any]],
        now: float | None = None,
    ) -> TaskCreateResult:
        if not self.enabled:
            raise RuntimeError("voice_tasks_disabled")
        if not 1 <= len(operations) <= 8:
            raise ValueError("voice_task_operation_count_invalid")
        instant = _now(now)
        batch_id = uuid.uuid4().hex
        batch_ref = f"batch_{batch_id}"
        batch = {
            "_id": batch_id,
            "batch_id": batch_id,
            "batch_ref": batch_ref,
            "user_id": _text(user_id, 128),
            "origin_session_id": _text(session_id, 128),
            "status": "active",
            "revision": 1,
            "total": len(operations),
            "completed": 0,
            "created_at": instant,
            "updated_at": instant,
        }
        rows: list[dict[str, Any]] = []
        keys: set[str] = set()
        for position, operation in enumerate(operations):
            key = _text(operation.get("operation_key"), 40)
            if not key or key in keys:
                raise ValueError("voice_task_operation_key_invalid")
            keys.add(key)
            depends_on = [_text(item, 40) for item in operation.get("depends_on") or []]
            task_id = uuid.uuid4().hex
            action = dict(operation.get("action") or {})
            row = {
                "_id": task_id,
                "task_id": task_id,
                "task_ref": f"task_{task_id}",
                "batch_id": batch_id,
                "batch_ref": batch_ref,
                "user_id": batch["user_id"],
                "origin_session_id": batch["origin_session_id"],
                "operation_key": key,
                "position": position,
                "depends_on": depends_on,
                "capability_id": _text(action.get("capability_id"), 100),
                "title": _text(action.get("title"), 120),
                "execution_kind": _text(action.get("execution_kind"), 40),
                "risk": _text(action.get("risk"), 20),
                "arguments": copy.deepcopy(operation.get("arguments") or {}),
                "status": "queued",
                "stage": "queued",
                "progress_percent": None,
                "revision": 1,
                "cancellable": bool(action.get("cancellable", True)),
                "declared_cancellable": bool(action.get("cancellable", True)),
                "idempotency_key": _text(
                    operation.get("idempotency_key")
                    or f"voice-task:{batch_id}:{key}", 160,
                ),
                "action_id": "",
                "attempt": 0,
                "lease_id": "",
                "lease_until": 0.0,
                "result_summary": "",
                "error_code": "",
                "interaction": {},
                "undo_action": {},
                "undo_expires_at": 0.0,
                "undo_consumed_at": 0.0,
                "created_at": instant,
                "updated_at": instant,
            }
            rows.append(row)
        for row in rows:
            unknown = set(row["depends_on"]) - keys
            if unknown or row["operation_key"] in row["depends_on"]:
                raise ValueError("voice_task_dependency_invalid")
        previous_serialized = ""
        for row in rows:
            if row.get("risk") == "read":
                continue
            if previous_serialized and previous_serialized not in row["depends_on"]:
                row["depends_on"].append(previous_serialized)
            previous_serialized = str(row["operation_key"])
        graph = {row["operation_key"]: set(row["depends_on"]) for row in rows}
        pending = {key: set(value) for key, value in graph.items()}
        resolved: set[str] = set()
        while pending:
            ready = {key for key, value in pending.items() if value <= resolved}
            if not ready:
                raise ValueError("voice_task_dependency_cycle")
            resolved |= ready
            pending = {key: value for key, value in pending.items() if key not in ready}
        try:
            self.batches.insert_one(copy.deepcopy(batch))
            self.tasks.insert_many([copy.deepcopy(row) for row in rows], ordered=True)
        except Exception:
            try:
                self.batches.delete_one({"_id": batch_id})
                self.tasks.delete_many({"batch_id": batch_id})
            except Exception:
                pass
            raise RuntimeError("voice_task_storage_unavailable")
        return TaskCreateResult(batch=batch, tasks=rows)

    def _rows(self, query: dict[str, Any], *, limit: int = 20) -> list[dict[str, Any]]:
        try:
            cursor = self.tasks.find(query, {"_id": 0}).sort("updated_at", -1).limit(limit)
            return [dict(row) for row in cursor]
        except Exception as exc:
            raise RuntimeError("voice_task_storage_unavailable") from exc

    def list_tasks(
        self, user_id: str, task_filter: str = "active", *, limit: int = 20,
        include_edit_values: bool = True,
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        selected = task_filter if task_filter in {"active", "recent", "all"} else "active"
        query: dict[str, Any] = {"user_id": _text(user_id, 128)}
        if selected == "active":
            query["status"] = {"$in": list(ACTIVE_STATUSES)}
        elif selected == "recent":
            query["status"] = {"$in": list(TERMINAL_STATUSES)}
        return [
            self.project(row, include_edit_values=include_edit_values)
            for row in self._rows(query, limit=max(1, min(limit, 20)))
        ]

    def project(
        self, row: dict[str, Any], *, include_edit_values: bool = True,
    ) -> dict[str, Any]:
        percent = row.get("progress_percent")
        if percent is not None:
            try:
                percent = max(0, min(100, int(percent)))
            except (TypeError, ValueError):
                percent = None
        projected = {
            "task_ref": _text(row.get("task_ref"), 80),
            "batch_ref": _text(row.get("batch_ref"), 80),
            "title": _text(row.get("title"), 120),
            "capability_id": _text(row.get("capability_id"), 100),
            "operation_key": _text(row.get("operation_key"), 40),
            "risk": _text(row.get("risk"), 20),
            "status": _text(row.get("status"), 40),
            "stage": _text(row.get("stage"), 80),
            "progress_percent": percent,
            "revision": max(0, int(row.get("revision", 0) or 0)),
            "cancellable": bool(row.get("cancellable")) and row.get("status") in ACTIVE_STATUSES,
            "retryable": _retryable(row),
            "undoable": (
                row.get("status") == "completed"
                and isinstance(row.get("undo_action"), dict)
                and bool(row.get("undo_action"))
                and not row.get("undo_consumed_at")
                and float(row.get("undo_expires_at", 0) or 0) >= _now()
            ),
            "undo_expires_at": float(row.get("undo_expires_at", 0) or 0),
            "result_summary": _text(row.get("result_summary"), 600),
            "error_code": _text(row.get("error_code"), 80),
            "updated_at": float(row.get("updated_at", 0) or 0),
        }
        interaction = row.get("interaction")
        if not isinstance(interaction, dict) or not interaction:
            interaction = _interaction(row)
        if interaction:
            projected_interaction = copy.deepcopy(interaction)
            if not include_edit_values:
                for field in projected_interaction.get("fields") or []:
                    if isinstance(field, dict):
                        field.pop("value", None)
            projected["interaction"] = projected_interaction
        return projected

    def get_by_action(self, user_id: str, action_id: str) -> dict[str, Any] | None:
        cleaned_action_id = _text(action_id, 128)
        if not cleaned_action_id:
            return None
        try:
            row = self.tasks.find_one({
                "user_id": _text(user_id, 128), "action_id": cleaned_action_id,
            })
            return dict(row) if row else None
        except Exception:
            return None

    def get_task(self, user_id: str, task_id: str) -> dict[str, Any] | None:
        try:
            row = self.tasks.find_one({
                "user_id": _text(user_id, 128), "task_id": _text(task_id, 80),
            })
            return dict(row) if row else None
        except Exception:
            return None

    def get_by_ref(self, user_id: str, task_ref: str) -> dict[str, Any] | None:
        try:
            row = self.tasks.find_one({
                "user_id": _text(user_id, 128), "task_ref": _text(task_ref, 80),
            })
            return dict(row) if row else None
        except Exception:
            return None

    def tasks_for_batch(self, user_id: str, batch_id: str) -> list[dict[str, Any]]:
        try:
            cursor = self.tasks.find({
                "user_id": _text(user_id, 128), "batch_id": _text(batch_id, 80),
            }).sort("position", 1)
            return [dict(row) for row in cursor]
        except Exception as exc:
            raise RuntimeError("voice_task_storage_unavailable") from exc

    def active_count(self, user_id: str, *, risk: str | None = None) -> int:
        if not self.enabled:
            return 0
        query: dict[str, Any] = {
            "user_id": _text(user_id, 128),
            "status": {
                "$in": [
                    "running", "waiting_device", "waiting_confirmation",
                    "cancel_requested",
                ],
            },
        }
        if risk == "read":
            query["risk"] = "read"
        elif risk == "serialized":
            query["risk"] = {"$ne": "read"}
        try:
            return int(self.tasks.count_documents(query))
        except Exception as exc:
            raise RuntimeError("voice_task_storage_unavailable") from exc

    def queued_batch_ids(self, user_id: str, *, limit: int = 20) -> list[str]:
        if not self.enabled:
            return []
        try:
            cursor = self.tasks.find(
                {"user_id": _text(user_id, 128), "status": "queued"},
                {"batch_id": 1, "created_at": 1, "position": 1},
            ).sort([("created_at", 1), ("position", 1)]).limit(160)
            result: list[str] = []
            seen: set[str] = set()
            for row in cursor:
                batch_id = _text(row.get("batch_id"), 80)
                if batch_id and batch_id not in seen:
                    seen.add(batch_id)
                    result.append(batch_id)
                    if len(result) >= max(1, min(int(limit), 20)):
                        break
            return result
        except Exception as exc:
            raise RuntimeError("voice_task_storage_unavailable") from exc

    def ready_queued_tasks(self, user_id: str, batch_id: str) -> list[dict[str, Any]]:
        rows = self.tasks_for_batch(user_id, batch_id)
        status_by_key = {str(row.get("operation_key") or ""): row.get("status") for row in rows}
        ready: list[dict[str, Any]] = []
        for row in rows:
            if row.get("status") != "queued":
                continue
            dependencies = [str(item) for item in row.get("depends_on") or []]
            if any(status_by_key.get(item) in {"failed", "cancelled", "expired"} for item in dependencies):
                self.update(
                    str(row.get("task_id") or ""), status="waiting_input",
                    stage="dependency_failed", error_code="dependency_failed",
                    result_summary="前置任務沒有完成，這項工作已暫停。",
                    expected_statuses={"queued"},
                )
                continue
            if all(status_by_key.get(item) == "completed" for item in dependencies):
                ready.append(row)
        return ready

    def recover_for_session(self, user_id: str) -> list[str]:
        """Resume safe reads; require user input for writes with unknown device outcome."""
        if not self.enabled:
            return []
        owner = _text(user_id, 128)
        instant = _now()
        try:
            rows = list(self.tasks.find({
                "user_id": owner,
                "$or": [
                    {"status": "waiting_device"},
                    {
                        "status": {"$in": ["running", "cancel_requested"]},
                        "lease_until": {"$lte": instant},
                    },
                ],
            }))
        except Exception as exc:
            raise RuntimeError("voice_task_storage_unavailable") from exc
        batches: set[str] = set()
        for row in rows:
            task_id = str(row.get("task_id") or "")
            batch_id = str(row.get("batch_id") or "")
            if batch_id:
                batches.add(batch_id)
            if row.get("status") == "cancel_requested":
                self.update(
                    task_id, status="cancelled", stage="cancelled",
                    result_summary="任務已取消。",
                    expected_statuses={"cancel_requested"},
                )
                continue
            if row.get("risk") == "write":
                self.update(
                    task_id, status="waiting_input", stage="device_result_unknown",
                    error_code="device_result_unknown",
                    result_summary="連線中斷後無法確認寫入結果，請先查看目前狀態。",
                    expected_statuses={str(row.get("status") or "")},
                )
            else:
                self.update(
                    task_id, status="queued", stage="queued", action_id="",
                    result_summary="",
                    expected_statuses={str(row.get("status") or "")},
                )
        return sorted(batches)

    def claim(
        self, task_id: str, *, stage: str, progress_percent: int = 0,
        lease_seconds: int = LEASE_SECONDS, now: float | None = None,
    ) -> dict[str, Any] | None:
        instant = _now(now)
        lease_id = uuid.uuid4().hex
        with self._lock:
            try:
                row = self.tasks.find_one({
                    "task_id": _text(task_id, 80), "status": "queued",
                })
                if not row:
                    return None
                result = self.tasks.update_one(
                    {"_id": row["_id"], "revision": row.get("revision", 0), "status": "queued"},
                    {"$set": {
                        "status": "running", "stage": _text(stage, 80),
                        "progress_percent": max(0, min(100, int(progress_percent))),
                        "lease_id": lease_id,
                        "lease_until": instant + max(15, min(int(lease_seconds), 600)),
                        "cancellable": bool(row.get("cancellable")) and row.get("risk") == "read",
                        "updated_at": instant,
                        "revision": int(row.get("revision", 0) or 0) + 1,
                    }, "$inc": {"attempt": 1}},
                )
                if not getattr(result, "modified_count", 0):
                    return None
                updated = self.tasks.find_one({"_id": row["_id"]})
            except Exception as exc:
                raise RuntimeError("voice_task_storage_unavailable") from exc
        if updated:
            self._refresh_batch(str(updated.get("batch_id") or ""), instant)
            return dict(updated)
        return None

    def renew_lease(
        self, task_id: str, lease_id: str, *, lease_seconds: int = LEASE_SECONDS,
        now: float | None = None,
    ) -> bool:
        instant = _now(now)
        try:
            result = self.tasks.update_one(
                {
                    "task_id": _text(task_id, 80),
                    "lease_id": _text(lease_id, 80),
                    "status": "running",
                },
                {"$set": {
                    "lease_until": instant + max(15, min(int(lease_seconds), 600)),
                    "updated_at": instant,
                }},
            )
            return bool(getattr(result, "modified_count", 0))
        except Exception:
            return False

    def acquire_worker_slot(self, timeout: float = 1.0) -> bool:
        return self._worker_slots.acquire(timeout=max(0.0, min(float(timeout), 5.0)))

    def release_worker_slot(self) -> None:
        try:
            self._worker_slots.release()
        except ValueError:
            pass

    def set_action(
        self, task_id: str, *, action_id: str, status: str = "waiting_device",
        stage: str = "waiting_device", cancellable: bool | None = None,
        expected_statuses: set[str] | frozenset[str] | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        return self.update(
            task_id, status=status, stage=stage, action_id=_text(action_id, 128),
            cancellable=cancellable, expected_statuses=expected_statuses, now=now,
        )

    def update(
        self, task_id: str, *, status: str | None = None, stage: str | None = None,
        progress_percent: int | None = None, result_summary: str | None = None,
        error_code: str | None = None, action_id: str | None = None,
        cancellable: bool | None = None,
        expected_statuses: set[str] | frozenset[str] | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        instant = _now(now)
        with self._lock:
            try:
                row = self.tasks.find_one({"task_id": _text(task_id, 80)})
                if not row:
                    return None
                if expected_statuses is not None and row.get("status") not in expected_statuses:
                    return None
                updates: dict[str, Any] = {
                    "revision": int(row.get("revision", 0) or 0) + 1,
                    "updated_at": instant,
                }
                if status is not None:
                    if status not in ALL_STATUSES:
                        raise ValueError("voice_task_status_invalid")
                    updates["status"] = status
                    updates["cancellable"] = (
                        bool(row.get("cancellable")) and status in ACTIVE_STATUSES
                        if cancellable is None else bool(cancellable) and status in ACTIVE_STATUSES
                    )
                    if status in TERMINAL_STATUSES:
                        updates["completed_at"] = instant
                        updates["expires_at"] = _expiry(instant)
                    if status != "running":
                        updates["lease_id"] = ""
                        updates["lease_until"] = 0.0
                if stage is not None:
                    updates["stage"] = _text(stage, 80)
                if progress_percent is not None:
                    updates["progress_percent"] = max(0, min(100, int(progress_percent)))
                if result_summary is not None:
                    updates["result_summary"] = _text(result_summary, 600)
                if error_code is not None:
                    updates["error_code"] = _text(error_code, 80)
                if action_id is not None:
                    updates["action_id"] = _text(action_id, 128)
                elif cancellable is not None:
                    updates["cancellable"] = bool(cancellable) and updates.get("status", row.get("status")) in ACTIVE_STATUSES
                projected_row = {**row, **updates}
                if projected_row.get("status") in {"waiting_input", "failed", "expired"}:
                    updates["interaction"] = _interaction(projected_row)
                elif status is not None:
                    updates["interaction"] = {}
                update_query: dict[str, Any] = {
                    "_id": row["_id"], "revision": row.get("revision", 0),
                }
                if expected_statuses is not None:
                    update_query["status"] = {"$in": list(expected_statuses)}
                result = self.tasks.update_one(
                    update_query,
                    {"$set": updates},
                )
                if not getattr(result, "modified_count", 0):
                    return None
                updated = self.tasks.find_one({"_id": row["_id"]})
            except ValueError:
                raise
            except Exception as exc:
                raise RuntimeError("voice_task_storage_unavailable") from exc
        if updated:
            self._refresh_batch(str(updated.get("batch_id") or ""), instant)
            return dict(updated)
        return None

    def complete_action(
        self, user_id: str, action_id: str, *, success: bool,
        message: str, error_code: str = "", now: float | None = None,
    ) -> dict[str, Any] | None:
        row = self.get_by_action(user_id, action_id)
        if not row or row.get("status") in TERMINAL_STATUSES:
            return row
        if row.get("status") == "cancel_requested":
            return self.update(
                str(row.get("task_id") or ""),
                status="cancelled", stage="cancelled",
                result_summary="任務已取消；完成結果已捨棄。",
                expected_statuses={"cancel_requested"}, now=now,
            )
        updated = self.update(
            str(row.get("task_id") or ""),
            status="completed" if success else "failed",
            stage="completed" if success else "failed",
            progress_percent=100 if success else None,
            result_summary=message,
            error_code="" if success else error_code or "operation_failed",
            expected_statuses=set(ACTIVE_STATUSES) - {"cancel_requested"},
            now=now,
        )
        if (
            success
            and updated
            and updated.get("capability_id") == "settings.set"
            and (updated.get("arguments") or {}).get("key") in {
                "ui.liquid_glass", "ui.dark_mode",
            }
        ):
            instant = _now(now)
            arguments = dict(updated.get("arguments") or {})
            try:
                result = self.tasks.update_one(
                    {"_id": updated["_id"], "revision": updated.get("revision", 0)},
                    {"$set": {
                        "undo_action": {
                            "capability_id": "settings.set",
                            "arguments": {
                                "key": arguments.get("key"),
                                "enabled": arguments.get("enabled") is not True,
                            },
                        },
                        "undo_expires_at": instant + UNDO_SECONDS,
                        "updated_at": instant,
                    }, "$inc": {"revision": 1}},
                )
                if getattr(result, "modified_count", 0):
                    refreshed = self.tasks.find_one({"_id": updated["_id"]})
                    if refreshed:
                        updated = dict(refreshed)
            except Exception:
                pass
        return updated

    def _owned_task(
        self, user_id: str, task_ref: str, expected_revision: int | None,
    ) -> dict[str, Any]:
        try:
            row = self.tasks.find_one({
                "user_id": _text(user_id, 128),
                "task_ref": _text(task_ref, 80),
            })
        except Exception as exc:
            raise RuntimeError("voice_task_storage_unavailable") from exc
        if not row:
            raise ValueError("voice_task_not_found")
        if (
            expected_revision is not None
            and int(row.get("revision", 0) or 0) != int(expected_revision)
        ):
            raise ValueError("voice_task_revision_stale")
        return dict(row)

    def _reset_to_queued(
        self, row: dict[str, Any], *, arguments: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        instant = _now(now)
        updates = {
            "status": "queued",
            "stage": "queued",
            "progress_percent": None,
            "result_summary": "",
            "error_code": "",
            "action_id": "",
            "lease_id": "",
            "lease_until": 0.0,
            "cancellable": bool(row.get("declared_cancellable", True)),
            "interaction": {},
            "updated_at": instant,
            "revision": int(row.get("revision", 0) or 0) + 1,
        }
        if arguments is not None:
            updates["arguments"] = copy.deepcopy(arguments)
        try:
            result = self.tasks.update_one(
                {"_id": row["_id"], "revision": row.get("revision", 0)},
                {
                    "$set": updates,
                    "$unset": {"completed_at": "", "expires_at": ""},
                },
            )
            if not getattr(result, "modified_count", 0):
                raise ValueError("voice_task_revision_stale")
            updated = self.tasks.find_one({"_id": row["_id"]})
        except ValueError:
            raise
        except Exception as exc:
            raise RuntimeError("voice_task_storage_unavailable") from exc
        self._refresh_batch(str(row.get("batch_id") or ""), instant)
        return dict(updated)

    def retry(
        self, user_id: str, task_ref: str, *, expected_revision: int | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("voice_tasks_disabled")
        with self._lock:
            row = self._owned_task(user_id, task_ref, expected_revision)
            if not _retryable(row):
                raise ValueError("voice_task_not_retryable")
            return self._reset_to_queued(row, now=now)

    def provide_input(
        self, user_id: str, task_ref: str, values: dict[str, Any], *,
        expected_revision: int | None = None, now: float | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("voice_tasks_disabled")
        if not isinstance(values, dict) or not values or len(values) > 8:
            raise ValueError("voice_task_input_invalid")
        with self._lock:
            row = self._owned_task(user_id, task_ref, expected_revision)
            if not _retryable(row):
                raise ValueError("voice_task_not_editable")
            editable = {field["key"] for field in _editable_fields(row)}
            if set(str(key) for key in values) - editable:
                raise ValueError("voice_task_input_field_invalid")
            arguments = copy.deepcopy(row.get("arguments") or {})
            for key, value in values.items():
                _set_nested(arguments, str(key), copy.deepcopy(value))
            # Import lazily to keep the durable state module independent from
            # model routing during startup.
            from .capability_proxy import proposal_from_capability
            proposal = proposal_from_capability(
                str(row.get("capability_id") or ""), arguments, revision=0,
            )
            if proposal is None:
                raise ValueError("voice_task_input_validation_failed")
            return self._reset_to_queued(
                row, arguments=dict(proposal.arguments), now=now,
            )

    def dismiss(
        self, user_id: str, task_ref: str, *, expected_revision: int | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("voice_tasks_disabled")
        row = self._owned_task(user_id, task_ref, expected_revision)
        if row.get("status") not in {"waiting_input", "failed", "expired"}:
            raise ValueError("voice_task_not_dismissible")
        updated = self.update(
            str(row.get("task_id") or ""), status="cancelled", stage="dismissed",
            result_summary="使用者已結束這項任務。",
            expected_statuses={str(row.get("status") or "")}, now=now,
        )
        if updated is None:
            raise ValueError("voice_task_revision_stale")
        return updated

    def create_undo_batch(
        self, user_id: str, task_ref: str, *, session_id: str,
        expected_revision: int | None = None, now: float | None = None,
    ) -> TaskCreateResult:
        if not self.enabled:
            raise RuntimeError("voice_tasks_disabled")
        instant = _now(now)
        with self._lock:
            row = self._owned_task(user_id, task_ref, expected_revision)
            undo_action = row.get("undo_action")
            if (
                row.get("status") != "completed"
                or not isinstance(undo_action, dict)
                or not undo_action
                or row.get("undo_consumed_at")
                or float(row.get("undo_expires_at", 0) or 0) < instant
            ):
                raise ValueError("voice_task_not_undoable")
            capability_id = str(undo_action.get("capability_id") or "")
            spec = ACTIONS.get(capability_id)
            if capability_id != "settings.set" or not isinstance(spec, dict):
                raise ValueError("voice_task_undo_invalid")
            created = self.create_batch(
                user_id=user_id,
                session_id=session_id,
                operations=[{
                    "operation_key": "undo",
                    "depends_on": [],
                    "arguments": copy.deepcopy(undo_action.get("arguments") or {}),
                    "action": {
                        "capability_id": capability_id,
                        "title": f"復原：{_text(row.get('title'), 100)}",
                        "execution_kind": str(spec.get("execution_kind") or "inline_device"),
                        "risk": str(spec.get("risk") or "write"),
                        "cancellable": bool(spec.get("cancellable", True)),
                    },
                }],
                now=instant,
            )
            try:
                result = self.tasks.update_one(
                    {"_id": row["_id"], "revision": row.get("revision", 0)},
                    {"$set": {
                        "undo_consumed_at": instant,
                        "updated_at": instant,
                    }, "$inc": {"revision": 1}},
                )
                if not getattr(result, "modified_count", 0):
                    raise ValueError("voice_task_revision_stale")
            except Exception:
                try:
                    self.tasks.delete_many({"batch_id": created.batch["batch_id"]})
                    self.batches.delete_one({"batch_id": created.batch["batch_id"]})
                except Exception:
                    pass
                raise
            return created

    def cancel(
        self, user_id: str, task_ref: str, *, expected_revision: int | None = None,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            raise RuntimeError("voice_tasks_disabled")
        owner = _text(user_id, 128)
        ref = _text(task_ref, 80)
        query: dict[str, Any] = {"user_id": owner}
        if ref.startswith("batch_"):
            query["batch_ref"] = ref
            if expected_revision is not None:
                try:
                    batch = self.batches.find_one({
                        "user_id": owner, "batch_ref": ref,
                    })
                except Exception as exc:
                    raise RuntimeError("voice_task_storage_unavailable") from exc
                if (
                    not batch
                    or int(batch.get("revision", 0) or 0) != expected_revision
                ):
                    raise ValueError("voice_task_revision_stale")
        elif ref.startswith("task_"):
            query["task_ref"] = ref
        else:
            raise ValueError("voice_task_ref_invalid")
        rows = self._rows(query, limit=20)
        if not rows:
            raise ValueError("voice_task_not_found")
        if expected_revision is not None and len(rows) == 1 and int(rows[0].get("revision", 0) or 0) != expected_revision:
            raise ValueError("voice_task_revision_stale")
        changed: list[dict[str, Any]] = []
        for row in rows:
            if row.get("status") in TERMINAL_STATUSES:
                changed.append(row)
                continue
            if not row.get("cancellable"):
                changed.append(row)
                continue
            next_status = "cancel_requested" if row.get("status") == "running" else "cancelled"
            updated = self.update(
                str(row.get("task_id") or ""), status=next_status,
                stage=next_status, result_summary="使用者已要求取消。", now=now,
                expected_statuses={str(row.get("status") or "")},
            )
            changed.append(updated or row)
        return [self.project(row) for row in changed]

    def expire_waiting_device(self, *, now: float | None = None) -> int:
        if not self.enabled:
            return 0
        instant = _now(now)
        query = {
            "status": "waiting_device",
            "updated_at": {"$lte": instant - DEVICE_WAIT_SECONDS},
        }
        try:
            batches = {
                str(row.get("batch_id") or "")
                for row in self.tasks.find(query, {"batch_id": 1})
            }
            result = self.tasks.update_many(
                query,
                {"$set": {
                    "status": "expired", "stage": "expired",
                    "updated_at": instant, "completed_at": instant,
                    "expires_at": _expiry(instant),
                    "cancellable": False, "error_code": "device_unavailable",
                    "result_summary": "App 長時間沒有回傳結果，任務已過期。",
                }, "$inc": {"revision": 1}},
            )
            for batch_id in batches:
                self._refresh_batch(batch_id, instant)
            return int(getattr(result, "modified_count", 0) or 0)
        except Exception:
            return 0

    def expire_waiting_confirmations(self, *, now: float | None = None) -> int:
        if not self.enabled:
            return 0
        instant = _now(now)
        query = {
            "status": "waiting_confirmation",
            "updated_at": {"$lte": instant - CONFIRMATION_WAIT_SECONDS},
        }
        try:
            batches = {
                str(row.get("batch_id") or "")
                for row in self.tasks.find(query, {"batch_id": 1})
            }
            result = self.tasks.update_many(
                query,
                {"$set": {
                    "status": "waiting_input", "stage": "confirmation_expired",
                    "updated_at": instant, "lease_id": "", "lease_until": 0.0,
                    "error_code": "confirmation_expired",
                    "result_summary": "確認已逾時，請重新說明是否繼續。",
                }, "$inc": {"revision": 1}},
            )
            for batch_id in batches:
                self._refresh_batch(batch_id, instant)
            return int(getattr(result, "modified_count", 0) or 0)
        except Exception:
            return 0

    def recover_expired_leases(self, *, now: float | None = None) -> int:
        if not self.enabled:
            return 0
        instant = _now(now)
        query = {
            "status": {"$in": ["running", "cancel_requested"]},
            "lease_until": {"$lte": instant},
        }
        try:
            rows = list(self.tasks.find(query, {"task_id": 1, "batch_id": 1}))
        except Exception:
            return 0
        changed = 0
        for row in rows:
            current = self.get_task_by_id(str(row.get("task_id") or ""))
            if not current or current.get("status") not in {"running", "cancel_requested"}:
                continue
            if current.get("status") == "cancel_requested":
                next_status, stage, error = "cancelled", "cancelled", ""
                summary = "任務已取消。"
            elif current.get("risk") == "read":
                next_status, stage, error = "queued", "recovered", ""
                summary = "伺服器重啟後已排回等待佇列。"
            else:
                next_status, stage, error = "waiting_input", "result_unknown", "result_unknown"
                summary = "伺服器重啟後無法確認寫入結果，請先查看目前狀態。"
            if self.update(
                str(current.get("task_id") or ""), status=next_status, stage=stage,
                error_code=error, result_summary=summary, now=instant,
                expected_statuses={str(current.get("status") or "")},
            ):
                changed += 1
        return changed

    def get_task_by_id(self, task_id: str) -> dict[str, Any] | None:
        try:
            row = self.tasks.find_one({"task_id": _text(task_id, 80)})
            return dict(row) if row else None
        except Exception:
            return None

    def _refresh_batch(self, batch_id: str, instant: float) -> None:
        if not batch_id:
            return
        try:
            rows = list(self.tasks.find({"batch_id": batch_id}, {"status": 1}))
            terminal = sum(1 for row in rows if row.get("status") in TERMINAL_STATUSES)
            failed = any(row.get("status") == "failed" for row in rows)
            cancelled = bool(rows) and all(row.get("status") == "cancelled" for row in rows)
            status = (
                "failed" if terminal == len(rows) and failed
                else "cancelled" if terminal == len(rows) and cancelled
                else "completed" if rows and terminal == len(rows)
                else "active"
            )
            update: dict[str, Any] = {
                "status": status, "completed": terminal, "updated_at": instant,
            }
            if status != "active":
                update.update({
                    "completed_at": instant,
                    "expires_at": _expiry(instant),
                })
            mutation: dict[str, Any] = {
                "$set": update,
                "$inc": {"revision": 1},
            }
            if status == "active":
                mutation["$unset"] = {"completed_at": "", "expires_at": ""}
            self.batches.update_one({"batch_id": batch_id}, mutation)
        except Exception:
            pass


_worker_thread: threading.Thread | None = None
_worker_stop = threading.Event()


def start_voice_task_worker(service: VoiceTaskService) -> None:
    global _worker_thread
    if not service.enabled or (_worker_thread and _worker_thread.is_alive()):
        return
    service.ensure_indexes()
    _worker_stop.clear()

    def run() -> None:
        while not _worker_stop.wait(5.0):
            service.expire_waiting_device()
            service.expire_waiting_confirmations()
            service.recover_expired_leases()

    _worker_thread = threading.Thread(target=run, name="app-voice-task-worker", daemon=True)
    _worker_thread.start()


def stop_voice_task_worker() -> None:
    global _worker_thread
    _worker_stop.set()
    if _worker_thread is not None:
        _worker_thread.join(timeout=2)
    _worker_thread = None
