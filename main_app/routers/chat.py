from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import asyncio
import json
from models import ChatRequest, DirectChatRequest, ResetRequest, RiskFeedbackRequest
from database import profiles_coll, messages_coll, matches_coll
from services.ai_service import (
    analyze_big_five, 
    analyze_deep_profile, 
    get_embedding, 
    generate_chat_completion, 
    generate_chat_completion_stream,
    summarize_context
)
from services.chat_service import generate_room_id, save_message
from services.guidance_service import track_message_in_buffer
from services.idle_service import update_activity, check_boundary_guard
from services.provenance_service import build_provenance_and_dto
from services import risk_client
from services.appwrite_service import appwrite_srv
from services.delivery_planner import plan_delivery, WARNING_MSG

router = APIRouter(prefix="/api", tags=["Chat"])


@router.post("/chat")
def chat_endpoint(req: ChatRequest):
    if req.state == "big_five":
        user_doc = profiles_coll.find_one({"user_id": req.user_id})
        prev_big_five = user_doc.get("temp_big_five", {}) if user_doc else {}
        interaction_count = user_doc.get("interaction_count", 0) if user_doc else 0
        
        result = analyze_big_five(req.message, prev_big_five, interaction_count, req.initial_interest)
        
        update_fields = {
            "temp_big_five": result.get("big_five", {}),
            "interaction_count": interaction_count + 1
        }
        
        if result.get("is_complete", False):
            update_fields["big_five"] = result.get("big_five", {})
            room_id = generate_room_id(req.user_id, "ai_assistant")
            count = messages_coll.count_documents({"room_id": room_id})
            if count == 0:
                save_message(room_id, "ai_assistant", "你的性格分析已經完成囉！🥳 那你最近有沒有什麼想做的事情？想去哪裡玩呢？")

        profiles_coll.update_one(
            {"user_id": req.user_id}, 
            {"$set": update_fields}, 
            upsert=True
        )

        return {
            "status": "success", 
            "big_five": result.get("big_five"), 
            "reply": result.get("reply"),
            "is_complete": result.get("is_complete", False)
        }
    elif req.state == "deep_profile":
        user_doc = profiles_coll.find_one({"user_id": req.user_id})
        prev_deep = user_doc.get("temp_deep_profile", {}) if user_doc else {}
        interaction_count = user_doc.get("interaction_count_deep", 0) if user_doc else 0
        big_five = user_doc.get("big_five", {}) if user_doc else {}
        current_context = user_doc.get("current_context", "") if user_doc else ""
        
        user_context = {"big_five": big_five, "current_context": current_context}
        
        result = analyze_deep_profile(req.message, prev_deep, interaction_count, user_context)
        
        update_fields = {
            "temp_deep_profile": result.get("deep_profile", {}),
            "interaction_count_deep": interaction_count + 1
        }
        
        if result.get("is_complete", False):
            update_fields["deep_profile"] = result.get("deep_profile", {})
            room_id = generate_room_id(req.user_id, "ai_assistant")
            count = messages_coll.count_documents({"room_id": room_id})
            if count == 0:
                save_message(room_id, "ai_assistant", "你的性格與價值觀分析都已經完成囉！🥳 那你最近有沒有什麼想做的事情？想去哪裡玩呢？")

        profiles_coll.update_one(
            {"user_id": req.user_id}, 
            {"$set": update_fields}, 
            upsert=True
        )

        return {
            "status": "success", 
            "deep_profile": result.get("deep_profile"), 
            "reply": result.get("reply"),
            "is_complete": result.get("is_complete", False)
        }
    else:
        raise HTTPException(status_code=400, detail="Invalid state")

@router.post("/chat/reset")
def reset_chat_state(req: ResetRequest):
    if req.state == "big_five":
        profiles_coll.update_one(
            {"user_id": req.user_id},
            {"$set": {"interaction_count": 0, "temp_big_five": {}}}
        )
    elif req.state == "deep_profile":
        profiles_coll.update_one(
            {"user_id": req.user_id},
            {"$set": {"interaction_count_deep": 0, "temp_deep_profile": {}}}
        )
    return {"status": "success"}

@router.get("/messages/{contact_id}")
def get_messages(contact_id: str, user_id: str):
    room_id = generate_room_id(user_id, contact_id)
    
    if contact_id == "ai_assistant":
        count = messages_coll.count_documents({"room_id": room_id})
        if count == 0:
            save_message(room_id, "ai_assistant", "哈囉！👋 歡迎來到 MatchApp。最近有沒有什麼特別想做的事情？或者是想去哪裡玩呢？跟我分享一下，我來幫你找個好夥伴！")
            
    msgs = list(messages_coll.find({"room_id": room_id}, {"_id": 0}).sort("timestamp", 1))
    return {"messages": msgs}

@router.post("/direct_chat")
def direct_chat(req: DirectChatRequest, background_tasks: BackgroundTasks):
    room_id = generate_room_id(req.user_id, req.contact_id)
    
    # === BEGIN risk detection integration ===
    risk_assessment = risk_client.check_risk(
        conversation_id=room_id,
        sender_id=req.user_id,
        receiver_id=req.contact_id,
        content=req.message,
    )
    if risk_client.is_blocked(risk_assessment):
        return {
            "reply": None,
            "is_blocked": True,
            "ui_priority": "risk",
            "risk_assessment": risk_assessment,
        }
    # === END risk detection integration ===
    
    # Update activity and get welcome_back_draft if returning from idle
    welcome_back_draft = update_activity(room_id, req.user_id)
    
    new_msg = save_message(room_id, req.user_id, req.message)
    
    # Track user message in Agent 1 buffer
    history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", 1)
    full_history = list(history_cursor)
    track_message_in_buffer(room_id, new_msg, full_history, background_tasks)
    
    boundary_warning = check_boundary_guard(req.message)
    if boundary_warning:
        new_ai_msg = save_message(room_id, "ai_assistant", boundary_warning)
        history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", 1)
        full_history = list(history_cursor)
        track_message_in_buffer(room_id, new_ai_msg, full_history, background_tasks)
        
        build_provenance_and_dto(
            room_id=room_id,
            nudge_type="boundary_refusal",
            nudge_text=boundary_warning,
            var_role="SYSTEM",
            var_strategy="邊界強制執行",
            var_fact="None",
            var_graph_edge="None",
            var_model="system",
            var_t_invoke_ms=50.0
        )
        
        res_data = {"reply": boundary_warning}
        if req.contact_id == "ai_assistant":
            res_data["is_locked"] = False
        if welcome_back_draft:
            res_data["welcome_back_draft"] = welcome_back_draft
        risk_client.attach_to_response(res_data, risk_assessment)
        return res_data
    
    if req.contact_id == "ai_assistant":
        history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", -1).limit(20)
        history = list(history_cursor)[::-1]
        
        user_doc = profiles_coll.find_one({"user_id": req.user_id})
        bf = user_doc.get("big_five", {}) if user_doc else {}
        interaction_count = user_doc.get("ai_chat_interaction_count", 0) if user_doc else 0
        
        current_context = user_doc.get("current_context", "無") if user_doc else "無"
        current_round = interaction_count + 1
        
        if current_round < 3:
            round_instruction = f"""【極度重要】目前是第 {current_round} 輪對話。你「必須」給予共鳴並自然地拋出簡短的問題往下聊。絕對不允許結束話題。
【強制要求】你回傳的 JSON 中，"is_context_updated" 必須是 false，"new_context" 必須是 null。"""
        else:
            round_instruction = f"""【極度重要】目前達到第 {current_round} 輪對話。不管使用者回答什麼，你「必須」直接「結束這個話題」，給予一句簡短的結語，絕對不允許以任何形式再丟出問號或問題！
【強制要求】你回傳的 JSON 中，"is_context_updated" 必須是 true，並在 "new_context" 精準總結這幾輪對話得出的「使用者最新狀態或近期興趣」。"""

        sys_prompt = f"""
你是溫暖的 AI 小助手。你正在與使用者閒聊，關心他的近況。
你的目標是透過簡短的自然對話，了解使用者最近「想做的事」或「目前的最新動態」。
【背景資訊】
- 使用者的性格特質：{bf.get('summary', '未知')}
- 使用者上次紀錄的情境：{current_context}

對話守則：
1. 每次回覆請盡量簡短（1~2句話以內）。
2. ⚠️切換話題：你必須【非常仔細看使用者最後說了什麼】！如果使用者在最後一句話提到了全新的計畫或動態（例如：我要去某個地方），請「立刻」順著他的新話題給予強烈共鳴與追問，【絕對不要】再回頭提背景資訊裡的舊情境（例如蛋塔、咖啡等）。
3. ⚠️注意時態與事實：如果使用者表示事情「已經發生」，請如實記錄（例如「剛去過福岡玩」），絕對不能寫成「想去...」。

{round_instruction}

請嚴格回傳以下 JSON 格式：
{{
    "reply": "你給使用者的回覆 (繁體中文)",
    "is_context_updated": true/false,
    "new_context": "濃縮後的簡短情境 / null"
}}
"""
        prompt = sys_prompt + "\n\n【對話紀錄】\n"
        for m in history:
            speaker = "使用者" if m["sender_id"] == req.user_id else "AI小助手"
            prompt += f"{speaker}: {m['content']}\n"
        
        prompt += "\n請身為「AI小助手」，針對對話紀錄中「使用者」的【最後一句話】，給出你的回覆："
            
        # Add the current message which is already in history because of save_message above
        
        try:
            import json
            ai_res_str = generate_chat_completion(prompt, temperature=0.6, json_output=True)
            ai_res = json.loads(ai_res_str)
            ai_reply = ai_res.get("reply", "收到！")
            
            is_locked = False
            
            # Programmatically enforce round limit
            if current_round < 3:
                ai_res["is_context_updated"] = False
                ai_res["new_context"] = None

            if ai_res.get("is_context_updated") and ai_res.get("new_context"):
                new_ctx = ai_res.get("new_context")
                is_locked = True
                profiles_coll.update_one(
                    {"user_id": req.user_id},
                    {"$set": {"current_context": new_ctx, "ai_chat_locked": True, "ai_chat_interaction_count": 0}},
                    upsert=True
                )
                try:
                    context_embedding = get_embedding(new_ctx)
                    profiles_coll.update_one(
                        {"user_id": req.user_id},
                        {"$set": {"context_embedding": context_embedding}},
                        upsert=True
                    )
                except HTTPException as e:
                    print(f"Embedding skipped in direct_chat: {e.detail}")
            else:
                profiles_coll.update_one(
                    {"user_id": req.user_id},
                    {"$inc": {"ai_chat_interaction_count": 1}},
                    upsert=True
                )
                    
        except Exception as e:
            print(f"Chat error (AI): {e}")
            ai_reply = "不好意思，我現在有點短路，請稍後再試！"
            is_locked = False
            
        new_ai_msg = save_message(room_id, "ai_assistant", ai_reply)
        history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", 1)
        full_history = list(history_cursor)
        track_message_in_buffer(room_id, new_ai_msg, full_history, background_tasks)
        
        res_data = {"reply": ai_reply, "is_locked": is_locked}
        if welcome_back_draft:
            res_data["welcome_back_draft"] = welcome_back_draft
        risk_client.attach_to_response(res_data, risk_assessment)
        return res_data
        
    else:
        target_doc = profiles_coll.find_one({"user_id": req.contact_id})
        target_bf = target_doc.get("big_five", {}) if target_doc else {}
        
        history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", -1).limit(20)
        history = list(history_cursor)[::-1]
        
        sys_prompt = f"你現在扮演使用者 {req.contact_id}。你的大五人格設定為：{target_bf}。你要跟一位配對到的用戶聊天，請用符合你性格的方式回答他的話（繁體中文），絕不能透露你是AI或語言模型。"
        prompt = sys_prompt + "\n\n【對話紀錄】\n"
        for m in history:
            speaker = "對方" if m["sender_id"] == req.user_id else "你"
            prompt += f"{speaker}: {m['content']}\n"
        prompt += "你:"
            
        try:
            reply = generate_chat_completion(prompt, temperature=0.7, json_output=False)
        except Exception as e:
            print(f"Chat error (User {req.contact_id}): {e}")
            reply = "不好意思，我現在有點短路，請稍後再試！"
        new_ai_msg = save_message(room_id, req.contact_id, reply)
        
        # Track AI message in buffer as well
        history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", 1)
        full_history = list(history_cursor)
        track_message_in_buffer(room_id, new_ai_msg, full_history, background_tasks)
        
        res_data = {"reply": reply}
        if welcome_back_draft:
            res_data["welcome_back_draft"] = welcome_back_draft
        risk_client.attach_to_response(res_data, risk_assessment)
        return res_data

@router.post("/direct_chat_stream")
def direct_chat_stream(req: DirectChatRequest, background_tasks: BackgroundTasks):
    room_id = generate_room_id(req.user_id, req.contact_id)
    
    # === BEGIN risk detection integration ===
    risk_assessment = risk_client.check_risk(
        conversation_id=room_id,
        sender_id=req.user_id,
        receiver_id=req.contact_id,
        content=req.message,
    )
    if risk_client.is_blocked(risk_assessment):
        async def blocked_generator():
            warning_msg = "⚠️ 偵測到敏感內容，訊息已遭系統安全攔截。"
            yield "data: " + json.dumps({"type": "content", "content": warning_msg}) + "\n\n"
            save_message(room_id, "ai_assistant", warning_msg)
            yield "data: " + json.dumps({"type": "meta", "is_locked": True, "risk_assessment": risk_assessment, "ui_priority": "risk"}) + "\n\n"
        return StreamingResponse(blocked_generator(), media_type="text/event-stream")
    # === END risk detection integration ===
    
    welcome_back_draft = update_activity(room_id, req.user_id)
    new_msg = save_message(room_id, req.user_id, req.message)
    
    # Track user message in Agent 1 buffer
    history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", 1)
    full_history = list(history_cursor)
    track_message_in_buffer(room_id, new_msg, full_history, background_tasks)
    
    boundary_warning = check_boundary_guard(req.message)
    if boundary_warning:
        async def boundary_generator():
            yield "data: " + json.dumps({"type": "content", "content": boundary_warning}) + "\n\n"
            new_ai_msg = save_message(room_id, "ai_assistant", boundary_warning)
            history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", 1)
            full_history = list(history_cursor)
            track_message_in_buffer(room_id, new_ai_msg, full_history, background_tasks)
            build_provenance_and_dto(
                room_id=room_id,
                nudge_type="boundary_refusal",
                nudge_text=boundary_warning,
                var_role="SYSTEM",
                var_strategy="邊界強制執行",
                var_fact="None",
                var_graph_edge="None",
                var_model="system",
                var_t_invoke_ms=50.0
            )
            yield "data: " + json.dumps({
                "type": "meta", 
                "is_locked": False, 
                "welcome_back_draft": welcome_back_draft,
                "risk_assessment": risk_assessment,
                "ui_priority": "risk" if risk_client.should_show_risk_ui(risk_assessment.get("risk_level") if risk_assessment else "") else "coach"
            }) + "\n\n"
        return StreamingResponse(boundary_generator(), media_type="text/event-stream")
        
    if req.contact_id == "ai_assistant":
        history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", -1).limit(20)
        history = list(history_cursor)[::-1]
        
        user_doc = profiles_coll.find_one({"user_id": req.user_id})
        bf = user_doc.get("big_five", {}) if user_doc else {}
        interaction_count = user_doc.get("ai_chat_interaction_count", 0) if user_doc else 0
        
        current_context = user_doc.get("current_context", "無") if user_doc else "無"
        current_round = interaction_count + 1
        
        if current_round < 3:
            round_instruction = f"""【極度重要】目前是第 {current_round} 輪對話。你「必須」給予共鳴並自然地拋出簡短的問題往下聊。絕對不允許結束話題。
【強制要求】絕對不允許以任何形式拋出最後總結性的言詞，要多發問。"""
        else:
            round_instruction = f"""【極度重要】目前達到第 {current_round} 輪對話。不管使用者回答什麼，你「必須」直接「結束這個話題」，給予一句簡短的結語，絕對不允許以任何形式再丟出問號或問題！"""

        sys_prompt = f"""
你是溫暖的 AI 小助手。你正在與使用者閒聊，關心他的近況。
你的目標是透過簡短的自然對話，了解使用者最近「想做的事」或「目前的最新動態」。
【背景資訊】
- 使用者的性格特質：{bf.get('summary', '未知')}
- 使用者上次紀錄的情境：{current_context}

對話守則：
1. 每次回覆請盡量簡短（1~2句話以內）。
2. ⚠️切換話題：你必須【非常仔細看使用者最後說了什麼】！如果使用者在最後一句話提到了全新的計畫或動態（例如：我要去某個地方），請「立刻」順著他的新話題給予強烈共鳴與追問，【絕對不要】再回頭提背景資訊裡的舊情境。
3. ⚠️注意時態與事實：如果使用者表示事情「已經發生」，請如實記錄，絕對不能寫成「想去...」。

{round_instruction}

【極度重要限制】：請直接給出你的回覆內容（繁體中文），「不要」輸出任何 JSON 格式，也不要包裝在引號中。
"""
        prompt = sys_prompt + "\n\n【對話紀錄】\n"
        for m in history:
            speaker = "使用者" if m["sender_id"] == req.user_id else "AI小助手"
            prompt += f"{speaker}: {m['content']}\n"
        
        prompt += "\n請身為「AI小助手」，針對對話紀錄中「使用者」的【最後一句話】，給出你的回覆："
        
        async def assistant_generator():
            full_reply = ""
            try:
                stream = generate_chat_completion_stream(prompt, temperature=0.6)
                for chunk in stream:
                    full_reply += chunk
                    yield "data: " + json.dumps({"type": "content", "content": chunk}) + "\n\n"
                    await asyncio.sleep(0.01)
            except Exception as e:
                print(f"Chat error (AI Assistant Stream): {e}")
                err_msg = "不好意思，我現在有點短路，請稍後再試！"
                full_reply = err_msg
                yield "data: " + json.dumps({"type": "content", "content": err_msg}) + "\n\n"
            
            is_locked = False
            if current_round >= 3:
                is_locked = True
                try:
                    new_ctx = summarize_context(req.message, current_context)
                    profiles_coll.update_one(
                        {"user_id": req.user_id},
                        {"$set": {"current_context": new_ctx, "ai_chat_locked": True, "ai_chat_interaction_count": 0}},
                        upsert=True
                    )
                    try:
                        context_embedding = get_embedding(new_ctx)
                        profiles_coll.update_one(
                            {"user_id": req.user_id},
                            {"$set": {"context_embedding": context_embedding}},
                            upsert=True
                        )
                    except Exception as emb_e:
                        print(f"Embedding error in stream post-processing: {emb_e}")
                except Exception as sum_e:
                    print(f"Summarize context error in stream post-processing: {sum_e}")
            else:
                profiles_coll.update_one(
                    {"user_id": req.user_id},
                    {"$inc": {"ai_chat_interaction_count": 1}},
                    upsert=True
                )
                
            new_ai_msg = save_message(room_id, "ai_assistant", full_reply.strip())
            history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", 1)
            full_history = list(history_cursor)
            track_message_in_buffer(room_id, new_ai_msg, full_history, background_tasks)
            
            yield "data: " + json.dumps({
                "type": "meta", 
                "is_locked": is_locked, 
                "welcome_back_draft": welcome_back_draft,
                "risk_assessment": risk_assessment,
                "ui_priority": "risk" if risk_client.should_show_risk_ui(risk_assessment.get("risk_level") if risk_assessment else "") else "coach"
            }) + "\n\n"
            
        return StreamingResponse(assistant_generator(), media_type="text/event-stream")
        
    else:
        target_doc = profiles_coll.find_one({"user_id": req.contact_id})
        target_bf = target_doc.get("big_five", {}) if target_doc else {}
        
        history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", -1).limit(20)
        history = list(history_cursor)[::-1]
        
        sys_prompt = f"你現在扮演使用者 {req.contact_id}。你的大五人格設定為：{target_bf}。你要跟一位配對到的用戶聊天，請用符合你性格的方式回答他的話（繁體中文），絕不能透露你是AI或語言模型。"
        prompt = sys_prompt + "\n\n【對話紀錄】\n"
        for m in history:
            speaker = "對方" if m["sender_id"] == req.user_id else "你"
            prompt += f"{speaker}: {m['content']}\n"
        prompt += "你:"
        
        async def peer_generator():
            full_reply = ""
            try:
                stream = generate_chat_completion_stream(prompt, temperature=0.7)
                for chunk in stream:
                    full_reply += chunk
                    yield "data: " + json.dumps({"type": "content", "content": chunk}) + "\n\n"
                    await asyncio.sleep(0.01)
            except Exception as e:
                print(f"Chat error (Peer Stream): {e}")
                err_msg = "不好意思，我現在有點短路，請稍後再試！"
                full_reply = err_msg
                yield "data: " + json.dumps({"type": "content", "content": err_msg}) + "\n\n"
                
            new_ai_msg = save_message(room_id, req.contact_id, full_reply.strip())
            history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", 1)
            full_history = list(history_cursor)
            track_message_in_buffer(room_id, new_ai_msg, full_history, background_tasks)
            
            yield "data: " + json.dumps({
                "type": "meta", 
                "is_locked": False, 
                "welcome_back_draft": welcome_back_draft,
                "risk_assessment": risk_assessment,
                "ui_priority": "risk" if risk_client.should_show_risk_ui(risk_assessment.get("risk_level") if risk_assessment else "") else "coach"
            }) + "\n\n"
            
        return StreamingResponse(peer_generator(), media_type="text/event-stream")

@router.get("/contacts")
def get_contacts(user_id: str):
    user_doc = profiles_coll.find_one({"user_id": user_id})
    ai_locked = user_doc.get("ai_chat_locked", False) if user_doc else False
    
    query = {
        "status": "accepted",
        "$or": [{"from_user": user_id}, {"to_user": user_id}]
    }
    matches = list(matches_coll.find(query))
    
    contacts = [
        {"id": "ai_assistant", "name": "AI 小助手", "role": "system", "context": "幫助您分析性格與配對", "is_locked": ai_locked}
    ]
    
    for m in matches:
        other_id = m["to_user"] if m["from_user"] == user_id else m["from_user"]
        other_doc = profiles_coll.find_one({"user_id": other_id})
        ctx = other_doc.get("current_context", "交朋友") if other_doc else "交朋友"
        contacts.append({
            "id": other_id,
            "name": other_id, 
            "role": "user",
            "context": ctx
        })
        
    return {"contacts": contacts}

import time
@router.get("/proactive_check")
def proactive_check(user_id: str):
    user_doc = profiles_coll.find_one({"user_id": user_id})
    if not user_doc:
        return {"has_new": False}
        
    freq_str = str(user_doc.get("proactive_frequency", "none"))
    if freq_str == "none":
        return {"has_new": False}
        
    try:
        freq_seconds = int(freq_str)
    except ValueError:
        return {"has_new": False}
        
    last_time = user_doc.get("last_proactive_time", 0)
    current_time = time.time()
    
    if current_time - last_time >= freq_seconds:
        # Time to send a proactive message
        # Update time first to prevent duplicate triggers
        profiles_coll.update_one({"user_id": user_id}, {"$set": {"last_proactive_time": current_time}})
        
        bf = user_doc.get("big_five", {})
        ctx = user_doc.get("current_context", "無特別情境")
        
        prompt = f"""
你是溫暖的 AI 小助手。這是你主動發起的對話，用來關心使用者最近的狀況。
使用者的性格特質是：{bf.get('summary', '未知')}
使用者上次紀錄的情境/興趣是：{ctx}

請用繁體中文，大約 2~3 句話，根據上述資訊主動開話題關心對方。
【極度重要】：你必須針對他「上次的情境」進行自然的「後續追問」（例如：上次聽說你想去非洲，後來去成了嗎？有沒有看到大象？）。如果沒有特別情境，就隨機找個輕鬆的話題閒聊。語氣自然、像朋友一樣。
"""
        try:
            ai_reply = generate_chat_completion(prompt, temperature=0.7, json_output=False)
            room_id = generate_room_id(user_id, "ai_assistant")
            save_message(room_id, "ai_assistant", ai_reply)
            profiles_coll.update_one({"user_id": user_id}, {"$set": {"ai_chat_locked": False, "ai_chat_interaction_count": 0}})
            return {"has_new": True, "message": ai_reply}
        except Exception as e:
            print(f"Proactive chat error: {e}")
            return {"has_new": False}
            
    return {"has_new": False}


@router.post("/risk/feedback")
def submit_risk_feedback(req: RiskFeedbackRequest):
    success = risk_client.submit_feedback(
        triggered_by_msg_id=req.triggered_by_msg_id,
        role=req.role,
        feedback=req.feedback
    )
    return {"status": "success" if success else "failed"}


class HumanChatRequest(BaseModel):
    sender_id: str
    receiver_id: str
    message: str

@router.get("/human_chat/state")
def get_human_chat_state(sender_id: str, receiver_id: str):
    room_id = generate_room_id(sender_id, receiver_id)
    state = risk_client.get_risk_state(room_id, sender_id)
    if not state:
        return {
            "risk_level": "safe",
            "risk_state": {
                "sexual_boundary": 0.0,
                "coercion": 0.0,
                "manipulation": 0.0,
                "harassment": 0.0,
                "emotional_pressure": 0.0
            }
        }
    return state

@router.post("/human_chat")
def human_chat(req: HumanChatRequest, background_tasks: BackgroundTasks):
    room_id = generate_room_id(req.sender_id, req.receiver_id)

    # 1. Check risk with risk_backend
    risk_assessment = risk_client.check_risk(
        conversation_id=room_id,
        sender_id=req.sender_id,
        receiver_id=req.receiver_id,
        content=req.message,
    )
    risk_level = (risk_assessment or {}).get("risk_level", "safe")

    # 2. blocked：攔截，寫警告（不帶 tbm），不走原文投遞路
    if risk_level == "blocked":
        try:
            appwrite_srv.save_chat_message(
                sender_id="ai_assistant",
                receiver_id=req.receiver_id,
                room_id=room_id,
                content=WARNING_MSG,
            )
        except Exception as e:
            print(f"Failed to write warning message to Appwrite: {e}")
        save_message(room_id, "ai_assistant", WARNING_MSG)
        return plan_delivery("blocked", risk_assessment, None).response

    # 3. 非 blocked：計算投遞計畫，依計畫帶 triggered_by_msg_id
    plan = plan_delivery(risk_level, risk_assessment, None)

    # 4. Appwrite 先寫原文（source of truth），再 Mongo，再 track
    try:
        appwrite_srv.save_chat_message(
            sender_id=req.sender_id,
            receiver_id=req.receiver_id,
            room_id=room_id,
            content=req.message,
            triggered_by_msg_id=plan.triggered_by_msg_id,
        )
    except Exception as e:
        print(f"Failed to write chat message to Appwrite: {e}")
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=f"寫入 Appwrite 失敗: {e}")

    new_msg = save_message(room_id, req.sender_id, req.message)

    # Track in Agent 1 buffer
    history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", 1)
    full_history = list(history_cursor)
    track_message_in_buffer(room_id, new_msg, full_history, background_tasks)

    message_doc = {
        "sender_id": new_msg["sender_id"],
        "content": new_msg["content"],
        "timestamp": new_msg["timestamp"],
        "_id": str(new_msg["_id"]),
    }
    plan.response["message"] = message_doc
    return plan.response



