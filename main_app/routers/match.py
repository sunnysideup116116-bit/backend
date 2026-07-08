import time
from fastapi import APIRouter, HTTPException, BackgroundTasks
from models import MatchRequest, AcceptRequest
from database import profiles_coll, matches_coll
from services.ai_service import get_embedding, generate_peer_first_message
from services.chat_service import generate_room_id, save_message
from bson.objectid import ObjectId
from agent_api import do_match, do_feedback, do_global_reflection

router = APIRouter(prefix="/api/match", tags=["Match"])

@router.post("")
def match_endpoint(req: MatchRequest):
    user_doc = profiles_coll.find_one({"user_id": req.user_id}, {"_id": 0})
    if not user_doc:
         raise HTTPException(status_code=400, detail="User context not found.")
         
    user_embedding = user_doc.get("context_embedding", [])
    if not user_embedding:
        ctx = user_doc.get("current_context", "交朋友")
        user_embedding = get_embedding(ctx)
        user_doc["current_context"] = ctx
        profiles_coll.update_one({"user_id": req.user_id}, {"$set": {"context_embedding": user_embedding, "current_context": ctx}})
    
    existing_matches = list(matches_coll.find({"$or": [{"from_user": req.user_id}, {"to_user": req.user_id}]}))
    excluded_users = {req.user_id}
    for m in existing_matches:
        excluded_users.add(m["from_user"])
        excluded_users.add(m["to_user"])
    
    pipeline = [
        {
            "$vectorSearch": {
                "index": "vector_index",
                "path": "context_embedding",
                "queryVector": user_embedding,
                "numCandidates": 50,
                "limit": 20
            }
        },
        {
            "$match": {
                "user_id": { "$nin": list(excluded_users) }
            }
        },
        {
            "$addFields": {
                "score": { "$meta": "vectorSearchScore" }
            }
        },
        {
            "$limit": 5
        },
        {
            "$project": {
                "_id": 0
            }
        }
    ]
    
    try:
        raw_candidates = list(profiles_coll.aggregate(pipeline))
    except Exception as e:
        print(f"Vector search failed: {e}")
        raise HTTPException(status_code=500, detail="Vector search failed. 請確認已在 MongoDB Atlas 建立 vector_index 且具備 context_embedding 欄位。")

    top_5_candidates = []
    for c in raw_candidates:
        score = c.get("score", 0.0)
        top_5_candidates.append((score, c))
    
    if not top_5_candidates:
        raise HTTPException(status_code=404, detail="Not enough candidates.")

    # 將配對決策委派給 9001 港口的 V2 Agent
    clean_candidates = [c[1] if isinstance(c, tuple) else c for c in top_5_candidates]
    
    # 取得 target_user 的 deep_profile
    target_deep_profile = user_doc.get("deep_profile", {})
    
    # 取得每位 candidate 的 deep_profile
    for c in clean_candidates:
        c_doc = profiles_coll.find_one({"user_id": c.get("user_id")}, {"deep_profile": 1, "_id": 0})
        if c_doc and c_doc.get("deep_profile"):
            c["deep_profile"] = c_doc["deep_profile"]
    
    payload = {
        "target_user": user_doc,
        "candidates": clean_candidates,
        "target_deep_profile": target_deep_profile
    }
    
    try:
        print("📞 直接呼叫媒婆 Agent（已整合）...")
        agent_data = do_match(user_doc, clean_candidates, target_deep_profile)
        # 🥚 雙黃蛋：解析 matches 陣列
        agent_matches = agent_data.get("matches", [])
        if not agent_matches:
            raise HTTPException(status_code=500, detail="Agent 未回傳任何配對結果")
        print(f"✅ Agent 回應: {len(agent_matches)} 位候選人")
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ 媒婆 Agent 呼叫失敗: {e}")
        raise HTTPException(status_code=500, detail=f"配對 Agent 呼叫失敗: {e}")
    
    # 🥚 為每位候選人建立 match_doc，初始狀態為 draft
    result_matches = []
    for m in agent_matches:
        matched_id = m.get("matched_user_id")
        reason = m.get("recommendation_reason", "")
        receiver_reason = m.get("receiver_reason", "")
        contrast_label = m.get("contrast_label", "")
        distinctive_tags = m.get("distinctive_tags", [])
        
        if not matched_id:
            continue
        
        match_doc = {
            "from_user": req.user_id,
            "to_user": matched_id,
            "reason": reason,
            "receiver_reason": receiver_reason,
            "contrast_label": contrast_label,
            "distinctive_tags": distinctive_tags,
            "status": "draft",  # 🔑 初始為 draft，發起者接受後才變 pending
            "created_at": time.time()
        }
        insert_result = matches_coll.insert_one(match_doc)
        
        # 查詢候選人的 profile 供前端渲染
        to_doc = profiles_coll.find_one({"user_id": matched_id}, {"_id": 0})
        
        result_matches.append({
            "match_id": str(insert_result.inserted_id),
            "matched_user_id": matched_id,
            "contrast_label": contrast_label,
            "distinctive_tags": distinctive_tags,
            "recommendation_reason": reason,
            "receiver_reason": receiver_reason,
            "big_five": to_doc.get("big_five", {}) if to_doc else {},
            "current_context": to_doc.get("current_context", "") if to_doc else ""
        })
        print(f"  ✅ 建立 draft 配對: {req.user_id} → {matched_id} [{contrast_label}]")
    
    debug_candidates = []
    for score, doc in top_5_candidates:
        debug_candidates.append({
            "user_id": doc.get("user_id"),
            "score": round(score * 100, 2),
            "context": doc.get("current_context"),
            "big_five_summary": doc.get("big_five", {}).get("summary", "")
        })
    
    return {
        "status": "success",
        "matches": result_matches,
        "debug_info": debug_candidates
    }

@router.post("/accept")
def accept_match(req: AcceptRequest, background_tasks: BackgroundTasks):
    match_doc = matches_coll.find_one({"_id": ObjectId(req.match_id)})
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")
    
    current_status = match_doc.get("status")
    from_id = match_doc["from_user"]
    to_id = match_doc["to_user"]
    reason = match_doc.get("reason", "")
    
    # 🔄 狀態機：雙情境路由
    if current_status == "draft" and req.user_id == from_id:
        # 情境 A：發起者確認發送邀請 → draft → pending
        matches_coll.update_one({"_id": ObjectId(req.match_id)}, {"$set": {"status": "pending"}})
        print(f"📤 發起者 {from_id} 確認邀請 {to_id}：draft → pending")
        return {"status": "success", "new_status": "pending"}
    
    elif current_status == "pending" and req.user_id == to_id:
        # 情境 B：接收者互相接受 → pending → accepted
        matches_coll.update_one({"_id": ObjectId(req.match_id)}, {"$set": {"status": "accepted"}})
        print(f"🤝 接收者 {to_id} 接受邀請 {from_id}：pending → accepted")
        
        # ✅ 觸發 AI 破冰訊息
        initiator_doc = profiles_coll.find_one({"user_id": from_id})
        target_doc = profiles_coll.find_one({"user_id": to_id})
        
        def send_first_msg():
            first_msg = generate_peer_first_message(initiator_doc, target_doc, reason)
            room_id = generate_room_id(from_id, to_id)
            save_message(room_id, from_id, first_msg)
            
        background_tasks.add_task(send_first_msg)
        
        # ✅ 觸發全域抽象化反思（配對成功 → 歸納通用法則）
        from_big_five = initiator_doc.get("big_five", {}) if initiator_doc else {}
        from_context = initiator_doc.get("current_context", "") if initiator_doc else ""
        to_big_five = target_doc.get("big_five", {}) if target_doc else {}
        to_context = target_doc.get("current_context", "") if target_doc else ""
        
        def trigger_global_reflection():
            try:
                do_global_reflection(from_big_five, from_context, to_big_five, to_context)
                print("🧠 已觸發全域抽象化反思")
            except Exception as e:
                print(f"⚠️ 觸發全域反思失敗: {e}")
        
        background_tasks.add_task(trigger_global_reflection)
        
        return {"status": "success", "new_status": "accepted"}
    
    else:
        # 無效的狀態轉換
        raise HTTPException(
            status_code=400, 
            detail=f"無效的狀態轉換：目前狀態={current_status}，操作者={req.user_id}（發起者={from_id}，接收者={to_id}）"
        )

@router.post("/decline")
def decline_match(req: AcceptRequest, background_tasks: BackgroundTasks):
    print(f"📥 V1 收到婉拒請求，準備轉發給 Agent: {req.explicit_reasons}")
    match_doc = matches_coll.find_one({"_id": ObjectId(req.match_id)})
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")
    
    current_status = match_doc.get("status")
    from_id = match_doc["from_user"]
    to_id = match_doc["to_user"]
    
    matches_coll.update_one({"_id": ObjectId(req.match_id)}, {"$set": {"status": "declined"}})
    
    # 🔄 狀態機：雙情境路由回饋
    if current_status == "draft" and req.user_id == from_id:
        # 情境 A：發起者婉拒草稿 → 回饋「發起者」的偏好
        to_doc = profiles_coll.find_one({"user_id": to_id})
        target_traits = to_doc.get("big_five", {}) if to_doc else {}
        
        def notify_agent_decline_initiator():
            try:
                print(f"📝 發起者婉拒草稿回饋: user_id={from_id}, target_id={to_id}")
                do_feedback(from_id, to_id, "decline", target_traits, req.explicit_reasons)
                print("📝 已通知 Agent 發起者婉拒回饋")
            except Exception as e:
                print(f"❌ 轉發 Feedback 給 Agent 失敗: {e}")
                print(f"⚠️ 通知 Agent 回饋失敗: {e}")
        
        background_tasks.add_task(notify_agent_decline_initiator)
        print(f"❌ 發起者 {from_id} 婉拒草稿 {to_id}：draft → declined")
        return {"status": "success", "new_status": "declined", "context": "initiator_declined_draft"}
    
    elif current_status == "pending" and req.user_id == to_id:
        # 情境 B：接收者婉拒邀請 → 回饋「接收者」的偏好
        from_doc = profiles_coll.find_one({"user_id": from_id})
        target_traits = from_doc.get("big_five", {}) if from_doc else {}
        
        def notify_agent_decline_receiver():
            try:
                print(f"📝 接收者婉拒邀請回饋: user_id={to_id}, target_id={from_id}")
                do_feedback(to_id, from_id, "decline", target_traits, req.explicit_reasons)
                print("📝 已通知 Agent 接收者婉拒回饋")
            except Exception as e:
                print(f"❌ 轉發 Feedback 給 Agent 失敗: {e}")
                print(f"⚠️ 通知 Agent 回饋失敗: {e}")
        
        background_tasks.add_task(notify_agent_decline_receiver)
        print(f"❌ 接收者 {to_id} 婉拒邀請 {from_id}：pending → declined")
        return {"status": "success", "new_status": "declined", "context": "receiver_declined_pending"}
    
    else:
        # 無效的狀態轉換
        raise HTTPException(
            status_code=400,
            detail=f"無效的狀態轉換：目前狀態={current_status}，操作者={req.user_id}（發起者={from_id}，接收者={to_id}）"
        )
