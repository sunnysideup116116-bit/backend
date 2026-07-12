"""
Pydantic 資料模型 - 規範化版本 (與 Appwrite 0518 Schema 深度對齊版)
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Any, Dict

class TemporalFeatures(BaseModel):
    """時間行為特徵 - 擴充版 (與 Appwrite Integer/Optional 對齊)"""
    # 原始數據 (以原始秒數整數為準，對齊 Appwrite Integer Type)
    reply_latency_seconds: Optional[int] = Field(None, description="對方說完後，我多久才回")
    idle_time_seconds: Optional[int] = Field(None, description="上一則訊息距離現在多久")
    
    # 統計特徵
    unreplied_count: int = Field(0, description="對方未回覆前，我方連續發言數")
    consecutive_char_count: int = Field(0, description="本次連續發言累計字數")
    message_ratio: float = Field(1.0, description="近期發言數量佔比")
    volume_ratio: float = Field(1.0, description="近期發言字數佔比")
    avg_chars_per_message: float = Field(0.0, description="近期平均單則訊息字數")
    
    # 舊有相容欄位
    latency: float = Field(0.0, ge=0, le=1)
    frequency: float = Field(0.0, ge=0, le=1)
    message_burst_count: int = Field(0, ge=0)

class RiskState(BaseModel):
    """風險狀態 (允許負值以支援風險降權邏輯)"""
    sexual_boundary: float = Field(0.0, le=1.0)
    coercion: float = Field(0.0, le=1.0)
    manipulation: float = Field(0.0, le=1.0)
    harassment: float = Field(0.0, le=1.0)
    emotional_pressure: float = Field(0.0, le=1.0)

class Message(BaseModel):
    """訊息 (用於 LLM 上下文，應僅包含 delivered 內容)"""
    sender: str
    content: str
    timestamp: str

class RelationshipMetrics(BaseModel):
    """關係動力學指標 (L2) - 規格對齊版"""
    conversation_id: str
    user_a_id: str
    user_b_id: str
    total_messages: int = 0
    user_a_message_count: int = 0
    user_b_message_count: int = 0
    familiarity_score: float = 0.0
    conversation_balance: float = 0.5
    interaction_days: int = 1
    first_contact_at: str
    last_contact_at: Optional[str] = None
    updated_at: Optional[str] = None
    intimacy_progression_rate: float = 0.0

class ConversationSummary(BaseModel):
    """對話語意摘要 (L1) - 規格對齊版"""
    conversation_id: str
    summary_content: str
    intimacy_level: float = 0.0
    version: int = 1
    main_topics: Optional[str] = None # 對齊 Appwrite String Type (JSON String)
    tone_shift: Optional[str] = "stable"
    first_processed_msg_id: Optional[str] = None
    last_processed_msg_id: Optional[str] = None
    updated_at: str
    # 可解釋性欄位
    conversation_summaries_reasoning: Optional[str] = None
    self_disclosure_depth: float = 0.0
    emotional_intensity: float = 0.0
    exclusivity_framing: float = 0.0
    physical_intimacy_reference: float = 0.0

class RiskDetectionRequest(BaseModel):
    """風險檢測請求"""
    conversation_id: str
    current_message: str
    sender_id: str
    receiver_id: str
    recent_messages: List[Message] = []
    temporal_features: Optional[TemporalFeatures] = None
    prior_risk_state: Optional[RiskState] = None
    relationship_memory: Optional[RelationshipMetrics] = None
    last_summary: Optional[ConversationSummary] = None

class RiskDetectionResponse(BaseModel):
    """風險檢測回應"""
    conversation_id: str
    risk_delta_rule: RiskState
    risk_delta_nlp: RiskState
    risk_delta_total: RiskState
    new_risk_state: RiskState
    risk_level: str
    should_intervene: bool
    intervention_message: Optional[str] = None
    intervention_command: Optional[Any] = None
    triggered_rules: List[str] = []
    diagnostic_signals: Optional[Dict[str, float]] = Field(None)

class FeedbackRequest(BaseModel):
    """Receiver / sender 對某次介入的回饋"""
    triggered_by_msg_id: str
    role: str
    feedback: str
