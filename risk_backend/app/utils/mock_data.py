"""
Knowledge Base: 規則定義與風險配置
"""

# === 行為規則定義 (用於 Rule-based Engine) ===
MOCK_RULES = [
    {
        'rule_id': 'rule_behavior_001',
        'rule_name': 'unreplied_harassment',
        'description': '對方未回覆前持續發言',
        'conditions': {
            'unreplied_count': {'operator': '>=', 'threshold': 3}
        },
        'actions': {
            'harassment': 0.15
        },
        'enabled': True
    },
    {
        'rule_id': 'rule_behavior_002',
        'rule_name': 'volume_dumping',
        'description': '發言量極度不對等 (情緒傾倒)',
        'conditions': {
            'volume_ratio': {'operator': '>=', 'threshold': 3.0}
        },
        'actions': {
            'emotional_pressure': 0.10
        },
        'enabled': True
    },
    {
        'rule_id': 'rule_behavior_003',
        'rule_name': 'high_frequency_burst',
        'description': '短時間爆發式發送',
        'conditions': {
            'message_burst_count': {'operator': '>', 'threshold': 3}
        },
        'actions': {
            'emotional_pressure': 0.15,
            'harassment': 0.10
        },
        'enabled': True
    }
]

# === 風險融合配置 ===
MOCK_RISK_CONFIG = {
    'alpha': 0.6,           # Rule-based 權重 (行為)
    'beta': 0.4,            # NLP 權重 (語意)
    'decay_factor': 0.90,   # 衰減係數
    'T_LOW': 0.25,
    'T_HIGH': 0.60,
    'T_CRITICAL': 0.85
}
