import json
import os
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import HTMLResponse
import requests
from dotenv import load_dotenv

project_env = Path(__file__).resolve().parents[2] / ".env"
if project_env.exists():
    load_dotenv(dotenv_path=project_env)

router = APIRouter(tags=["Admin Audit"])

AGENT_URL = os.getenv("MATCHMAKER_AGENT_URL", "http://127.0.0.1:9001")
RISK_URL = os.getenv("RISK_SERVICE_URL", "http://127.0.0.1:8001")


def _get_appwrite_db():
    try:
        from appwrite.client import Client
        from appwrite.services.databases import Databases
        from appwrite.query import Query as AWQuery

        endpoint = os.getenv("APPWRITE_ENDPOINT", "https://appwrite.misproject.us.ci/v1").strip()
        project_id = (os.getenv("APPWRITE_PROJECT_ID") or os.getenv("APPWRITE_PROJECT") or "").strip()
        api_key = os.getenv("APPWRITE_API_KEY", "").strip()
        db_id = (os.getenv("APPWRITE_DB_ID") or os.getenv("APPWRITE_DATABASE_ID") or "").strip()

        if not project_id or not db_id:
            return None, None, None

        client = Client()
        client.set_endpoint(endpoint)
        client.set_project(project_id)
        if api_key:
            client.set_key(api_key)

        return Databases(client), db_id, AWQuery
    except Exception as exc:
        print(f"[admin] Appwrite client setup failed: {exc}")
        return None, None, None


@router.get("/admin/dashboard", response_class=HTMLResponse)
def serve_admin_dashboard():
    """Serve the Admin Audit Dashboard web page."""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    file_path = os.path.join(base_dir, "admin_dashboard.html")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="admin_dashboard.html not found")
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()


@router.get("/api/admin/audit-logs")
def get_audit_logs(
    limit: int = Query(200, ge=1, le=500),
    status: Optional[str] = Query(None, description="filter by delivery_status or risk_level"),
    conversation_id: Optional[str] = Query(None, description="filter by conversation_id"),
):
    """Fetch combined audit logs from Appwrite (messages, risk_analysis_logs, risk_state_history, intervention_logs)."""
    db, db_id, AWQuery = _get_appwrite_db()

    if not db or not db_id:
        return {"logs": [], "total": 0, "notice": "Appwrite Database not configured"}

    try:
        # 1. Fetch recent messages
        msg_queries = [AWQuery.order_desc("timestamp"), AWQuery.limit(limit)]
        if conversation_id:
            msg_queries.append(AWQuery.equal("conversation_id", conversation_id))

        msg_res = db.list_documents(db_id, "messages", queries=msg_queries)
        msg_docs = msg_res.get("documents", []) if isinstance(msg_res, dict) else msg_res.documents

        # 2. Fetch analysis details map (by message_id)
        analysis_map = {}
        try:
            an_res = db.list_documents(db_id, "risk_analysis_logs", queries=[AWQuery.order_desc("timestamp"), AWQuery.limit(limit * 2)])
            an_docs = an_res.get("documents", []) if isinstance(an_res, dict) else an_res.documents
            for doc in an_docs:
                d = doc if isinstance(doc, dict) else (doc.data if hasattr(doc, 'data') else doc.to_dict())
                m_id = d.get("message_id")
                if m_id and m_id not in analysis_map:
                    analysis_map[m_id] = d
        except Exception as e:
            print(f"[admin] fetch risk_analysis_logs failed: {e}")

        # 3. Fetch risk state history map (by triggered_by_msg_id)
        state_map = {}
        try:
            st_res = db.list_documents(db_id, "risk_state_history", queries=[AWQuery.order_desc("timestamp"), AWQuery.limit(limit * 2)])
            st_docs = st_res.get("documents", []) if isinstance(st_res, dict) else st_res.documents
            for doc in st_docs:
                d = doc if isinstance(doc, dict) else (doc.data if hasattr(doc, 'data') else doc.to_dict())
                t_id = d.get("triggered_by_msg_id")
                if t_id and t_id not in state_map:
                    state_map[t_id] = d
        except Exception as e:
            print(f"[admin] fetch risk_state_history failed: {e}")

        # 4. Fetch intervention logs map
        intervention_map = {}
        try:
            it_res = db.list_documents(db_id, "intervention_logs", queries=[AWQuery.order_desc("timestamp"), AWQuery.limit(limit * 2)])
            it_docs = it_res.get("documents", []) if isinstance(it_res, dict) else it_res.documents
            for doc in it_docs:
                d = doc if isinstance(doc, dict) else (doc.data if hasattr(doc, 'data') else doc.to_dict())
                t_id = d.get("triggered_by_msg_id")
                if t_id and t_id not in intervention_map:
                    intervention_map[t_id] = d
        except Exception as e:
            print(f"[admin] fetch intervention_logs failed: {e}")

        logs = []
        for m in msg_docs:
            m_data = m if isinstance(m, dict) else (m.data if hasattr(m, 'data') else m.to_dict())
            sender = m_data.get("sender_id", "")
            if sender == "ai_assistant" or "assistant" in sender:
                continue
            m_id = m_data.get("$id") or m_data.get("id") or m_data.get("msg_id")

            an = analysis_map.get(m_id, {})
            st = state_map.get(m_id, {})
            it = intervention_map.get(m_id, {})

            delta_nlp = {}
            if an.get("delta_nlp"):
                try:
                    delta_nlp = json.loads(an["delta_nlp"])
                except Exception:
                    pass

            risk_state_dict = {}
            if st.get("risk_state"):
                try:
                    risk_state_dict = json.loads(st["risk_state"])
                except Exception:
                    pass

            delta_final_dict = {}
            if an.get("delta_final"):
                try:
                    delta_final_dict = json.loads(an["delta_final"])
                except Exception:
                    pass

            triggered_rules = []
            if an.get("triggered_rules"):
                try:
                    triggered_rules = json.loads(an["triggered_rules"])
                except Exception:
                    pass

            triggered_scenarios = []
            if an.get("triggered_scenarios"):
                try:
                    triggered_scenarios = json.loads(an["triggered_scenarios"])
                except Exception:
                    pass

            flagged_words = []
            if an.get("guardrail_flagged_words"):
                try:
                    flagged_words = json.loads(an["guardrail_flagged_words"])
                except Exception:
                    pass

            risk_level = st.get("risk_level") or it.get("risk_level") or ("blocked" if m_data.get("is_blocked") else "normal")
            delivery_status = m_data.get("delivery_status", "delivered")

            item = {
                "message_id": m_id,
                "conversation_id": m_data.get("conversation_id", ""),
                "sender_id": m_data.get("sender_id", ""),
                "content": m_data.get("content", ""),
                "timestamp": m_data.get("timestamp", ""),
                "delivery_status": delivery_status,
                "is_blocked": bool(m_data.get("is_blocked", False)),
                "risk_level": risk_level,
                "nlp_reasoning": an.get("nlp_reasoning", "無 NLP 診斷摘要"),
                "confidence": float(an.get("confidence", 0.0)),
                "delta_nlp": delta_nlp,
                "risk_state": risk_state_dict,
                "delta_final": delta_final_dict,
                "triggered_rules": triggered_rules,
                "triggered_scenarios": triggered_scenarios,
                "diagnostic_signals": {
                    "composite": float(an.get("composite_score", 0.0)),
                    "max": float(an.get("max_score", 0.0)),
                    "spread": float(an.get("spread_score", 0.0)),
                    "trend": float(an.get("trend_score", 0.0)),
                },
                "guardrail_flagged_words": flagged_words,
                "sender_action": it.get("sender_action", "none"),
                "receiver_action": it.get("receiver_action", "none"),
                "decision_reason": it.get("decision_reason", ""),
            }

            if status:
                s_lower = status.lower()
                if s_lower not in (risk_level.lower(), delivery_status.lower()):
                    continue

            logs.append(item)

        return {"logs": logs, "total": len(logs)}

    except Exception as exc:
        print(f"[admin] Appwrite fetch failed, falling back to MongoDB: {exc}")

    # MongoDB Fallback
    try:
        from database import messages_coll
        query_filter = {}
        if conversation_id:
            query_filter["room_id"] = conversation_id

        mongo_docs = list(messages_coll.find(query_filter).sort("timestamp", -1).limit(limit))
        mongo_logs = []
        for m in mongo_docs:
            sender = m.get("sender_id", "")
            if sender == "ai_assistant" or "assistant" in sender:
                continue
            m_id = str(m.get("_id", ""))
            ra = m.get("risk_assessment") or {}
            risk_state_dict = ra.get("new_risk_state") or ra.get("risk_state") or ra.get("risk_scores") or {}
            risk_delta_dict = ra.get("risk_delta_total") or ra.get("risk_delta_nlp") or ra.get("risk_scores") or {}
            diag = ra.get("diagnostic_signals") or {}

            is_blocked = bool(m.get("is_blocked", False)) or (ra.get("risk_level") == "blocked")
            r_level = ra.get("risk_level") or ("blocked" if is_blocked else "normal")
            delivery_status = m.get("delivery_status") or ("blocked" if is_blocked else "delivered")

            ts_val = m.get("timestamp")
            if isinstance(ts_val, (int, float)):
                import datetime
                ts_str = datetime.datetime.fromtimestamp(ts_val, datetime.timezone.utc).isoformat()
            else:
                ts_str = str(ts_val or "")

            item = {
                "message_id": m_id,
                "conversation_id": m.get("room_id", ""),
                "sender_id": m.get("sender_id", ""),
                "content": m.get("content", ""),
                "timestamp": ts_str,
                "delivery_status": "blocked" if is_blocked else "delivered",
                "is_blocked": is_blocked,
                "risk_level": r_level,
                "nlp_reasoning": ra.get("nlp_reasoning") or ra.get("reasoning") or "無 NLP 診斷摘要",
                "confidence": float(ra.get("confidence", 0.85)),
                "delta_nlp": risk_delta_dict,
                "risk_state": risk_state_dict,
                "delta_final": risk_delta_dict,
                "triggered_rules": ra.get("triggered_rules", []),
                "triggered_scenarios": [],
                "diagnostic_signals": {
                    "composite": float(diag.get("composite", 0.0)),
                    "max": float(diag.get("max", 0.0)),
                    "spread": float(diag.get("spread", 0.0)),
                    "trend": float(diag.get("trend", 0.0)),
                },
                "guardrail_flagged_words": ra.get("flagged_words", []),
                "sender_action": "none",
                "receiver_action": "none",
                "decision_reason": ra.get("reason", ""),
            }

            if status:
                s_lower = status.lower()
                if s_lower not in (r_level.lower(), item["delivery_status"].lower()):
                    continue

            mongo_logs.append(item)

        return {"logs": mongo_logs, "total": len(mongo_logs), "source": "mongodb"}
    except Exception as mongo_err:
        print(f"[admin] Mongo fallback error: {mongo_err}")
        return {"logs": [], "total": 0, "error": str(mongo_err)}


@router.get("/api/admin/users")
def get_user_list():
    """Get list of users from MongoDB for memory inspection."""
    from database import profiles_coll
    try:
        profiles = list(profiles_coll.find({}, {"user_id": 1, "name": 1, "mbti": 1, "profile_memory_summary": 1}).limit(50))
        users = []
        for p in profiles:
            users.append({
                "user_id": p.get("user_id", ""),
                "name": p.get("name") or p.get("user_id"),
                "mbti": p.get("mbti", "-"),
                "memory_summary": p.get("profile_memory_summary", "")
            })
        return {"users": users}
    except Exception as e:
        return {"users": [], "error": str(e)}


@router.get("/api/admin/user-memories/{user_id}")
def get_user_memories_endpoint(user_id: str):
    """Proxy request to Port 9001 matchmaker agent memory endpoint."""
    try:
        resp = requests.get(f"{AGENT_URL}/api/memory/{user_id}", params={"limit": 30}, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        # Fallback to MongoDB preview if agent unavailable
        from database import profiles_coll
        doc = profiles_coll.find_one({"user_id": user_id}, {"profile_memory_preview": 1}) or {}
        return {
            "memories": doc.get("profile_memory_preview", []),
            "fallback": True,
            "error": str(e)
        }


@router.get("/api/admin/stats")
def get_admin_stats():
    """Get statistics for dashboard stat cards."""
    db, db_id, AWQuery = _get_appwrite_db()
    stats = {
        "total_messages": 0,
        "blocked_count": 0,
        "warning_count": 0,
        "memory_count": 0,
        "agent_9001_status": "offline",
        "risk_8001_status": "offline",
    }

    # Check 9001 status
    try:
        r9 = requests.get(f"{AGENT_URL}/api/memory/healthcheck", timeout=2)
        stats["agent_9001_status"] = "online"
    except Exception:
        try:
            r9 = requests.get(f"{AGENT_URL}/api/memory/demo_user", timeout=2)
            if r9.status_code == 200:
                stats["agent_9001_status"] = "online"
        except Exception:
            pass

    # Check 8001 status
    try:
        r8 = requests.get(f"{RISK_URL}/api/v1/risk/state?conversation_id=test&user_id=test", timeout=2)
        if r8.status_code == 200:
            stats["risk_8001_status"] = "online"
    except Exception:
        pass

    if db and db_id:
        try:
            m_res = db.list_documents(db_id, "messages", queries=[AWQuery.limit(100)])
            m_docs = m_res.get("documents", []) if isinstance(m_res, dict) else m_res.documents
            stats["total_messages"] = len(m_docs)
            stats["blocked_count"] = sum(1 for m in m_docs if (m.get("is_blocked") if isinstance(m, dict) else getattr(m, "is_blocked", False)))

            # Count warnings from intervention logs
            try:
                it_res = db.list_documents(db_id, "intervention_logs", queries=[AWQuery.limit(100)])
                it_docs = it_res.get("documents", []) if isinstance(it_res, dict) else it_res.documents
                stats["warning_count"] = len(it_docs)
            except Exception:
                pass
        except Exception as e:
            print(f"[admin] stats query error: {e}")

    if stats["total_messages"] == 0:
        try:
            from database import messages_coll
            m_list = list(messages_coll.find({}, {"is_blocked": 1, "risk_assessment": 1, "sender_id": 1}))
            # Filter out AI assistant messages
            m_list = [m for m in m_list if m.get("sender_id") != "ai_assistant" and "assistant" not in (m.get("sender_id") or "")]
            stats["total_messages"] = len(m_list)
            for m in m_list:
                ra = m.get("risk_assessment") or {}
                lvl = ra.get("risk_level", "")
                if m.get("is_blocked") or lvl == "blocked":
                    stats["blocked_count"] += 1
                elif lvl in ("warning", "restricted"):
                    stats["warning_count"] += 1
        except Exception as mongo_stats_err:
            print(f"[admin] Mongo stats fallback error: {mongo_stats_err}")

    # Count Mongo memories preview
    try:
        from database import profiles_coll
        total_mem = 0
        for doc in profiles_coll.find({}, {"profile_memory_preview": 1}):
            total_mem += len(doc.get("profile_memory_preview", []))
        stats["memory_count"] = total_mem
    except Exception:
        pass

    return stats
