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
    message_timestamp: Optional[str] = Field(
        None,
        description="訊息發送時間 (ISO 8601)。用於時段相關的情境判定；未提供時以處理當下為準。"
                    "正式運作可省略；離線重跑歷史訊息或評估時應帶入，以確保結果可重現。"
    )

class RiskDetectionResponse(BaseModel):
    """風險檢測回應"""
    conversation_id: str
    risk_delta_rule: RiskState
    risk_delta_nlp: RiskState
    risk_delta_total: RiskState
    new_risk_state: RiskState
    risk_level: str
    should_intervene: bool
    nlp_reasoning: Optional[str] = None
    nlp_confidence: float = Field(
        0.0,
        description="NLP 引擎對本次判斷的自評信心。融合層據此決定規則／語意的權重。"
    )
    nlp_degraded: bool = Field(
        False,
        description="NLP 是否走了 fallback（LLM 呼叫或解析失敗）。為 True 時語意通道的 delta 恆為 0，"
                    "本次判斷僅由規則引擎與情境層支撐，風險等級可能被低估。"
                    "呼叫端應將此視為『判斷不完整』而非『判定安全』；離線評估時必須排除或另行標記，"
                    "否則失敗案例會與真正的 safe 混在一起無法分辨（見 known-issues #17）。"
    )
    intervention_message: Optional[str] = None
    intervention_command: Optional[Any] = None
    triggered_rules: List[str] = []
    diagnostic_signals: Optional[Dict[str, float]] = Field(None)

class FeedbackRequest(BaseModel):
    """Receiver / sender 對某次介入的回饋"""
    triggered_by_msg_id: str
    role: str
    feedback: str

class SenderAppealRequest(BaseModel):
    """寄件方對某次介入的文字申訴。

    僅供人工稽核使用，**不進入任何演算法**：若讓被警告者自述無惡意即可
    降低風險分數，將形成可被濫用的繞道。
    """
    triggered_by_msg_id: str
    sender_id: str
    appeal_text: str = Field(..., min_length=1, max_length=2000,
                             description="寄件方對此次介入的說明或異議")
