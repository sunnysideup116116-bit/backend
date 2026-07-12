"""
二階情境風險檢測層 (Scenario Risk Detection Layer) - 動態動態匹配版
"""

import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Tuple, List
from app.models.schemas import RiskState, TemporalFeatures
from app.services.kb_service import KBService

class ScenarioRiskLayer:
    def __init__(self):
        # 定義 DB Key 與物件屬性的對應關係，確保未來新增維度時只需改這裡或完全自動化
        self.nlp_map = {
            'sexual': 'sexual_boundary',
            'coercion': 'coercion',
            'manipulation': 'manipulation',
            'harassment': 'harassment',
            'emotional': 'emotional_pressure'
        }
        
        self.rule_map = {
            'freq': 'frequency',
            'latency': 'latency',
            'burst': 'message_burst_count',
            'ratio': 'volume_ratio',
            'volume_ratio': 'volume_ratio',
            'message_ratio': 'message_ratio',
            'unreplied': 'unreplied_count',
            'consecutive_char': 'consecutive_char_count',
            'avg_chars': 'avg_chars_per_message'
        }

    def evaluate(
        self, 
        rule_result: Dict, 
        nlp_result: Dict, 
        temporal_features: TemporalFeatures,
        memory_metrics: Dict = None,
        last_summary: Dict = None
    ) -> Tuple[RiskState, List[str]]:
        """
        根據第一層引擎的輸出與環境/記憶資訊，判定是否觸發二階複合情境規則
        """
        bonus_delta_values = {
            'sexual_boundary': 0.0,
            'coercion': 0.0,
            'manipulation': 0.0,
            'harassment': 0.0,
            'emotional_pressure': 0.0
        }
        triggered_scenarios = []

        # 1. 從 MySQL 獲取二階規則
        scenarios = KBService.get_scenario_rules()
        
        # 取得當前環境資訊 (明確使用 Asia/Taipei 時區)
        now_taipei = datetime.datetime.now(ZoneInfo("Asia/Taipei"))
        current_hour = now_taipei.hour
        
        # 定義深夜時段: 00:00 <= hour < 06:00 (台北時間)
        is_midnight = 0 <= current_hour <= 5
        
        nlp_delta = nlp_result.get('delta')

        for sc in scenarios:
            logic = sc['condition_logic']
            match = True
            
            # 2. 動態條件匹配
            for key, threshold in logic.items():
                # --- [A] 原始分析引擎條件 ---
                if key.startswith('nlp_') and key.endswith('_min'):
                    short_key = key.replace('nlp_', '').replace('_min', '')
                    attr_name = self.nlp_map.get(short_key)
                    if not attr_name:
                        print(f"   [ Scenario Warning ] Unknown NLP condition: {key}")
                        match = False
                        break
                    if getattr(nlp_delta, attr_name) < threshold:
                        match = False
                        break
                elif key.startswith('rule_') and key.endswith('_min'):
                    short_key = key.replace('rule_', '').replace('_min', '')
                    attr_name = self.rule_map.get(short_key)
                    if not attr_name:
                        print(f"   [ Scenario Warning ] Unknown rule condition: {key}")
                        match = False
                        break
                    if getattr(temporal_features, attr_name) < threshold:
                        match = False
                        break
                elif key == 'is_midnight':
                    if threshold != is_midnight:
                        match = False
                        break

                # --- [B] 新增：記憶與指標條件 (L1 & L2) ---
                elif key == 'familiarity_max':
                    val = memory_metrics.get('familiarity_score', 0.0) if memory_metrics else 0.0
                    if val > threshold: match = False; break
                elif key == 'familiarity_min':
                    val = memory_metrics.get('familiarity_score', 0.0) if memory_metrics else 0.0
                    if val < threshold: match = False; break
                elif key == 'intimacy_min':
                    val = last_summary.get('intimacy_level', 0.0) if last_summary else 0.0
                    if val < threshold: match = False; break
                elif key == 'intimacy_max':
                    val = last_summary.get('intimacy_level', 0.0) if last_summary else 0.0
                    if val > threshold: match = False; break
                elif key == 'progression_rate_min':
                    val = memory_metrics.get('intimacy_progression_rate', 0.0) if memory_metrics else 0.0
                    if val < threshold: match = False; break
                elif key == 'balance_max':
                    val = memory_metrics.get('conversation_balance', 0.5) if memory_metrics else 0.5
                    if val > threshold: match = False; break
                elif key == 'balance_range':
                    val = memory_metrics.get('conversation_balance', 0.5) if memory_metrics else 0.5
                    if not (threshold[0] <= val <= threshold[1]): match = False; break
                elif key == 'imbalance_min':
                    val = memory_metrics.get('conversation_balance', 0.5) if memory_metrics else 0.5
                    imbalance = abs(val - 0.5) * 2
                    if imbalance < threshold: match = False; break
                elif key == 'total_messages_min':
                    val = memory_metrics.get('total_messages', 0) if memory_metrics else 0
                    if val < threshold: match = False; break
            
            # 3. 如果全部符合，套用 Bonus
            if match:
                bonus_actions = sc.get('bonus_actions', {})
                for category, value in bonus_actions.items():
                    if category in bonus_delta_values:
                        bonus_delta_values[category] += value
                triggered_scenarios.append(sc['rule_name'])
                print(f"   [ Scenario Layer Triggered ] 觸發規則: {sc['rule_name']}")

        return RiskState(**bonus_delta_values), triggered_scenarios
