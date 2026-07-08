"""依風險等級決定 human_chat 投遞內容與回應的純函式（無副作用，方便測試）。"""
from dataclasses import dataclass
from typing import Optional

WARNING_MSG = "⚠️ 偵測到敏感內容，訊息已遭系統安全攔截。"

@dataclass
class DeliveryPlan:
    deliver_original: bool
    triggered_by_msg_id: Optional[str]
    response: dict

def _ui_priority(level: str) -> str:
    return "risk" if level in ("warning", "restricted", "blocked") else "coach"

def plan_delivery(risk_level: str, risk_assessment: dict, message_doc: dict) -> DeliveryPlan:
    cmd = (risk_assessment or {}).get("intervention_command") or {}
    tbm = cmd.get("triggered_by_msg_id")

    if risk_level == "blocked":
        return DeliveryPlan(
            deliver_original=False,
            triggered_by_msg_id=None,
            response={
                "is_blocked": True,
                "warning_msg": WARNING_MSG,
                "risk_assessment": risk_assessment,
                "ui_priority": "risk",
            },
        )

    if risk_level == "restricted":
        return DeliveryPlan(
            deliver_original=True,
            triggered_by_msg_id=tbm,
            response={
                "is_blocked": False,
                "message": message_doc,
                "risk_assessment": risk_assessment,
                "ui_priority": "risk",
            },
        )

    if risk_level in ("observation", "warning"):
        return DeliveryPlan(
            deliver_original=True,
            triggered_by_msg_id=tbm,
            response={
                "is_blocked": False,
                "message": message_doc,
                "risk_assessment": risk_assessment,
                "ui_priority": _ui_priority(risk_level),
            },
        )

    # safe（含未知等級 fallback）
    return DeliveryPlan(
        deliver_original=True,
        triggered_by_msg_id=None,
        response={
            "is_blocked": False,
            "message": message_doc,
            "risk_assessment": risk_assessment,
            "ui_priority": "coach",
        },
    )