import random
import requests
import hmac
import ipaddress
import os
import re
import time
import config
from fastapi import APIRouter, Header, HTTPException, Request
from models import ClearRequest, SettingsRequest, MediatorToneRequest, ProfileMemoryActionRequest, ProfileLocationRequest, ModelSettingsRequest
from database import db, profiles_coll, matches_coll, messages_coll
from services.ai_service import get_embedding
from services.profile_projection import safe_recent_context
from services.language_service import normalize_model_text, normalize_zh_tw
from services.ayue_agent.proactive_care import normalize_proactive_frequency, schedule_proactive_care
from services.ayue_agent.v3.scheduler import has_active_public_confirmation
from services.ayue_agent.v3.debug_trace import get_run as get_debug_run, local_debug_enabled
from services.ayue_agent.web_tools import web_enabled
from services.profile_location import normalize_profile_location, safe_profile_location
from services.ayue_agent.public_relationship_projection import anonymize_counterparty_payload
from services.match_reason_service import V4_REASON_VERSION, reason_for_viewer
from services.demo_cleanup_service import DemoCleanupError, clear_all_demo_state, graph_health

router = APIRouter(prefix="/api", tags=["System"])


def _is_loopback_debug_request(request: Request) -> bool:
    if not local_debug_enabled() or request.client is None:
        return False
    try:
        client_is_loopback = ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        client_is_loopback = request.client.host == "localhost"
    hostname = (request.url.hostname or "").lower()
    try:
        host_is_loopback = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        host_is_loopback = hostname == "localhost"
    return client_is_loopback and host_is_loopback


@router.get("/debug/ayue-runs/{run_id}")
def local_ayue_debug_run(run_id: str, user_id: str, request: Request):
    """Return one ephemeral raw V3 diagnostic only to a true loopback client."""
    if not _is_loopback_debug_request(request) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise HTTPException(status_code=404, detail="Debug trace unavailable")
    run = get_debug_run(run_id, user_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Debug trace unavailable")
    return run


@router.get("/client-config")
def client_config():
    """Return browser-safe configuration; Maps browser keys must be referrer-restricted."""
    enabled = bool(
        getattr(config, "AYUE_GOOGLE_PLACE_CARDS_ENABLED", False)
        and getattr(config, "GOOGLE_PLACES_SERVER_API_KEY", "")
        and getattr(config, "GOOGLE_MAPS_BROWSER_API_KEY", "")
    )
    return {
        "google_place_cards_enabled": enabled,
        "google_maps_browser_api_key": str(getattr(config, "GOOGLE_MAPS_BROWSER_API_KEY", "")) if enabled else "",
    }

@router.get("/init")
def init_system(user_id: str):
    profiles = list(profiles_coll.find({}, {"user_id": 1, "big_five": 1, "current_context": 1, "_id": 0}))
    users = ["demo_user"]
    
    is_complete = False
    my_context = "交朋友"
    my_bf_summary = "尚無性格分析資料"
    
    is_deep_complete = False
    my_deep_summary = "尚無深層價值觀分析資料"
    proactive_frequency = "3600"
    mediator_tone = "friend"
    mediator_tone_selected = False
    probe_mode = "balanced"
    profile_memories = []
    context_revision = 0
    match_search = {"status": "idle"}
    onboarding_completed = False
    user_location = {}
    
    for p in profiles:
        uid = p.get("user_id")
        if uid and uid not in users:
            users.append(uid)
        
        if uid == user_id:
            bf = p.get("big_five", {})
            my_context = safe_recent_context(p.get("current_context", ""), "交朋友")
            if bf and len(bf) >= 5:
                is_complete = True
                my_bf_summary = normalize_zh_tw(bf.get("summary", "已完成性格分析，具備基本資料。"))
                
    # 取得深層價值觀分析狀態
    my_dp_values = None
    my_dp_future = None
    my_deep_profile = None
    my_initial_interest = None
    my_doc = profiles_coll.find_one({"user_id": user_id})
    if my_doc:
        dp = my_doc.get("deep_profile", {})
        if dp and dp.get("summary"):
            dp_display = normalize_model_text(dp)
            is_deep_complete = True
            my_deep_summary = normalize_zh_tw(dp_display.get("summary", "已完成深層價值觀分析。"))
            # 提供深層價值觀欄位供前端 dropdown 顯示
            core_vals = dp_display.get("core_values") or dp_display.get("values")
            my_dp_values = "、".join(core_vals) if isinstance(core_vals, list) else (core_vals or None)
            my_dp_future = dp_display.get("life_philosophy", None) or dp_display.get("ideal_future", None)
            # 回傳完整 deep_profile 物件供前端組合顯示
            my_deep_profile = dp_display
        proactive_frequency = normalize_proactive_frequency(my_doc.get("proactive_frequency", "3600"))
        mediator_tone = my_doc.get("mediator_tone", "friend")
        mediator_tone_selected = bool(my_doc.get("mediator_tone_selected", False))
        probe_mode = my_doc.get("probe_mode", "balanced")
        profile_memories = normalize_model_text(my_doc.get("profile_memory_preview", []))
        context_revision = int(my_doc.get("current_context_revision", 0))
        match_search = my_doc.get("match_search", {"status": "idle"})
        onboarding_completed = bool(my_doc.get("onboarding_completed", False))
        my_initial_interest = normalize_zh_tw(my_doc.get("initial_interest"), max_length=120) or None
        user_location = safe_profile_location(my_doc)
                
    return {
        "users": users,
        "is_complete": is_complete,
        "my_context": my_context,
        "my_bf_summary": my_bf_summary,
        "is_deep_complete": is_deep_complete,
        "my_deep_summary": my_deep_summary,
        "my_dp_values": my_dp_values,
        "my_dp_future": my_dp_future,
        "my_deep_profile": my_deep_profile,
        "my_initial_interest": my_initial_interest,
        "proactive_frequency": proactive_frequency,
        "mediator_tone": mediator_tone,
        "mediator_tone_selected": mediator_tone_selected,
        "probe_mode": probe_mode,
        "profile_memories": profile_memories,
        "current_context_revision": context_revision,
        "match_search": match_search,
        "onboarding_completed": onboarding_completed,
        "user_location": user_location,
    }

@router.post("/seed")
def seed_data():
    hobbies = ["想去喝咖啡", "晚上想看電影", "想找人打籃球", "週末想去郊外圖書館看書", "想要去居酒屋小酌"]
    personalities = [
        {"O": 8, "C": 7, "E": 9, "A": 8, "N": 3, "summary": "開朗活潑，喜歡戶外活動，容易親近。"},
        {"O": 5, "C": 9, "E": 4, "A": 6, "N": 4, "summary": "嚴謹踏實，作風穩健，偏好安靜的環境。"},
        {"O": 9, "C": 5, "E": 7, "A": 8, "N": 5, "summary": "充滿好奇心，點子很多，喜歡嘗試新鮮事物。"},
        {"O": 6, "C": 8, "E": 3, "A": 7, "N": 6, "summary": "內斂溫和，做事有條理，是個可靠的傾聽者。"},
        {"O": 7, "C": 6, "E": 8, "A": 5, "N": 4, "summary": "直率果斷，行動力強，喜歡與人交流辯論。"}
    ]
    deep_profiles = [
        {"life_philosophy": "及時行樂，享受每個當下", "attachment_style": "安全型", "decision_style": "直覺型", "core_values": ["自由", "快樂", "冒險"], "summary": "活在當下的樂天派，重視自由與快樂"},
        {"life_philosophy": "穩紮穩打，慢工出細活", "attachment_style": "穩定型", "decision_style": "深思熟慮型", "core_values": ["可靠", "耐心", "誠實"], "summary": "重視承諾與穩定的務實主義者"},
        {"life_philosophy": "世界是一本大書，不旅行的人只讀了一頁", "attachment_style": "探索型", "decision_style": "創意型", "core_values": ["好奇", "成長", "體驗"], "summary": "充滿好奇心的探索者，永遠在尋找新視角"},
        {"life_philosophy": "傾聽是最溫柔的陪伴", "attachment_style": "照顧型", "decision_style": "謹慎型", "core_values": ["同理", "信任", "安穩"], "summary": "溫柔可靠的傾聽者，重視深層連結"},
        {"life_philosophy": "行動是治癒焦慮的最佳良藥", "attachment_style": "獨立型", "decision_style": "果斷型", "core_values": ["效率", "勇氣", "自主"], "summary": "果斷的行動派，相信做就對了"}
    ]
    fake_users = []
    seed_display_names = ["小安", "小晴", "小涵", "小宇", "小葵", "小哲", "小玟", "小凱", "小樂", "小岑"]
    seed_user_pattern = {"$regex": r"^seed_user_"}
    stale_match_query = {
        "$or": [
            {"from_user": seed_user_pattern},
            {"to_user": seed_user_pattern},
        ]
    }
    stale_match_ids = [
        item["_id"] for item in matches_coll.find(stale_match_query, {"_id": 1})
    ]
    if stale_match_ids:
        matches_coll.delete_many({"_id": {"$in": stale_match_ids}})
        profiles_coll.update_many(
            {"active_match_proposal_id": {"$in": stale_match_ids}},
            {"$unset": {"active_match_proposal_id": ""}},
        )
    profiles_coll.delete_many({"user_id": {"$regex": "^seed_user"}})
    for i in range(1, 11):
        uid = f"seed_user_{i:02d}"
        ctx = random.choice(hobbies)
        bf = random.choice(personalities)
        dp = random.choice(deep_profiles)
        fake_users.append({
            "user_id": uid, "display_name": seed_display_names[i - 1], "big_five": bf,
            "deep_profile": dp,
            "current_context": ctx, "context_embedding": get_embedding(ctx),
            "profile_location": normalize_profile_location("高雄市", "鹽埕區") if uid == "seed_user_01" else {},
        })
    profiles_coll.insert_many(fake_users)
    return {
        "status": "success",
        "message": "10 seed profiles created.",
        "stale_seed_matches_removed": len(stale_match_ids),
    }


@router.get("/demo/status")
def get_demo_status(user_id: str):
    """Return a privacy-safe snapshot for the local Demo tools panel."""
    mongo_status = "available"
    try:
        profile = profiles_coll.find_one(
            {"user_id": user_id},
            {
                "_id": 0,
                "current_context": 1,
                "profile_location": 1,
                "match_search.status": 1,
            },
        )
    except Exception:
        profile = None
        mongo_status = "unavailable"
    profile = profile or {}
    search = profile.get("match_search") or {}
    search_status = str(search.get("status") or "idle")[:40]
    return {
        "profile_exists": bool(profile),
        "agent_version": "v3",
        "web_search_ready": web_enabled(),
        "location": safe_profile_location(profile),
        "recent_context": safe_recent_context(profile.get("current_context", ""), "尚無近期情境"),
        "match_search_status": search_status,
        "has_pending_confirmation": has_active_public_confirmation(user_id),
        "graph_status": graph_health()["status"],
        "mongo_status": mongo_status,
    }


@router.get("/profile/recent-context/status")
def get_recent_context_status(user_id: str, run_key: str | None = None):
    """Privacy-safe polling projection for the asynchronous profile process."""
    profile = profiles_coll.find_one(
        {"user_id": user_id},
        {
            "_id": 0, "current_context": 1, "current_context_revision": 1,
            "recent_context_updated_at": 1, "agentic_profile_process": 1,
        },
    ) or {}
    try:
        revision = max(0, int(profile.get("current_context_revision", 0) or 0))
    except (TypeError, ValueError):
        revision = 0
    try:
        updated_at = max(0.0, float(profile.get("recent_context_updated_at", 0) or 0))
    except (TypeError, ValueError):
        updated_at = 0.0
    response = {
        "current_context": safe_recent_context(profile.get("current_context"), ""),
        "revision": revision,
        "updated_at": updated_at,
    }
    token = str(run_key or "").strip().lower()
    if not token:
        return response
    if len(token) != 32 or any(character not in "0123456789abcdef" for character in token):
        response["process"] = {"state": "unavailable", "outcome": None}
        return response
    process = profile.get("agentic_profile_process") or {}
    if not isinstance(process, dict) or process.get("run_key") != token:
        response["process"] = {"state": "superseded", "outcome": None}
        return response
    state = str(process.get("state") or "queued")
    try:
        expires_at = float(process.get("expires_at", 0) or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    if state not in {"queued", "processing", "completed"}:
        state = "unavailable"
    elif state != "completed" and expires_at and expires_at <= time.time():
        state = "timeout"
    outcome = str(process.get("outcome") or "") if state == "completed" else ""
    response["process"] = {
        "state": state,
        "outcome": outcome if outcome in {"updated", "no_update", "error"} else None,
    }
    return response

@router.post("/clear")
def clear_data(req: ClearRequest):
    try:
        return clear_all_demo_state()
    except DemoCleanupError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc

@router.get("/notifications")
def get_notifications(user_id: str):
    """查詢指定 user 的待回應配對邀請（pending）。"""
    # Keep notification text on the same role-bound projection as proposal
    # cards and mediator delivery.  A V4 invitation never reconstructs an
    # explanation from the other party's profile or old reason fields.
    pending = list(matches_coll.find({
        "to_user": user_id,
        "status": "pending",
        "delivery_channel": {"$ne": "mediator_chat"}
    }))
    results = []
    for p in pending:
        viewer_reason = reason_for_viewer(p, user_id)
        if p.get("reason_version") == V4_REASON_VERSION:
            results.append({
                "match_id": str(p["_id"]),
                "viewer_reason": viewer_reason,
                # Compatibility aliases contain the same receiver projection;
                # they do not expose the initiator preview.
                "reason": viewer_reason,
                "receiver_reason": viewer_reason,
                "reason_version": V4_REASON_VERSION,
            })
            continue
        # 查詢發起人的完整 profile，取得 big_five 與 context 供前端渲染 Checkbox
        from_doc = profiles_coll.find_one({"user_id": p["from_user"]}, {"_id": 0})
        from_name = str((from_doc or {}).get("display_name") or (from_doc or {}).get("nickname") or (from_doc or {}).get("name") or "").strip()
        from_big_five = anonymize_counterparty_payload(
            from_doc.get("big_five", {}), p["from_user"], counterparty_name=from_name,
        ) if from_doc else {}
        from_context = anonymize_counterparty_payload(
            from_doc.get("current_context", ""), p["from_user"], counterparty_name=from_name,
        ) if from_doc else ""
        from_distinctive_tags = anonymize_counterparty_payload(
            from_doc.get("distinctive_tags", []), p["from_user"], counterparty_name=from_name,
        ) if from_doc else []
        results.append({
            "match_id": str(p["_id"]),
            "from_user": p["from_user"],
            "reason": anonymize_counterparty_payload(
                p["reason"], p["from_user"], counterparty_name=from_name,
            ),
            "receiver_reason": anonymize_counterparty_payload(
                p.get("receiver_reason", p.get("reason", "")), p["from_user"],
                counterparty_name=from_name,
            ),
            "from_user_big_five": from_big_five,
            "from_user_context": from_context,
            "from_user_distinctive_tags": from_distinctive_tags
        })
    return {"notifications": results}

@router.post("/settings")
def update_settings(req: SettingsRequest):
    """更新使用者設定（如主動配對頻率）"""
    proactive_frequency = normalize_proactive_frequency(req.proactive_frequency)
    existing = profiles_coll.find_one({"user_id": req.user_id}, {"last_user_activity_at": 1}) or {}
    schedule_proactive_care(
        req.user_id, proactive_frequency,
        last_activity=float(existing.get("last_user_activity_at", 0) or 0),
    )
    return {"status": "success", "proactive_frequency": proactive_frequency}

@router.patch("/profile/location")
def update_profile_location(req: ProfileLocationRequest):
    location = normalize_profile_location(req.city, req.district)
    profiles_coll.update_one(
        {"user_id": req.user_id},
        {"$set": {"profile_location": location}},
        upsert=True,
    )
    return {"status": "success", "location": location}

@router.post("/settings/mediator")
def update_mediator_tone(req: MediatorToneRequest):
    allowed = {"friend", "gentle", "enthusiastic"}
    tone = req.mediator_tone if req.mediator_tone in allowed else "friend"
    update = {"mediator_tone": tone, "mediator_tone_selected": True}
    if req.probe_mode in {"balanced", "active", "manual"}:
        update["probe_mode"] = req.probe_mode
    profiles_coll.update_one({"user_id": req.user_id}, {"$set": update}, upsert=True)
    return {"status": "success", "mediator_tone": tone, "probe_mode": update.get("probe_mode")}

@router.post("/settings/model")
def update_model_settings(
    req: ModelSettingsRequest,
    x_ayue_admin_token: str | None = Header(default=None),
):
    """Admin-only process-wide override for local evaluation."""
    from services.ai_service import set_runtime_model_override, get_runtime_model_override
    admin_token = os.getenv("AYUE_RUNTIME_MODEL_SETTINGS_TOKEN", "").strip()
    if not admin_token or not x_ayue_admin_token or not hmac.compare_digest(admin_token, x_ayue_admin_token):
        raise HTTPException(status_code=403, detail="Runtime model settings are disabled.")
    allowed_models = {
        value.strip()
        for value in os.getenv("AYUE_ALLOWED_RUNTIME_MODELS", config.OLLAMA_CHAT_MODEL).split(",")
        if value.strip()
    }
    if req.model is not None and req.model not in allowed_models:
        raise HTTPException(status_code=400, detail="Model is not allowlisted.")
    set_runtime_model_override(req.model, req.thinking_level)
    return {"status": "success", **get_runtime_model_override()}

@router.get("/settings/model")
def get_model_settings():
    """Return non-sensitive model state; mutation remains admin-only."""
    from services.ai_service import get_runtime_model_override
    return {**get_runtime_model_override(), "mutable": False}

@router.post("/onboarding/complete")
def complete_onboarding(req: ClearRequest):
    profiles_coll.update_one(
        {"user_id": req.user_id},
        {"$set": {"onboarding_completed": True}},
        upsert=True
    )
    return {"status": "success", "onboarding_completed": True}

@router.post("/context/undo")
def undo_recent_context(req: ClearRequest):
    doc = profiles_coll.find_one({"user_id": req.user_id}) or {}
    previous = doc.get("previous_context")
    if not previous:
        return {"status": "nothing_to_undo", "current_context": doc.get("current_context")}
    current = doc.get("current_context", "")
    profiles_coll.update_one({"user_id": req.user_id}, {"$set": {
        "current_context": previous, "previous_context": current,
        "current_context_revision": int(doc.get("current_context_revision", 0)) + 1,
        "context_embedding": get_embedding(previous)}})
    return {"status": "success", "current_context": previous}

@router.get("/profile/memories")
def get_profile_memories(user_id: str):
    doc = profiles_coll.find_one({"user_id": user_id}, {"profile_memory_preview": 1, "profile_memory_summary": 1}) or {}
    return {"memories": doc.get("profile_memory_preview", []), "summary": doc.get("profile_memory_summary", "")}

@router.post("/profile/memories/action")
def profile_memory_action(req: ProfileMemoryActionRequest):
    from services.memory_service import apply_memory_action
    try:
        return apply_memory_action(req.user_id, req.key, req.action, req.value)
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

@router.get("/debug/profile_skill_runs")
def debug_profile_skill_runs(user_id: str, limit: int = 12):
    safe_limit = max(1, min(limit, 30))
    runs = list(db["profile_skill_runs"].find({"user_id": user_id}, {"_id": 0}).sort("created_at", -1).limit(safe_limit))
    return {"runs": runs}
@router.get("/debug/profile_state")
def debug_profile_state(user_id: str):
    """Development-only snapshot for tracing Ayue's event and memory state."""
    profile = profiles_coll.find_one(
        {"user_id": user_id},
        {
            "_id": 0,
            "user_id": 1,
            "current_context": 1,
            "current_context_revision": 1,
            "context_signals": 1,
            "last_conversation_intent": 1,
            "match_readiness_score": 1,
            "match_readiness_reason": 1,
            "match_search": 1,
            "matchmaking_in_progress": 1,
            "active_match_proposal_id": 1,
            "mediator_inbox": 1,
            "profile_memory_preview": 1,
            "profile_memory_summary": 1,
            "memory_notices": 1,
            "pending_private_feedback": 1,
            "pending_date_coordination": 1,
            "proactive_frequency": 1,
            "mediator_tone": 1,
            "probe_mode": 1,
            "last_user_activity_at": 1,
            "last_followup_activity_at": 1,
            "last_auto_match_revision": 1,
        },
    ) or {"user_id": user_id}
    active_matches = list(
        matches_coll.find(
            {
                "$or": [{"from_user": user_id}, {"to_user": user_id}],
                "status": {"$in": ["draft", "pending", "accepted"]},
            },
            {
                "from_user": 1,
                "to_user": 1,
                "status": 1,
                "delivery_channel": 1,
                "created_at": 1,
                "reason": 1,
                "receiver_reason": 1,
                "distinctive_tags": 1,
                "mediator_state": 1,
                "private_feedback": 1,
                "date_coordination": 1,
            },
        )
    )
    for match in active_matches:
        match["_id"] = str(match["_id"])
    inbox = profile.get("mediator_inbox") or []
    notices = profile.get("memory_notices") or []
    return {
        "profile": profile,
        "mediator_inbox_count": len(inbox),
        "memory_notice_count": len(notices),
        "active_matches": active_matches,
    }

@router.post("/reset_deep_profile")
def reset_deep_profile(req: ClearRequest):
    """重置深層價值觀分析，讓使用者可以重新分析"""
    profiles_coll.update_one(
        {"user_id": req.user_id},
        {"$unset": {"deep_profile": "", "dp_interaction_count": ""}}
    )
    return {"status": "success"}


