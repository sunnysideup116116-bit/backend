"""
ㄎSafety Intervention Engine - 指令式介入決策中心 (修正模板匹配邏輯)
"""

import json
import uuid
from app.services.kb_service import KBService

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
                      msg_id: str, decision_reason: str) -> dict:
        if risk_level == "safe":
            return self._build_empty_command(conv_id, msg_id)

        primary_risk = max(risk_state, key=risk_state.get)
        
        # 取得所有該等級的模板
        all_templates = KBService.get_interventions_by_level(risk_level)
        
        # 2. 取得發送方指令 (確保 template_id 包含 'sender')
        sender_d = self._get_specific_directive(all_templates, primary_risk, "sender")
        
        # 3. 取得接收方指令 (確保 template_id 包含 'receiver')
        receiver_d = self._get_specific_directive(all_templates, "any", "receiver")
        
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

    def _get_specific_directive(self, templates: list, risk_type: str, role: str) -> dict:
        """更精確的過濾邏輯"""
        # 1. 優先找對應維度 + 對應角色
        target = next((t for t in templates if t['primary_risk_type'] == risk_type and role in t['template_id']), None)
        
        # 2. 退而求其次找 any + 對應角色
        if not target:
            target = next((t for t in templates if t['primary_risk_type'] == 'any' and role in t['template_id']), None)
        
        if not target:
            return {"action": "none", "content": None}

        return {
            "action": target['action_type'],
            "cooldown_seconds": target['ui_behavior'].get('cooldown', 0),
            "require_acknowledgment": target['ui_behavior'].get('require_ack', False),
            "content": {
                "title": target['message_template'].get('title'),
                "body": target['message_template'].get('body'),
                "primary_risk_type": risk_type
            }
        }

    def _build_empty_command(self, conv_id, msg_id):
        return {
            "intervention_id": None,
            "conversation_id": conv_id,
            "triggered_by_msg_id": msg_id,
            "risk_level": "safe",
            "sender_directive": {"action": "none", "content": None},
            "receiver_directive": {"action": "none", "content": None}
        }
