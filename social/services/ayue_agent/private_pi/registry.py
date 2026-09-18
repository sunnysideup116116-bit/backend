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
    item = {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["impression", "preference", "boundary", "future_intent"],
                "description": "省略時視為 impression；偏好、界線、未來期待才改用其他值。",
            },
            "evidence_span": {"type": "string", "maxLength": 300},
            "statement": {
                "type": "string",
                "minLength": 2,
                "maxLength": 120,
                "description": "用第一人稱整理出的單一關係觀點；保留程度、時間與情境，不加引號。",
            },
        },
        "required": ["evidence_span", "statement"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": item,
            },
        },
        "required": ["candidates"],
        "additionalProperties": False,
    }


def _relationship_memory_entry() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 40},
            "summary": {"type": "string", "minLength": 1, "maxLength": 160},
            "label": {"type": "string", "minLength": 1, "maxLength": 32},
            "placement": {
                "type": "string",
                "maxLength": 32,
                "description": "入口位置；使用 before_reply 或 after_reply。",
            },
        },
        "required": ["title", "summary", "label", "placement"],
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
        "只有本回合訊息新表達、加深、修正或撤回 owner 對目前對象的主觀感受、印象、偏好、期待或界線時才呼叫。一次把本回合所有獨立觀點放進 candidates；不衝突的面向要分項保留。evidence_span 保留最短必要原文；statement 用第一人稱整理成可長期閱讀的單一觀點，保留程度、時間與情境，不得增加原文沒有的因果、喜歡程度或關係結論。Private history 只能協助辨認指涉與變化。查看或管理既有記憶的要求不得呼叫此工具。",
        _memory_candidate(),
        "我把這份感受排進記憶整理，確認後再收好～",
        risk="write",
    ),
    PrivateToolBinding(
        "private.surface.present_relationship_memories",
        "使用者明確要求查看、開啟、修改、撤銷或管理目前對象的既有記憶時必須呼叫。在本回合放置一個可開啟『阿月記住的事』的入口；你可自由撰寫標題、摘要、按鈕文字並決定放在回答前或回答後。入口只代表可查看與管理，不代表本回合已保存成功。",
        _relationship_memory_entry(),
        "我把這段關係的記憶入口整理到回覆裡～",
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
