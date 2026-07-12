"""
風險狀態機 - 智慧決策版 (完全資料驅動 + Pydantic V2 規範化)
"""

import math
from datetime import datetime
from typing import Dict, Optional, Tuple, List
from app.models.schemas import RiskState
from app.services.kb_service import KBService
from app.services.chat_log_service import ChatLogService

class RiskStateMachine:
    def __init__(self):
        self.chat_log_service = ChatLogService()
        self.last_diagnostic = {}

    async def get_user_state(self, conversation_id: str, user_id: str) -> Tuple[RiskState, Optional[str]]:
        return await self.chat_log_service.get_latest_risk_state_with_time(conversation_id, user_id)

    async def update(self, conversation_id: str, user_id: str, msg_id: str, delta: RiskState) -> Tuple[RiskState, str]:
        prior, last_ts_str = await self.get_user_state(conversation_id, user_id)
        #！！！！改Config改這邊！！！！
        config = KBService.get_fusion_config("threshold_v2_rule_heavy")
        
        msg_decay = 0.9
        if config:
            msg_decay = config.get('decay_factor', 0.9)
        
        time_decay_factor = 1.0
        if last_ts_str:
            try:
                last_ts = datetime.fromisoformat(last_ts_str.replace('Z', '+00:00'))
                delta_t_h = (datetime.now(last_ts.tzinfo) - last_ts).total_seconds() / 3600.0
                time_decay_factor = self._calculate_time_decay(delta_t_h, config)
            except Exception as e:
                print(f"   [ Warning ] Time decay calculation failed: {e}")
        
        total_decay = msg_decay * time_decay_factor

        feedback_signal = await self._get_feedback_signal(conversation_id, user_id)
        if feedback_signal == 'trust':
            total_decay *= 0.7
        if feedback_signal == 'alert':
            delta_dict = delta.model_dump()
            if any(v > 0 for v in delta_dict.values()):
                primary = max(delta_dict, key=delta_dict.get)
                delta_dict[primary] = min(1.0, delta_dict[primary] + 0.1)
                delta = RiskState(**delta_dict)

        new_values = {}
        fields = ['sexual_boundary', 'coercion', 'manipulation', 'harassment', 'emotional_pressure']
        for field in fields:
            new_val = (getattr(prior, field) * total_decay) + getattr(delta, field)
            new_values[field] = min(1.0, max(0.0, new_val))
        
        new_state = RiskState(**new_values)

        logic_cfg = {}
        if config and 'weights' in config:
            logic_cfg = config['weights'].get('decision_logic', {})
            
        w_max = logic_cfg.get('W_MAX', 0.60)
        w_spread = logic_cfg.get('W_SPREAD', 0.30)
        w_trend = logic_cfg.get('W_TREND', 0.10)
        noise_floor = logic_cfg.get('NOISE_FLOOR', 0.05)
        decrease_damping = logic_cfg.get('DECREASE_DAMPING', 0.03)

        max_s = max(new_values.values())
        active_dims = [v for v in new_values.values() if v > noise_floor]
        spread_s = len(active_dims) / len(fields)

        history = await self.chat_log_service.get_recent_risk_state_history(conversation_id, user_id, limit=5)
        trend_s = self._calculate_trend(new_state, history)

        level, composite_s, reason = self._decide_5_level(max_s, spread_s, trend_s, config, w_max, w_spread, w_trend, decrease_damping)

        decay_applied = (time_decay_factor < 1.0)

        self.last_diagnostic = {
            "max_score": max_s,
            "spread_score": spread_s,
            "trend_score": trend_s,
            "composite_score": composite_s,
            "reason": reason,
            "decay_applied": decay_applied,
            "feedback_signal": feedback_signal
        }

        await self.chat_log_service.save_risk_state_history(conversation_id, user_id, msg_id, new_state, level, delta, decay_applied=decay_applied)
        return new_state, level

    async def _get_feedback_signal(self, conversation_id: str, sender_id: str) -> str:
        """Aggregate receiver feedback and background judgments into a recalibration signal."""
        try:
            feedbacks = await self.chat_log_service.get_recent_feedbacks(
                conversation_id, sender_id, limit=5
            )
            judgments = await self.chat_log_service.get_recent_guardrail_context_reviews(
                conversation_id, sender_id, limit=5
            )

            uncomfortable_count = sum(1 for f in feedbacks if f == 'uncomfortable')
            comfortable_count = sum(1 for f in feedbacks if f == 'comfortable')
            concerning_count = sum(1 for j in judgments if j == 'concerning')
            healthy_count = sum(1 for j in judgments if j == 'healthy')

            if uncomfortable_count >= 1 or concerning_count >= 1:
                return 'alert'
            if comfortable_count >= 2 or healthy_count >= 2:
                return 'trust'
            return 'neutral'
        except Exception as e:
            print(f"feedback signal error: {e}")
            return 'neutral'

    def _calculate_trend(self, current: RiskState, history: List[RiskState]) -> float:
        if not history: return 0.0
        # 修正: 使用 model_dump() 取代 dict()
        prev_max = max(history[0].model_dump().values())
        curr_max = max(current.model_dump().values())
        return max(-0.3, min(0.3, (curr_max - prev_max)))

    def _decide_5_level(self, max_s, spread_s, trend_s, config, w_max, w_spread, w_trend, damp) -> Tuple[str, float, str]:
        t_safe, t_obs, t_warn, t_rest, t_crit = 0.10, 0.25, 0.45, 0.65, 0.85
        if config and 'thresholds' in config:
            t = config['thresholds']
            t_safe = t.get('T_SAFE', 0.10)
            t_obs = t.get('T_OBSERVATION', 0.25)
            t_warn = t.get('T_WARNING', 0.45)
            t_rest = t.get('T_RESTRICTED', 0.65)
            t_crit = t.get('T_CRITICAL', 0.85)

        if max_s >= t_crit: return 'blocked', max_s, "critical_override"

        trend_adj = (w_trend * trend_s) if trend_s > 0 else (damp * trend_s)
        composite = (max_s * w_max) + (spread_s * w_spread) + trend_adj
        composite = min(1.0, max(0.0, composite))

        if composite < t_safe: level = 'safe'
        elif composite < t_obs: level = 'observation'
        elif composite < t_warn: level = 'warning'
        elif composite < t_rest: level = 'restricted'
        else: level = 'blocked'
        return level, composite, "normal"

    def _calculate_time_decay(self, delta_t_h: float, config: dict) -> float:
        lam = 0.028
        if config and 'weights' in config and 'time_decay' in config['weights']:
            tiers = config['weights']['time_decay'].get('tiers', [])
            for tier in tiers:
                if tier['min_h'] <= delta_t_h < tier['max_h']:
                    lam = tier['lambda']
                    break
        return math.exp(-lam * delta_t_h)

    def decide_level(self, state: RiskState) -> str:
        return "SEE_UPDATE_RESULT"
