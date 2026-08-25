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

        # 規格 §5 吉祥物梯度 fallback：線上 KB 若還是舊資料（缺 mascot 旗標），
        # 依等級補上；寄件方 blocked 刻意無圖。
        if "mascot" not in sender_d:
            sender_default = {
                "warning": "warning_sign",
                "restricted": "danger",
                "blocked": None,
            }.get(risk_level)
            if sender_default is not None:
                sender_d["mascot"] = sender_default
        if "mascot" not in receiver_d and receiver_d.get("action") != "none":
            receiver_d["mascot"] = "heart"

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

        # 5. 已處置豁免判定：四條件全部成立時，標記本則不為同一件事再罰第二次。
        #    累積狀態全程不動（風險等級、directive action 都不變），只在 directive 上
        #    附加 sanction_exempted=True，讓 risk_detection 的攔截判定改為不鎖訊息，
        #    並讓前端可隱藏 mascot（intervention_widgets.dart:360 已就緒）。
        exempted = False
        if chat_log_service is not None and risk_level in LEVEL_ORDER:
            exempted = await self._check_sanction_exempted(
                risk_level, diagnosis, conv_id, sender_id, chat_log_service)
        if exempted:
            sender_d["sanction_exempted"] = True
            receiver_d["sanction_exempted"] = True
            # 豁免時改用陳述目前狀態的文案（*_state_notice），而非回應「這則訊息」
            # 的文案。原始文案如「頻繁的訊息會讓對方不舒服」在豁免時是假的——
            # 本則沒問題，只是還在觀察期。state_notice 的 risk_level='exempt'，
            # 不會被 get_interventions_by_level(真實等級) 誤抓。
            sender_d = self._apply_state_notice(sender_d, "sender")
            receiver_d = self._apply_state_notice(receiver_d, "receiver")

        intervention_id = f"int_{uuid.uuid4().hex[:8]}"
        command = {
            "intervention_id": intervention_id,
            "conversation_id": conv_id,
            "triggered_by_msg_id": msg_id,
            "risk_level": risk_level,
            "sender_directive": sender_d,
            "receiver_directive": receiver_d,
            "admin_directive": admin_d,
            "sanction_exempted": exempted,
        }

        return command

    async def _check_sanction_exempted(self, risk_level: str, diagnosis: dict,
                                       conv_id: str, sender_id: str, chat_log_service) -> bool:
        """已處置豁免：四條件「全部」成立才豁免，否則照罰。

        設計目的：避免同一件事在冷卻期內被重複處罰。累積風險狀態與名聲留著，
        只是「不為同一件事處罰第二次」——所以條件必須能區分「還在為同一件事」
        與「服完刑又違規／風險升高」。

        ① 上一次介入存在
            首次累積到該等級一律照罰，累積機制不受影響。
        ② 上次等級 ≥ 本次等級
            風險升高即失效，不會變成保護傘。
        ③ 剩餘冷卻 = 0
            確認真的服完刑；冷卻中代表仍在服刑，不豁免。
        ④ 本則 max(delta) < 0.05
            服完又違規則照罰，且從高狀態起跳罰得更重。delta_max 由呼叫端
            （risk_detection.py）以 diagnosis["delta_max"] 傳入；未提供時
            視為 0（保守，不擋豁免）。

        任一條件不成立即回 False。
        """
        try:
            # ① 上一次介入存在
            last = await chat_log_service.get_last_displayed_intervention(conv_id, sender_id, "sender")
            if not last or not last.get("risk_level"):
                return False

            # ② 上次等級 ≥ 本次等級
            last_level = LEVEL_ORDER.get(last["risk_level"], 0)
            curr_level = LEVEL_ORDER.get(risk_level, 0)
            if last_level < curr_level:
                return False

            # ③ 剩餘冷卻 = 0
            remaining = await chat_log_service.get_remaining_cooldown(conv_id, sender_id)
            if remaining > 0:
                return False

            # ④ 本則 max(delta) < 0.05
            delta_max = float(diagnosis.get("delta_max", 0.0) or 0.0)
            if delta_max >= 0.05:
                return False

            return True
        except Exception as e:
            print(f"   [ Exempt Warning ] 豁免判定失敗，保守不豁免: {e}")
            return False

    @staticmethod
    def _apply_state_notice(directive: dict, role: str) -> dict:
        """豁免時以陳述狀態的文案（*_state_notice）取代回應本則訊息的文案。

        state_notice 模板存於 kb_interventions 的 risk_level='exempt'，以 template_id
        結尾 '_state_notice' 區分。此處只換 content（文案），其餘 directive 旗標
        （action / cooldown / mascot / show_options 等）維持原樣——豁免只改「說什麼」，
        不改「做什麼」。找不到模板時保守保留原文案。
        """
        try:
            templates = KBService.get_interventions_by_level("exempt")
            target_id = f"{role}_state_notice"
            target = next((t for t in templates if t.get("template_id") == target_id), None)
            if not target:
                return directive
            msg_tpl = target.get("message_template", {})
            if isinstance(msg_tpl, str):
                import json as _json
                msg_tpl = _json.loads(msg_tpl)
            new_content = {
                "title": msg_tpl.get("title"),
                "body": msg_tpl.get("body"),
                "primary_risk_type": directive.get("content", {}).get("primary_risk_type"),
            }
            updated = dict(directive)
            updated["content"] = new_content
            return updated
        except Exception as e:
            print(f"   [ State Notice ] 取用失敗，保留原文案: {e}")
            return directive

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
        # 規格 §6：整包 passthrough。新增旗標是純資料變更，
        # 不再白名單（2026-08-15 前白名單曾使 mascot / show_options 從未送達前端）。
        for key, value in ui.items():
            if key in ("cooldown", "require_ack"):
                continue
            directive[key] = value
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
