import hashlib
import os
import json
from datetime import datetime, timezone
from typing import Optional
from dotenv import load_dotenv
from appwrite.client import Client
from appwrite.services.databases import Databases
from appwrite.query import Query
from appwrite.id import ID
from app.models.schemas import RiskState, Message
from app.core.appwrite_config import configure_appwrite_client
from app.services.relationship_service import RelationshipService
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

load_dotenv()

class ChatLogService:
    def __init__(self):
        self.client = Client()
        config = configure_appwrite_client(self.client)
        self.db = Databases(self.client)
        self.db_id = config.db_id
        self.rel_service = RelationshipService()

        # Initialize MongoDB Fallback Client
        try:
            from pymongo import MongoClient
            mongo_uri = os.getenv("MONGO_URI", "").strip()
            if not mongo_uri:
                raise RuntimeError("MONGO_URI is not configured")
            self.mongo_client = MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
            db_name = os.getenv("MONGO_DB_NAME", "profiling_db")
            self.mongo_db = self.mongo_client[db_name]
            self.mongo_state_coll = self.mongo_db["risk_state_history"]
        except Exception as e:
            print(f"[risk_backend] MongoDB init failed: {e}")
            self.mongo_state_coll = None

    async def log_message(self, req, msg_id: str = None, is_blocked: bool = False, delivery_status: str = "delivered"):
        """STEP 1: Store original message (支援預設 ID 與 狀態)"""
        try:
            msg_data = {
                "conversation_id": req.conversation_id,
                "sender_id": req.sender_id,
                "content": req.current_message,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "is_blocked": is_blocked,
                "delivery_status": delivery_status
            }
            # 如果是攔截或已送出，記錄審核時間
            if delivery_status in ["delivered", "blocked"]:
                msg_data["reviewed_at"] = datetime.now(timezone.utc).isoformat()
            if delivery_status == "delivered":
                msg_data["delivered_at"] = datetime.now(timezone.utc).isoformat()

            final_id = msg_id if msg_id else ID.unique()
            return self.db.create_document(self.db_id, "messages", final_id, msg_data)
        except Exception as e:
            print(f"log_message failed: {e}")
            return None

    async def update_message_status(self, msg_id: str, is_blocked: bool, status: str):
        """更新現有訊息的審核狀態"""
        try:
            now = datetime.now(timezone.utc).isoformat()
            data = {
                "is_blocked": is_blocked,
                "delivery_status": status,
                "reviewed_at": now
            }
            if status == "delivered":
                data["delivered_at"] = now
            
            return self.db.update_document(self.db_id, "messages", msg_id, data)
        except Exception as e:
            print(f"update_message_status failed: {e}")
            return None

    async def get_recent_messages(self, conversation_id: str, limit: int = 5, exclude_msg_id: str = None) -> list:
        """從資料庫抓取最近的歷史訊息 (語意對齊版：只抓取 delivered 訊息，供 LLM 使用)"""
        try:
            queries = [
                Query.equal("conversation_id", conversation_id),
                Query.equal("delivery_status", "delivered"), # 僅納入對方看得到的內容
                Query.order_desc("timestamp"),
                Query.limit(limit + 5) 
            ]
            
            response = self.db.list_documents(self.db_id, "messages", queries=queries)
            
            # 過濾掉排除的 ID
            docs = [d for d in response.documents if (d.id if hasattr(d, 'id') else d['$id']) != exclude_msg_id]
            docs = docs[:limit]
            
            messages = []
            for doc in reversed(docs):
                d = doc.data if hasattr(doc, 'data') else doc
                messages.append(Message(
                    sender=d.get('sender_id', 'User'),
                    content=d.get('content', ''),
                    timestamp=d.get('timestamp', '')
                ))
            return messages
        except Exception as e:
            print(f"get_recent_messages failed: {e}")
            return []

    async def get_recent_behavior_messages(self, conversation_id: str, limit: int = 20, exclude_msg_id: str = None) -> list:
        """從資料庫抓取最近的行為歷史 (行為對齊版：抓 delivered + pending_review，排除 blocked)"""
        try:
            queries = [
                Query.equal("conversation_id", conversation_id),
                Query.order_desc("timestamp"),
                Query.limit(limit + 5) 
            ]
            
            response = self.db.list_documents(self.db_id, "messages", queries=queries)
            
            # 在 Python 端過濾 delivery_status in ["delivered", "pending_review"]
            # 必須排除 blocked
            valid_statuses = {"delivered", "pending_review"}
            docs = []
            for d in response.documents:
                data = d.data if hasattr(d, 'data') else d
                d_id = d.id if hasattr(d, 'id') else d['$id']
                if data.get('delivery_status') in valid_statuses and d_id != exclude_msg_id:
                    docs.append(d)
            
            docs = docs[:limit]
            
            messages = []
            for doc in reversed(docs):
                d = doc.data if hasattr(doc, 'data') else doc
                messages.append(Message(
                    sender=d.get('sender_id', 'User'),
                    content=d.get('content', ''),
                    timestamp=d.get('timestamp', '')
                ))
            return messages
        except Exception as e:
            print(f"get_recent_behavior_messages failed: {e}")
            return []

    async def update_temporal_features(self, conv_id, user_id, temporal):
        """STEP 1: Update temporal features snapshot (新版欄位補齊)"""
        try:
            queries = [Query.equal("conversation_id", conv_id), Query.equal("user_id", user_id)]
            response = self.db.list_documents(self.db_id, "temporal_features", queries)
            
            data = {
                "conversation_id": conv_id,
                "user_id": user_id,
                "latency": float(temporal.latency),
                "frequency": float(temporal.frequency),
                "message_burst_count": int(temporal.message_burst_count),
                "last_message_time": datetime.now(timezone.utc).isoformat(),
                "reply_latency_seconds": temporal.reply_latency_seconds,
                "idle_time_seconds": temporal.idle_time_seconds,
                "unreplied_count": int(temporal.unreplied_count),
                "consecutive_char_count": int(temporal.consecutive_char_count),
                "message_ratio": float(temporal.message_ratio),
                "volume_ratio": float(temporal.volume_ratio),
                "avg_chars_per_message": float(temporal.avg_chars_per_message)
            }

            if response.documents:
                doc_id = response.documents[0].id if hasattr(response.documents[0], 'id') else response.documents[0]['$id']
                self.db.update_document(self.db_id, "temporal_features", doc_id, data)
            else:
                self.db.create_document(self.db_id, "temporal_features", ID.unique(), data)
        except Exception as e:
            print(f"update_temporal_features failed: {e}")

    async def log_analysis_detail(self, msg_id, conv_id, rule_res, nlp_res, final_delta, scenarios, diagnostic=None, flagged_words=None, classifier_flag=None):
        """STEP 2-6: Store full analysis summary"""
        try:
            data = {
                "message_id": msg_id,
                "conversation_id": conv_id,
                "delta_rule": json.dumps(rule_res['delta'].model_dump()),
                "delta_nlp": json.dumps(nlp_res['delta'].model_dump()),
                "delta_final": json.dumps(final_delta.model_dump()),
                "nlp_reasoning": nlp_res.get('reasoning', 'No reasoning provided'),
                "confidence": float(nlp_res.get('confidence', 0.5)),
                # NLP 判定本則成立的特徵，作為可解釋的審計軌跡（B5-①）
                "detected_features": json.dumps(nlp_res.get("detected_features", []), ensure_ascii=False)[:1000],
                "triggered_rules": json.dumps(rule_res['triggered_rules']),
                "triggered_scenarios": json.dumps(scenarios),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "max_score": float(diagnostic.get('max_score', 0.0)) if diagnostic else 0.0,
                "spread_score": float(diagnostic.get('spread_score', 0.0)) if diagnostic else 0.0,
                "trend_score": float(diagnostic.get('trend_score', 0.0)) if diagnostic else 0.0,
                "composite_score": float(diagnostic.get('composite_score', 0.0)) if diagnostic else 0.0,
                "guardrail_flagged_words": json.dumps(flagged_words or []),
                "guardrail_classifier_flag": json.dumps(classifier_flag or {}),
            }
            self.db.create_document(self.db_id, "risk_analysis_logs_", ID.unique(), data)
        except Exception as e:
            print(f"log_analysis_detail failed: {e}")

    async def get_latest_risk_state_with_time(self, conversation_id: str, user_id: str):
        """Fetch latest risk state and its timestamp"""
        try:
            response = self.db.list_documents(
                database_id=self.db_id,
                collection_id="risk_state_history",
                queries=[
                    Query.equal("conversation_id", conversation_id),
                    Query.equal("user_id", user_id),
                    Query.order_desc("timestamp"),
                    Query.limit(1)
                ]
            )
            if response.documents:
                doc = response.documents[0]
                d = doc.data if hasattr(doc, 'data') else doc.to_dict()
                state_data = json.loads(d.get('risk_state', '{}'))
                return RiskState(**state_data), d.get('timestamp')
        except Exception as e:
            print(f"Read risk state from Appwrite failed: {e}, falling back to MongoDB")
            
        # MongoDB Fallback
        if self.mongo_state_coll is not None:
            try:
                doc = self.mongo_state_coll.find_one(
                    {"conversation_id": conversation_id, "user_id": user_id},
                    sort=[("timestamp", -1)]
                )
                if doc:
                    state_data = doc.get("risk_state")
                    if isinstance(state_data, str):
                        state_data = json.loads(state_data)
                    return RiskState(**(state_data or {})), doc.get("timestamp")
            except Exception as mongo_err:
                print(f"Read risk state from MongoDB failed: {mongo_err}")
                
        return RiskState(sexual_boundary=0.0, coercion=0.0, manipulation=0.0, harassment=0.0, emotional_pressure=0.0), None

    async def get_recent_risk_state_history(self, conversation_id: str, user_id: str, limit: int = 5):
        """Fetch recent history for trend analysis"""
        try:
            response = self.db.list_documents(
                database_id=self.db_id,
                collection_id="risk_state_history",
                queries=[
                    Query.equal("conversation_id", conversation_id),
                    Query.equal("user_id", user_id),
                    Query.order_desc("timestamp"),
                    Query.limit(limit)
                ]
            )
            states = []
            for doc in response.documents:
                d = doc.data if hasattr(doc, 'data') else doc.to_dict()
                states.append(RiskState(**json.loads(d.get('risk_state', '{}'))))
            return states
        except Exception as e:
            print(f"get_recent_risk_state_history from Appwrite failed: {e}, falling back to MongoDB")
            
        # MongoDB Fallback
        if self.mongo_state_coll is not None:
            try:
                docs = list(self.mongo_state_coll.find(
                    {"conversation_id": conversation_id, "user_id": user_id}
                ).sort("timestamp", -1).limit(limit))
                states = []
                for doc in docs:
                    state_data = doc.get("risk_state")
                    if isinstance(state_data, str):
                        state_data = json.loads(state_data)
                    states.append(RiskState(**(state_data or {})))
                return states
            except Exception as mongo_err:
                print(f"get_recent_risk_state_history from MongoDB failed: {mongo_err}")
                
        return []

    async def save_risk_state_history(self, conversation_id, user_id, msg_id, risk_state, level, delta_total, decay_applied: bool = False):
        """STEP 7, 8: Store cumulative risk state"""
        data = {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "triggered_by_msg_id": msg_id,
            "risk_state": json.dumps(risk_state.model_dump()) if hasattr(risk_state, "model_dump") else json.dumps(risk_state),
            "risk_level": level,
            "risk_delta_total": json.dumps(delta_total.model_dump()) if hasattr(delta_total, "model_dump") else json.dumps(delta_total),
            "decay_applied": decay_applied,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        try:
            self.db.create_document(self.db_id, "risk_state_history", ID.unique(), data)
        except Exception as e:
            print(f"save_risk_state_history to Appwrite failed: {e}, falling back to MongoDB")
            
        # MongoDB Fallback
        if self.mongo_state_coll is not None:
            try:
                self.mongo_state_coll.insert_one(data.copy())
            except Exception as mongo_err:
                print(f"save_risk_state_history to MongoDB failed: {mongo_err}")

    async def log_intervention(self, conversation_id, triggered_by_msg_id,
                               sender_id, receiver_id, risk_level, risk_state,
                               diagnosis, decision_reason, primary_risk,
                               sender_action, receiver_action, cooldown_seconds: int = 0):
        """STEP 9: Store professional intervention logs into Appwrite"""
        try:
            log_data = {
                "triggered_by_msg_id":  triggered_by_msg_id,
                "conversation_id":      conversation_id,
                "user_id":              sender_id, # 修正：將 sender_id 映射到 user_id
                "sender_id":            sender_id,
                "receiver_id":          receiver_id,
                "risk_level":           risk_level,
                "action_taken":         sender_action, # 修正：紀錄執行的動作
                "sender_action":        sender_action,
                "receiver_action":      receiver_action,
                "decision_reason":      decision_reason,
                "primary_risk_type":    primary_risk,
                "timestamp":            datetime.now(timezone.utc).isoformat(),
                "risk_state_snapshot":  json.dumps(risk_state.model_dump()),
                "composite_score":      float(diagnosis.get("composite_score", 0.0)),
                "max_score":            float(diagnosis.get("max_score", 0.0)),
                "spread_score":         float(diagnosis.get("spread_score", 0.0)),
                "trend_score":          float(diagnosis.get("trend_score", 0.0)),
                "cooldown_seconds":     int(cooldown_seconds),
                "sender_feedback":      None,
                "receiver_feedback":    None
            }
            self.db.create_document(self.db_id, "intervention_logs", ID.unique(), log_data)
            return True
        except Exception as e:
            print(f"log_intervention failed: {e}")
            return False

    async def get_remaining_cooldown(self, conversation_id: str, user_id: str) -> int:
        """依最近一筆介入記錄計算寄件方剩餘的冷卻秒數；無紀錄或已過期回 0。

        冷卻必須由後端依「當初施加的秒數 − 已經過時間」推算，前端才能在重開
        App 或換裝置後還原倒數。若僅由前端自行計時，關閉重開即形同解除。
        """
        try:
            response = self.db.list_documents(
                self.db_id, "intervention_logs",
                queries=[
                    Query.equal("conversation_id", conversation_id),
                    Query.equal("user_id", user_id),
                    Query.order_desc("timestamp"),
                    Query.limit(1)
                ]
            )
            if not response.documents:
                return 0
            doc = response.documents[0]
            data = doc.data if hasattr(doc, 'data') else doc

            cooldown = int(data.get("cooldown_seconds") or 0)
            if cooldown <= 0:
                return 0

            ts = data.get("timestamp")
            if not ts:
                return 0
            last_ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)

            elapsed = (datetime.now(timezone.utc) - last_ts).total_seconds()
            return max(0, int(cooldown - elapsed))
        except Exception as e:
            print(f"get_remaining_cooldown failed: {e}")
            return 0

    async def get_last_displayed_intervention(self, conversation_id: str, user_id: str, role: str) -> Optional[dict]:
        """取得該對話／使用者最近一次**實際對指定角色顯示過**的介入。

        用於介入顯示節流：被節流而未顯示的紀錄，其 `{role}_action` 會是 "none" 或
        "suppressed"，不應視為「上次顯示」，否則節流窗會被自己不斷推遲。

        回傳 {"risk_level": str, "timestamp": str} 或 None。
        """
        if role not in ("sender", "receiver"):
            return None
        field = f"{role}_action"
        try:
            response = self.db.list_documents(
                self.db_id, "intervention_logs",
                queries=[
                    Query.equal("conversation_id", conversation_id),
                    Query.equal("user_id", user_id),
                    Query.order_desc("timestamp"),
                    Query.limit(20),
                ]
            )
            for doc in response.documents:
                data = doc.data if hasattr(doc, 'data') else doc
                action = data.get(field)
                if action and action not in ("none", "suppressed"):
                    return {
                        "risk_level": data.get("risk_level"),
                        "timestamp": data.get("timestamp"),
                    }
            return None
        except Exception as e:
            print(f"get_last_displayed_intervention failed: {e}")
            return None

    async def update_intervention_feedback(self, msg_id: str, role: str, feedback: str, detail: Optional[str] = None) -> bool:
        """
        更新 intervention_logs 內某則訊息的 sender_feedback 或 receiver_feedback 欄位。
        role 必須是 'sender' 或 'receiver'；feedback 必須是 'comfortable' 或 'uncomfortable'。
        detail 為選填的詳細說明，僅寫入 receiver_feedback_detail / sender_feedback_detail，
        不參與任何風險計算。
        """
        if role not in ("sender", "receiver"):
            return False
        if feedback not in ("comfortable", "uncomfortable"):
            return False

        try:
            response = self.db.list_documents(
                self.db_id, "intervention_logs",
                queries=[
                    Query.equal("triggered_by_msg_id", msg_id),
                    Query.order_desc("timestamp"),
                    Query.limit(1)
                ]
            )
            if not response.documents:
                return False

            doc = response.documents[0]
            doc_id = doc.id if hasattr(doc, 'id') else doc['$id']

            update_data = {f"{role}_feedback": feedback}
            if detail and detail.strip():
                update_data[f"{role}_feedback_detail"] = detail.strip()[:2000]
            self.db.update_document(
                self.db_id, "intervention_logs", doc_id,
                update_data
            )
            return True
        except Exception as e:
            print(f"update_intervention_feedback failed: {e}")
            return False

    async def get_recent_feedbacks(self, conversation_id: str, sender_id: str, limit: int = 5) -> list:
        """
        取得最近 N 次該 sender 在該對話內被回饋的 receiver_feedback 值。
        回傳 ['comfortable', 'uncomfortable', ...]（時間倒序），None / 空值會被過濾。
        """
        try:
            response = self.db.list_documents(
                self.db_id, "intervention_logs",
                queries=[
                    Query.equal("conversation_id", conversation_id),
                    Query.equal("sender_id", sender_id),
                    Query.order_desc("timestamp"),
                    Query.limit(limit),
                ]
            )
            feedbacks = []
            for doc in response.documents:
                data = doc.data if hasattr(doc, 'data') else doc
                fb = data.get('receiver_feedback')
                if fb:
                    feedbacks.append(fb)
            return feedbacks
        except Exception as e:
            print(f"get_recent_feedbacks failed: {e}")
            return []

    async def save_guardrail_context_review(
        self,
        conversation_id: str,
        sender_id: str,
        msg_id: str,
        flagged_words: list,
        classifier_flag: dict,
        judgment: str,
        reasoning: str,
        model: str,
    ) -> bool:
        """Store a background context judgment for Step 0 guardrail flags."""
        try:
            data = {
                "conversation_id": conversation_id,
                "sender_id": sender_id,
                "triggered_by_msg_id": msg_id,
                "flagged_words": json.dumps(flagged_words or []),
                "classifier_flag": json.dumps(classifier_flag or {}),
                "judgment": judgment,
                "reasoning": reasoning,
                "model": model,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self.db.create_document(self.db_id, "guardrail_context_reviews", ID.unique(), data)
            return True
        except Exception as e:
            print(f"save_guardrail_context_review failed: {e}")
            return False

    async def get_recent_guardrail_context_reviews(self, conversation_id: str, sender_id: str, limit: int = 5) -> list:
        """Return recent background guardrail judgments for a sender in a conversation."""
        try:
            response = self.db.list_documents(
                self.db_id, "guardrail_context_reviews",
                queries=[
                    Query.equal("conversation_id", conversation_id),
                    Query.equal("sender_id", sender_id),
                    Query.order_desc("timestamp"),
                    Query.limit(limit),
                ]
            )
            judgments = []
            for doc in response.documents:
                data = doc.data if hasattr(doc, 'data') else doc
                judgment = data.get("judgment")
                if judgment:
                    judgments.append(judgment)
            return judgments
        except Exception as e:
            print(f"get_recent_guardrail_context_reviews failed: {e}")
            return []

    async def save_sender_appeal(self, msg_id: str, sender_id: str, appeal_text: str) -> dict:
        """寫入寄件方對某次介入的文字申訴，供人工稽核。

        **此內容不進入任何演算法**：若讓被警告者自述無惡意即可降低風險分數，
        將形成可被濫用的繞道。僅寫入 intervention_logs 供後台並列檢視。

        回傳 {"ok": bool, "error": str|None}。
        需 Appwrite 的 intervention_logs collection 具備 `sender_appeal_text` 屬性
        （String，建議 size 2000）；屬性未建立時會回傳明確錯誤而非靜默失敗。
        """
        try:
            response = self.db.list_documents(
                self.db_id, "intervention_logs",
                queries=[
                    Query.equal("triggered_by_msg_id", msg_id),
                    Query.order_desc("timestamp"),
                    Query.limit(1)
                ]
            )
            if not response.documents:
                return {"ok": False, "error": "not_found"}

            doc = response.documents[0]
            data = doc.data if hasattr(doc, 'data') else doc
            doc_id = doc.id if hasattr(doc, 'id') else doc['$id']

            # 僅允許該則訊息的寄件方本人提出申訴
            if data.get("sender_id") != sender_id:
                return {"ok": False, "error": "sender_mismatch"}

            self.db.update_document(
                self.db_id, "intervention_logs", doc_id,
                {"sender_appeal_text": appeal_text}
            )
            return {"ok": True, "error": None}
        except Exception as e:
            msg = str(e)
            if "sender_appeal_text" in msg or "Unknown attribute" in msg or "Invalid document structure" in msg:
                print("save_sender_appeal failed: Appwrite intervention_logs 尚未建立 sender_appeal_text 屬性")
                return {"ok": False, "error": "attribute_missing"}
            print(f"save_sender_appeal failed: {e}")
            return {"ok": False, "error": "unknown"}

    async def save_receiver_report(self, msg_id: str, receiver_id: str, report_text: str) -> dict:
        """寫入收件方對某次介入／該則訊息的文字回報，供人工稽核。

        與 save_sender_appeal 對稱但角色相反：一個是被警告者自辯、一個是被保護者
        陳述，混用會使後台無從分辨。**此內容不進入任何演算法**。

        回傳 {"ok": bool, "error": str|None}。
        需 Appwrite 的 intervention_logs collection 具備 `receiver_report_text` 屬性
        （String，建議 size 2000）；屬性未建立時回傳明確錯誤而非靜默失敗。
        """
        try:
            response = self.db.list_documents(
                self.db_id, "intervention_logs",
                queries=[
                    Query.equal("triggered_by_msg_id", msg_id),
                    Query.order_desc("timestamp"),
                    Query.limit(1)
                ]
            )
            if not response.documents:
                return {"ok": False, "error": "not_found"}

            doc = response.documents[0]
            data = doc.data if hasattr(doc, 'data') else doc
            doc_id = doc.id if hasattr(doc, 'id') else doc['$id']

            # 僅允許該則訊息的收件方本人提出回報
            if data.get("receiver_id") != receiver_id:
                return {"ok": False, "error": "receiver_mismatch"}

            self.db.update_document(
                self.db_id, "intervention_logs", doc_id,
                {"receiver_report_text": report_text}
            )
            return {"ok": True, "error": None}
        except Exception as e:
            msg = str(e)
            if "receiver_report_text" in msg or "Unknown attribute" in msg or "Invalid document structure" in msg:
                print("save_receiver_report failed: Appwrite intervention_logs 尚未建立 receiver_report_text 屬性")
                return {"ok": False, "error": "attribute_missing"}
            print(f"save_receiver_report failed: {e}")
            return {"ok": False, "error": "unknown"}

    @staticmethod
    def _block_store_error(error: Exception) -> str:
        message = str(error).lower()
        if "user_blocks" in message or "could not be found" in message or "not found" in message:
            return "collection_missing"
        return "unavailable"

    async def save_user_block(
        self,
        blocker_id: str,
        blocked_id: str,
        conversation_id: str | None = None,
        source: str = "manual",
    ) -> dict:
        """Persist a reversible block. Repeated calls are idempotent."""
        if blocker_id == blocked_id:
            return {"ok": False, "already": False, "error": "self_block"}
        try:
            existing = self.db.list_documents(
                self.db_id,
                "user_blocks",
                queries=[
                    Query.equal("blocker_id", blocker_id),
                    Query.equal("blocked_id", blocked_id),
                    Query.limit(1),
                ],
            )
            if existing.documents:
                return {"ok": True, "already": True, "error": None}

            pair_digest = hashlib.sha256(
                f"{blocker_id}\0{blocked_id}".encode("utf-8")
            ).hexdigest()[:32]
            try:
                self.db.create_document(
                    self.db_id,
                    "user_blocks",
                    f"block_{pair_digest}",
                    {
                        "blocker_id": blocker_id,
                        "blocked_id": blocked_id,
                        "conversation_id": conversation_id or "",
                        "source": source,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            except Exception as error:
                if getattr(error, "code", None) == 409 or "already exists" in str(error).lower():
                    return {"ok": True, "already": True, "error": None}
                raise
            return {"ok": True, "already": False, "error": None}
        except Exception as error:
            code = self._block_store_error(error)
            print(f"save_user_block failed ({code}): {type(error).__name__}")
            return {"ok": False, "already": False, "error": code}

    async def remove_user_block(self, blocker_id: str, blocked_id: str) -> dict:
        try:
            existing = self.db.list_documents(
                self.db_id,
                "user_blocks",
                queries=[
                    Query.equal("blocker_id", blocker_id),
                    Query.equal("blocked_id", blocked_id),
                    Query.limit(25),
                ],
            )
            if not existing.documents:
                return {"ok": False, "error": "not_found"}
            for document in existing.documents:
                data = document.data if hasattr(document, "data") else document
                document_id = getattr(document, "id", None) or data.get("$id")
                self.db.delete_document(self.db_id, "user_blocks", document_id)
            return {"ok": True, "error": None}
        except Exception as error:
            code = self._block_store_error(error)
            print(f"remove_user_block failed ({code}): {type(error).__name__}")
            return {"ok": False, "error": code}

    async def get_user_block_sets(self, user_id: str) -> dict:
        """Return outgoing blocks and the bidirectional safety exclusion set."""
        outgoing: set[str] = set()
        incoming: set[str] = set()
        for field, take, target in (
            ("blocker_id", "blocked_id", outgoing),
            ("blocked_id", "blocker_id", incoming),
        ):
            try:
                response = self.db.list_documents(
                    self.db_id,
                    "user_blocks",
                    queries=[Query.equal(field, user_id), Query.limit(500)],
                )
                for document in response.documents:
                    data = document.data if hasattr(document, "data") else document
                    other = str(data.get(take) or "").strip()
                    if other and other != user_id:
                        target.add(other)
            except Exception as error:
                code = self._block_store_error(error)
                print(f"get_user_block_sets failed ({field}, {code}): {type(error).__name__}")
                return {
                    "ok": False,
                    "error": code,
                    "blocked_user_ids": [],
                    "excluded_user_ids": [],
                }
        return {
            "ok": True,
            "error": None,
            "blocked_user_ids": sorted(outgoing),
            "excluded_user_ids": sorted(outgoing | incoming),
        }

    async def get_blocked_user_ids(self, user_id: str) -> list:
        result = await self.get_user_block_sets(user_id)
        return result["excluded_user_ids"] if result["ok"] else []

    async def save_user_report(
        self,
        reporter_id: str,
        reported_id: str,
        reason_category: str,
        detail_text: str | None = None,
        conversation_id: str | None = None,
        triggered_by_msg_id: str | None = None,
    ) -> dict:
        if reporter_id == reported_id:
            return {"ok": False, "error": "self_report"}
        try:
            document = self.db.create_document(
                self.db_id,
                "user_reports",
                ID.unique(),
                {
                    "reporter_id": reporter_id,
                    "reported_id": reported_id,
                    "conversation_id": conversation_id or "",
                    "reason_category": reason_category,
                    "detail_text": detail_text or "",
                    "triggered_by_msg_id": triggered_by_msg_id or "",
                    "status": "pending",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            data = document.data if hasattr(document, "data") else document
            report_id = getattr(document, "id", None) or data.get("$id")
            return {"ok": True, "report_id": report_id, "error": None}
        except Exception as error:
            message = str(error).lower()
            code = (
                "collection_missing"
                if "user_reports" in message or "could not be found" in message or "not found" in message
                else "unavailable"
            )
            print(f"save_user_report failed ({code}): {type(error).__name__}")
            return {"ok": False, "error": code}
