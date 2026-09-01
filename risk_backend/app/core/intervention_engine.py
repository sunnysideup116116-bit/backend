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
EXEMPT_DELTA_EPSILON = 0.10
_NO_OPTIONS_ON_EXEMPT = {"warning"}
_ALLOWED_ACTION_OPTIONS = {
    "dismiss",
    "leave_conversation",
    "block_user",
    "report_user",
}


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
                      msg_id: str, decision_reason: str, chat_log_service=None,
                      message_delta: dict | None = None) -> dict:
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

        # 3.5 已處置豁免判定。只有呼叫端提供本則 delta（或舊版 diagnosis
        # 明確帶 delta_max）時才啟用，避免缺資料被誤判為零風險。
        exempted = False
        if chat_log_service is not None and risk_level in LEVEL_ORDER:
            exempted = await self._check_sanction_exempted(
                risk_level,
                diagnosis,
                conv_id,
                sender_id,
                chat_log_service,
                message_delta=message_delta,
            )
        if exempted:
            sender_d = self._apply_state_notice(sender_d, "sender", risk_level)
            receiver_d = self._apply_state_notice(receiver_d, "receiver", risk_level)

        # 4. 顯示節流：狀態式通知也使用自己的 300 秒節流設定。
        if chat_log_service is not None:
            sender_d = await self._apply_throttle(
                sender_d, risk_level, conv_id, sender_id, "sender", chat_log_service)
            receiver_d = await self._apply_throttle(
                receiver_d, risk_level, conv_id, sender_id, "receiver", chat_log_service)

        # 5. 管理員通報。本則若已處置豁免，沒有新的違規事實，不重複排入審核。
        admin_d = None
        if risk_level == "blocked" and not exempted:
            admin_d = {
                "type": "human_review_queue",
                "priority": "high" if decision_reason == "critical_override" else "normal",
                "requires_review_within_hours": 24
            }

        if sender_d.get("action") not in (None, "none", "suppressed"):
            sender_d["target_user_id"] = sender_id
        if receiver_d.get("action") not in (None, "none", "suppressed"):
            receiver_d["target_user_id"] = receiver_id

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
                                       conv_id: str, sender_id: str, chat_log_service,
                                       message_delta: dict | None = None) -> bool:
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
        ④ 本則 max(delta) < 0.10
            服完又違規則照罰，且從高狀態起跳罰得更重。delta_max 由呼叫端
            （risk_detection.py）以 message_delta 傳入；舊呼叫端也可明確帶
            diagnosis["delta_max"]。兩者都未提供時保守不豁免。

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

            # ④ 本則 max(delta) < epsilon。沒有 delta 資料時不啟用豁免。
            if message_delta is not None:
                values = [float(value or 0.0) for value in message_delta.values()]
                delta_max = max(values, default=0.0)
            elif "delta_max" in diagnosis:
                delta_max = float(diagnosis.get("delta_max") or 0.0)
            else:
                return False
            if delta_max >= EXEMPT_DELTA_EPSILON:
                return False

            return True
        except Exception as e:
            print(f"   [ Exempt Warning ] 豁免判定失敗，保守不豁免: {e}")
            return False

    def _apply_state_notice(self, directive: dict, role: str, risk_level: str) -> dict:
        """豁免時以陳述狀態的文案（*_state_notice）取代回應本則訊息的文案。

        state_notice 模板存於 kb_interventions 的 risk_level='exempt'。豁免解除
        原本的 modal／block sanction，因此 action 與 UI 行為也必須改用狀態模板。
        """
        try:
            templates = KBService.get_interventions_by_level("exempt")
            target_id = f"{role}_state_notice"
            target = next((t for t in templates if t.get("template_id") == target_id), None)
            if not target:
                fallback = dict(directive)
                old_content = directive.get("content") or {}
                fallback["cooldown_seconds"] = 0
                fallback["require_acknowledgment"] = False
                fallback["sanction_exempted"] = True
                fallback["show_feedback_buttons"] = False
                fallback["display_throttle_seconds"] = 300
                if role == "sender":
                    fallback["action"] = "show_reflection_banner"
                    fallback["allow_report_text"] = False
                    fallback.pop("action_options", None)
                    fallback["show_options"] = False
                    body = "先前的處置已完成，目前對話仍在安全觀察中。"
                else:
                    fallback["action"] = "show_safety_info_card"
                    body = "這段對話仍在安全觀察中，你可以隨時封鎖或檢舉對方。"
                    if risk_level in _NO_OPTIONS_ON_EXEMPT:
                        fallback.pop("action_options", None)
                        fallback["show_options"] = False
                    else:
                        fallback["show_options"] = bool(fallback.get("action_options"))
                fallback["content"] = {
                    "title": None,
                    "body": body,
                    "primary_risk_type": old_content.get("primary_risk_type", "any"),
                }
                return fallback

            out = self._get_specific_directive([target], "any", role)
            out["sanction_exempted"] = True
            out["cooldown_seconds"] = 0
            out["require_acknowledgment"] = False
            out["show_feedback_buttons"] = False
            if risk_level in _NO_OPTIONS_ON_EXEMPT:
                out.pop("action_options", None)
                out["show_options"] = False
            return out
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
        options = self._parse_action_options(target.get("action_options"))
        if options:
            directive["action_options"] = options
        return directive

    @staticmethod
    def _parse_action_options(raw) -> list:
        """Normalize the optional KB action list into a bounded UI contract."""
        if not raw:
            return []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                return []
        if not isinstance(raw, list):
            return []
        result = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "").strip()
            label = str(item.get("label") or "").strip()
            if action not in _ALLOWED_ACTION_OPTIONS or not label:
                continue
            result.append({"action": action, "label": label[:40]})
            if len(result) == 4:
                break
        return result

    def _build_empty_command(self, conv_id, msg_id):
        return {
            "intervention_id": None,
            "conversation_id": conv_id,
            "triggered_by_msg_id": msg_id,
            "risk_level": "safe",
            "sender_directive": {"action": "none", "content": None},
            "receiver_directive": {"action": "none", "content": None}
        }
