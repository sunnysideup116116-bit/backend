"""
風險融合層 - 細粒度 10 級距配置版
"""

from app.models.schemas import RiskState
from app.services.kb_service import KBService

class RiskFusionLayer:
    def __init__(self):
        pass

    def fuse(self, rule_delta: RiskState, nlp_delta: RiskState, nlp_confidence: float = 0.5) -> RiskState:
        """
        根據 NLP 的信心度，從資料庫級距表檢索權重 (STEP 4)
        實作 10 級距細粒度融合，兼顧平滑性與可解釋性。
        """
        
        # 1. 從 MySQL 讀取配置 (！！！！改Config改這邊！！！！)
        config = KBService.get_fusion_config("threshold_v2_rule_heavy")
        
        # 預設基礎權重 (若 DB 讀取失敗時使用)
        b_dynamic = 0.3 
        tier_label = "Default (Rule-Heavy)"

        if config and 'weights' in config:
            weights_cfg = config['weights']
            
            # 2. 執行 10 級距細粒度搜尋
            # 遍歷從 SQL 讀取的 tiers 列表
            tiers = weights_cfg.get('tiers', [])
            for tier in tiers:
                # 判斷信心度落在哪個區間 [min, max]
                if tier['min'] <= nlp_confidence <= tier['max']:
                    b_dynamic = tier['beta']
                    tier_label = tier.get('label', 'Unknown Tier')
                    break
        
        a_dynamic = 1.0 - b_dynamic

        print(f"   [ Step 4: Fusion ] NLP 信心度: {nlp_confidence:.2f}")
        print(f"   [ Step 4: Fusion ] 匹配級距: {tier_label}")
        print(f"   [ Step 4: Fusion ] 最終權重分配: Rule(α)={a_dynamic:.2f}, NLP(β)={b_dynamic:.2f}")

        # 3. 執行加權融合計算
        total = {}
        fields = ['sexual_boundary', 'coercion', 'manipulation', 'harassment', 'emotional_pressure']
        for key in fields:
            # 權重融合公式：Total = α * Rule_Delta + β * NLP_Delta
            val = a_dynamic * getattr(rule_delta, key) + b_dynamic * getattr(nlp_delta, key)
            total[key] = val

        return RiskState(**total)

    def apply_scenario_bonus(self, base_delta: RiskState, bonus_delta: RiskState) -> RiskState:
        """
        實作演算法 STEP 6: 最終融合
        將初次融合結果加上二階情境獎勵 (Bonus)，並確保不超過 1.0
        """
        final_values = {}
        fields = ['sexual_boundary', 'coercion', 'manipulation', 'harassment', 'emotional_pressure']
        for field in fields:
            # 簡單相加，並限制最大值為 1.0 (防止風險值溢出)
            sum_val = getattr(base_delta, field) + getattr(bonus_delta, field)
            final_values[field] = min(1.0, sum_val)
            
        return RiskState(**final_values)
