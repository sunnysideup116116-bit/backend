"""Closed tool registry for the Private Pi surface.

Tool names are surface-specific even when their canonical domain service is
shared with Public Ayue.  The model never receives executor authority fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PrivateToolBinding:
    name: str
    description: str
    parameters: dict[str, Any]
    progress_text: str
    risk: str = "read"


def _empty() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _scope() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"scope": {"type": "string", "maxLength": 80}},
        "additionalProperties": False,
    }


def _query() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"query": {"type": "string", "minLength": 2, "maxLength": 80}},
        "required": ["query"],
        "additionalProperties": False,
    }


def _feedback() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["answer", "decline"]},
            "evidence_span": {"type": "string", "maxLength": 300},
        },
        "required": ["action", "evidence_span"],
        "additionalProperties": False,
    }


def _memory_candidate() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["impression", "preference", "boundary", "future_intent"],
            },
            "evidence_span": {"type": "string", "maxLength": 300},
        },
        "required": ["category", "evidence_span"],
        "additionalProperties": False,
    }


PRIVATE_BINDINGS: tuple[PrivateToolBinding, ...] = (
    PrivateToolBinding(
        "private.relationship.get_pair_summary",
        "讀取目前已接受關係中可公開、共同或已同意分享的摘要。",
        _empty(),
        "我翻翻你們確認過的共同資訊～",
    ),
    PrivateToolBinding(
        "private.relationship.get_shared_history",
        "讀取目前這一對共同聊天室的近期對話脈絡。",
        _empty(),
        "翻翻你們最近聊過的，把話接起來。",
    ),
    PrivateToolBinding(
        "private.relationship.search_shared_history",
        "依使用者提供的主題搜尋目前這一對的共同聊天室歷史。",
        _query(),
        "我潛進聊天紀錄裡撈撈看～",
    ),
    PrivateToolBinding(
        "private.calendar.get_counterparty_availability",
        "只查看目前對象在指定期間的 busy/free；不可讀取行程內容。",
        _scope(),
        "我幫你看看對方那段時間方不方便～",
    ),
    PrivateToolBinding(
        "private.calendar.get_viewer_availability",
        "只在目前關係或約會規劃問題中查看本人 busy/free。",
        _scope(),
        "翻翻你的行事曆，看看那段時間怎麼排。",
    ),
    PrivateToolBinding(
        "private.date.get_coordination_state",
        "讀取目前這一對正式約會協調的安全狀態。",
        _empty(),
        "我確認一下目前的約會安排～",
    ),
    PrivateToolBinding(
        "private.date.start_coordination",
        "為目前聊天室的對象準備約會協調確認卡；不帶名字或 ID，也不直接通知對方。",
        _empty(),
        "我先把約會協調準備好，等你點頭～",
        risk="write",
    ),
    PrivateToolBinding(
        "private.relationship.record_post_date_feedback",
        "在有效的約會後感想問題中記錄本人回答或拒答；不會自動分享給對方。",
        _feedback(),
        "我把這次約會後的感受收好，只留在你這邊～",
        risk="write",
    ),
    PrivateToolBinding(
        "private.relationship.capture_memory_candidate",
        "提出目前 owner 對這段關係的明確主觀看法候選；Server 仍會複核。",
        _memory_candidate(),
        "我整理一下你剛才說的感受～",
        risk="write",
    ),
    PrivateToolBinding(
        "private.relationship.respond_to_probe",
        "回覆目前 context 中已存在的關係 probe；不建立新的 probe。",
        _feedback(),
        "我把這題的回答接起來～",
        risk="write",
    ),
    PrivateToolBinding(
        "private.interaction.cancel_pending",
        "只取消目前由使用者明確指定的待確認操作。",
        _empty(),
        "我先把目前這張確認卡收起來～",
        risk="write",
    ),
    PrivateToolBinding(
        "private.surface.redirect_to_public",
        "將不屬於目前關係的原問題預填到阿月主聊天室，不自動送出。",
        _empty(),
        "這題我帶你回主聊天室處理～",
        risk="read",
    ),
)

PRIVATE_TOOL_NAMES = frozenset(item.name for item in PRIVATE_BINDINGS)
PRIVATE_TOOL_MAP = {item.name: item for item in PRIVATE_BINDINGS}


def tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": item.name,
            "description": item.description,
            "parameters": item.parameters,
        }
        for item in PRIVATE_BINDINGS
    ]
