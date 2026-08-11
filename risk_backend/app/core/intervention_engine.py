"""
ㄎSafety Intervention Engine - 指令式介入決策中心 (修正模板匹配邏輯)
"""

import json
import uuid
from datetime import datetime, timezone
from app.services.kb_service import KBService

# 介入顯示節流的預設窗（秒）。可由 kb_interventions.ui_behavior.display_throttle_seconds 覆寫。
# 目的：避免同一段對話內連續觸發時重複跳出提示，造成警示疲勞與使用者抗拒。
DEFAULT_THROTTLE_SECONDS = {
    "observation": 1800,   # 30 分鐘；低調提示，不需頻繁出現
    "warning": 300,        # 5 分鐘；柔性提醒，門檻低故需節流
    "restricted": 0,       # 不節流；已具傷害性，每次都應提醒
    "blocked": 0,          # 不節流；硬性攔截必須告知
}

LEVEL_ORDER = {"safe": 0, "observation": 1, "warning": 2, "restricted": 3, "blocked": 4}


class InterventionEngine:
    def __init__(self):
        self.INTERVENTION_LABELS = {
            "show_ambient_icon": "安全提示",
            "show_reflection_banner": "柔性提醒",
            "show_modal_warning": "正式警告",
            "block_message": "訊息攔截"
        }

    async def execute(self, risk_level: str, risk_state: dict, diagnosis: dict,
                      conv_id: str, sender_id: str, receiver_id: str,
                      msg_id: str, decision_reason: str, chat_log_service=None) -> dict:
        """產生介入指令。

        chat_log_service: 提供則啟用顯示節流（查詢上次實際顯示的介入）。
                          未提供時不節流，維持既有行為。
        """
        if risk_level == "safe":
            return self._build_empty_command(conv_id, msg_id)

        primary_risk = max(risk_state, key=risk_state.get)

        # 取得所有該等級的模板
        all_templates = KBService.get_interventions_by_level(risk_level)

        # 2. 取得發送方指令 (確保 template_id 包含 'sender')
        sender_d = self._get_specific_directive(all_templates, primary_risk, "sender")

        # 3. 取得接收方指令 (確保 template_id 包含 'receiver')
        receiver_d = self._get_specific_directive(all_templates, "any", "receiver")

        # 3.5 顯示節流：寄件方與收件方分別判定
        if chat_log_service is not None:
            sender_d = await self._apply_throttle(
                sender_d, risk_level, conv_id, sender_id, "sender", chat_log_service)
            receiver_d = await self._apply_throttle(
                receiver_d, risk_level, conv_id, sender_id, "receiver", chat_log_service)

        # 4. 管理員通報
        admin_d = None
        if risk_level == "blocked":
            admin_d = {
                "type": "human_review_queue",
                "priority": "high" if decision_reason == "critical_override" else "normal",
                "requires_review_within_hours": 24
            }

        intervention_id = f"int_{uuid.uuid4().hex[:8]}"
        command = {
            "intervention_id": intervention_id,
            "conversation_id": conv_id,
            "triggered_by_msg_id": msg_id,
            "risk_level": risk_level,
            "sender_directive": sender_d,
            "receiver_directive": receiver_d,
            "admin_directive": admin_d
        }

        return command

    async def _apply_throttle(self, directive: dict, risk_level: str, conv_id: str,
                              user_id: str, role: str, chat_log_service) -> dict:
        """依節流窗決定是否抑制本次顯示。

        規則：
        1. 本來就沒有動作（action == "none"）→ 不處理。
        2. 節流窗為 0（restricted / blocked）→ 一律顯示。
        3. **等級較上次顯示時更高 → 一律顯示**（風險正在升高，不可因節流而沉默）。
        4. 距上次顯示未超過節流窗，且等級未升高 → 抑制，action 改為 "suppressed"。

        被抑制者仍會寫入 intervention_logs（action="suppressed"），保留完整稽核軌跡，
        且不會被視為「上次顯示」而推遲下一次的節流窗。
        """
        if not directive or directive.get("action") in (None, "none"):
            return directive

        window = directive.get("display_throttle_seconds")
        if window is None:
            window = DEFAULT_THROTTLE_SECONDS.get(risk_level, 0)
        if not window:
            return directive

        last = await chat_log_service.get_last_displayed_intervention(conv_id, user_id, role)
        if not last or not last.get("timestamp"):
            return directive

        # 等級升高一律顯示
        if LEVEL_ORDER.get(risk_level, 0) > LEVEL_ORDER.get(last.get("risk_level"), 0):
            return directive

        try:
            last_ts = datetime.fromisoformat(str(last["timestamp"]).replace("Z", "+00:00"))
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - last_ts).total_seconds()
        except (ValueError, TypeError) as e:
            print(f"   [ Throttle Warning ] 無法解析上次介入時間: {e}")
            return directive

        if elapsed < window:
            suppressed = dict(directive)
            suppressed["action"] = "suppressed"
            suppressed["content"] = None
            suppressed["cooldown_seconds"] = 0
            suppressed["require_acknowledgment"] = False
            suppressed["throttled"] = {
                "reason": "display_throttle",
                "window_seconds": window,
                "elapsed_seconds": int(elapsed),
                "last_shown_level": last.get("risk_level"),
            }
            print(f"   [ Throttle ] {role} 顯示已抑制（{int(elapsed)}s < {window}s，等級未升高）")
            return suppressed

        return directive

    def _get_specific_directive(self, templates: list, risk_type: str, role: str) -> dict:
        """更精確的過濾邏輯"""
        # 1. 優先找對應維度 + 對應角色
        target = next((t for t in templates if t['primary_risk_type'] == risk_type and role in t['template_id']), None)
        
        # 2. 退而求其次找 any + 對應角色
        if not target:
            target = next((t for t in templates if t['primary_risk_type'] == 'any' and role in t['template_id']), None)
        
        if not target:
            return {"action": "none", "content": None}

        ui = target['ui_behavior']
        directive = {
            "action": target['action_type'],
            "cooldown_seconds": ui.get('cooldown', 0),
            "require_acknowledgment": ui.get('require_ack', False),
            "content": {
                "title": target['message_template'].get('title'),
                "body": target['message_template'].get('body'),
                "primary_risk_type": risk_type
            }
        }
        # 節流窗可由模板覆寫；未設定則於 _apply_throttle 使用該等級的預設值
        if 'display_throttle_seconds' in ui:
            directive['display_throttle_seconds'] = ui['display_throttle_seconds']
        return directive

    def _build_empty_command(self, conv_id, msg_id):
        return {
            "intervention_id": None,
            "conversation_id": conv_id,
            "triggered_by_msg_id": msg_id,
            "risk_level": "safe",
            "sender_directive": {"action": "none", "content": None},
            "receiver_directive": {"action": "none", "content": None}
        }
