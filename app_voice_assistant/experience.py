"""Deterministic App Voice experience metadata and workflow expansion.

The model only selects signed catalog capabilities.  Everything in this file is
server-owned: coaching copy, permission repair, and workflow step expansion are
never accepted from the model or Flutter client.
"""

from __future__ import annotations

import copy
import hashlib
from typing import Any

from .capabilities import ACTIONS


MAX_EXPANDED_OPERATIONS = 8

PERMISSION_LABELS = {
    "navigation": "App 頁面導覽",
    "screen_read": "目前畫面讀取",
    "profile": "個人資料",
    "settings": "App 設定",
    "post_draft": "貼文草稿",
    "post_publish": "貼文發布",
    "gallery": "相簿最近照片",
    "match_read": "配對狀態讀取",
    "match_actions": "配對操作",
    "match_ayue": "配對阿月",
    "public_ayue": "公開阿月",
    "private_ayue": "私人阿月",
    "chat_list": "聯絡人清單",
    "chat_content": "聊天內容",
    "chat_send": "傳送訊息",
    "calendar_read": "行事曆讀取",
    "calendar_write": "行事曆變更",
    "web_search": "Web 搜尋",
    "places": "地點搜尋",
    "location_precise": "精確定位",
    "memory_read": "阿月記憶讀取",
    "memory_write": "阿月記憶新增",
    "status_read": "App 狀態讀取",
    "safety_actions": "封鎖與解除封鎖",
}


def permission_repair(missing_permissions: list[str]) -> dict[str, Any]:
    """Return UI-safe repair instructions without granting authority."""
    unique = list(dict.fromkeys(
        str(item) for item in missing_permissions if str(item) in PERMISSION_LABELS
    ))[:8]
    if not unique:
        return {}
    labels = [PERMISSION_LABELS[item] for item in unique]
    return {
        "kind": "voice_permission",
        "title": "需要開啟語音權限",
        "message": f"開啟{'、'.join(labels)}後，我才能完成這項操作。",
        "missing_permissions": unique,
        "permission_labels": labels,
        "destination": "voice_settings",
        "steps": [
            "開啟「阿月語音助理」設定",
            "只開啟你願意授權的項目",
            "回到語音模式後重新提出需求",
        ],
    }


def coach_for_match(match: dict[str, Any]) -> dict[str, Any]:
    """Project one catalog guide into a small, client-renderable coach card."""
    steps = [str(item).strip()[:240] for item in match.get("steps") or []]
    if not steps:
        return {}
    destination = str(match.get("destination") or "")
    projected_steps = []
    for index, instruction in enumerate(steps[:6]):
        projected_steps.append({
            "index": index,
            "title": f"步驟 {index + 1}",
            "instruction": instruction,
            **(
                {"destination": destination}
                if index == 0 and destination
                else {}
            ),
        })
    return {
        "guide_id": str(match.get("guide_id") or "")[:80],
        "title": str(match.get("title") or "App 操作指南")[:80],
        "summary": str(match.get("summary") or "")[:500],
        "current_step": 0,
        "steps": projected_steps,
    }


def discovery_experience(
    matches: list[dict[str, Any]], *, mode: str,
) -> dict[str, Any]:
    """Build optional coach and permission-repair payloads for one search."""
    result: dict[str, Any] = {}
    if matches and mode == "explain":
        coach = coach_for_match(matches[0])
        if coach:
            result["coach"] = coach
    missing = [
        str(permission)
        for match in matches[:1]
        for action in match.get("actions") or []
        if action.get("available") is False
        for permission in action.get("missing_permissions") or []
    ]
    repair = permission_repair(missing)
    if repair:
        result["permission_repair"] = repair
    return result


def _get_path(values: dict[str, Any], path: str) -> Any:
    current: Any = values
    for part in str(path).split("."):
        if not isinstance(current, dict) or part not in current:
            raise ValueError("workflow_argument_missing")
        current = current[part]
    return copy.deepcopy(current)


def _set_path(values: dict[str, Any], path: str, value: Any) -> None:
    parts = str(path).split(".")
    current = values
    for part in parts[:-1]:
        nested = current.get(part)
        if not isinstance(nested, dict):
            nested = {}
            current[part] = nested
        current = nested
    current[parts[-1]] = value


def _expanded_key(outer_key: str, index: int, step_key: str) -> str:
    # Mongo task operation keys are capped at 40 characters.  The numeric part
    # keeps keys deterministic and unique even when human labels are truncated.
    prefix = str(outer_key)[:20]
    suffix = str(step_key)[:6]
    digest = hashlib.sha256(
        f"{outer_key}:{index}:{step_key}".encode("utf-8"),
    ).hexdigest()[:6]
    return f"{prefix}__{index + 1}_{suffix}_{digest}"[:40]


def expand_catalog_operations(
    operations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expand trusted workflow specs into ordinary catalog operations.

    Input operations must already have a verified capability ref and canonical
    arguments.  The returned action IDs are validated again by the normal
    proposal path before task creation.
    """
    if not 1 <= len(operations) <= MAX_EXPANDED_OPERATIONS:
        raise ValueError("operation_count_invalid")
    outer_keys = [str(item.get("operation_key") or "") for item in operations]
    if any(not key for key in outer_keys) or len(set(outer_keys)) != len(outer_keys):
        raise ValueError("operation_key_invalid")
    unknown_dependencies = {
        str(dependency)
        for item in operations
        for dependency in item.get("depends_on") or []
        if str(dependency) not in outer_keys
    }
    if unknown_dependencies:
        raise ValueError("operation_dependency_invalid")

    groups: dict[str, list[dict[str, Any]]] = {}
    terminal_keys: dict[str, str] = {}
    for operation in operations:
        outer_key = str(operation["operation_key"])
        action_id = str(operation.get("action_id") or "")
        action = ACTIONS.get(action_id)
        if not isinstance(action, dict):
            raise ValueError("operation_action_invalid")
        workflow_steps = action.get("workflow_steps")
        if not workflow_steps:
            row = {
                "operation_key": outer_key,
                "depends_on": [],
                "external_depends_on": [
                    str(item) for item in operation.get("depends_on") or []
                ],
                "action_id": action_id,
                "arguments": copy.deepcopy(operation.get("arguments") or {}),
            }
            groups[outer_key] = [row]
            terminal_keys[outer_key] = outer_key
            continue

        expanded: list[dict[str, Any]] = []
        local_keys: dict[str, str] = {}
        supplied = dict(operation.get("arguments") or {})
        for index, raw_step in enumerate(workflow_steps):
            if not isinstance(raw_step, dict):
                raise ValueError("workflow_step_invalid")
            step_name = str(raw_step.get("key") or f"step{index + 1}")
            step_key = _expanded_key(outer_key, index, step_name)
            local_keys[step_name] = step_key
            step_arguments = copy.deepcopy(raw_step.get("arguments") or {})
            for target, source in (raw_step.get("argument_map") or {}).items():
                _set_path(step_arguments, str(target), _get_path(supplied, str(source)))
            expanded.append({
                "operation_key": step_key,
                "depends_on": [],
                "external_depends_on": (
                    [str(item) for item in operation.get("depends_on") or []]
                    if index == 0 else []
                ),
                "local_depends_on": [
                    str(item) for item in raw_step.get("depends_on") or []
                ],
                "action_id": str(raw_step.get("action_id") or ""),
                "arguments": step_arguments,
            })
        for row in expanded:
            local_dependencies = row.pop("local_depends_on", [])
            try:
                row["depends_on"] = [local_keys[item] for item in local_dependencies]
            except KeyError as exc:
                raise ValueError("workflow_dependency_invalid") from exc
        groups[outer_key] = expanded
        terminal_keys[outer_key] = expanded[-1]["operation_key"]

    result: list[dict[str, Any]] = []
    for outer_key in outer_keys:
        for row in groups[outer_key]:
            external = row.pop("external_depends_on", [])
            row["depends_on"].extend(terminal_keys[item] for item in external)
            if row["action_id"] not in ACTIONS:
                raise ValueError("workflow_action_unknown")
            if ACTIONS[row["action_id"]].get("workflow_steps"):
                raise ValueError("workflow_nested_not_allowed")
            result.append(row)
    if not 1 <= len(result) <= MAX_EXPANDED_OPERATIONS:
        raise ValueError("expanded_operation_count_invalid")
    return result
