"""
風險檢測 API 路由 - Phase 2 修正版 (Guardrail 審計強化)
"""

import warnings
import os
from fastapi import APIRouter, HTTPException, BackgroundTasks
from app.models.schemas import FeedbackRequest, RiskDetectionRequest, RiskDetectionResponse, RiskState, SenderAppealRequest, ReceiverReportRequest
from app.core.rule_engine import RuleBasedEngine
from app.core.nlp_engine import NLPEngine
from app.core.risk_fusion import RiskFusionLayer
from app.core.risk_state import RiskStateMachine
from app.core.scenario_risk_layer import ScenarioRiskLayer
from app.core.intervention_engine import InterventionEngine
from app.core.guardrail_engine import GuardrailEngine
from app.services.chat_log_service import ChatLogService
from app.services.background_judge_service import BackgroundJudgeService
from app.services.temporal_feature_service import TemporalFeatureService
from appwrite.id import ID
import datetime
import json

# 徹底靜音所有過時警告
warnings.simplefilter('ignore')
os.environ['PYTHONWARNINGS'] = 'ignore'

def _pretty_format_risk(label: str, state_dict: dict) -> str:
    """美化風險狀態輸出，確保條列對齊"""
    lines = [f"   [ {label} ]"]
    for k in sorted(state_dict.keys()):
        v = state_dict[k]
        lines.append(f"      |-- {k.ljust(18)} : {v:.4f}")
    return "\n".join(lines)

router = APIRouter()

# 初始化引擎
rule_engine = RuleBasedEngine()
nlp_engine = NLPEngine()
fusion = RiskFusionLayer()
state_machine = RiskStateMachine()
scenario_risk_layer = ScenarioRiskLayer()
intervention_engine = InterventionEngine()
guardrail_engine = GuardrailEngine()
chat_log_service = ChatLogService()
background_judge_service = BackgroundJudgeService(chat_log_service)

async def handle_relationship_update(conv_id, sender_id, receiver_id):
    """內部輔助：處理關係指標更新與摘要觸發"""
    try:
        total_msgs = await chat_log_service.rel_service.update_metrics(conv_id, sender_id, receiver_id)
        memory_ctx = await chat_log_service.rel_service.get_memory_context(conv_id)
        metrics = memory_ctx['metrics']
        last_summary = memory_ctx['summary']

        should_trigger = False
        last_snapshot = (last_summary.get('msg_count_snapshot') or 0) if last_summary else 0
        if total_msgs - last_snapshot >= 20:
            should_trigger = True
        elif last_summary:
            last_sum_time = datetime.datetime.fromisoformat(last_summary['updated_at'].replace('Z', '+00:00'))
            now = datetime.datetime.now(datetime.timezone.utc)
            if (now - last_sum_time).total_seconds() / 3600 >= 6 and total_msgs > last_snapshot:
                should_trigger = True

        if should_trigger and metrics:
            await chat_log_service.rel_service.generate_rolling_summary(conv_id, metrics)
    except Exception as e:
        print(f"Relationship background update failed: {e}")

@router.post("/detect", response_model=RiskDetectionResponse)
async def detect_risk(req: RiskDetectionRequest, background_tasks: BackgroundTasks):
    """
    執行風險檢測 (整合 Guardrail 完整審計)
    """
    try:
        real_msg_id = ID.unique()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print("\n" + "="*70)
        print(f"   [ REQUEST ] {now_str}")
        print(f"   Sender: {req.sender_id} | Msg: {req.current_message}")
        print("-" * 50)

        # ---------------------------------------------------------
        # STEP 0: Semantic Guardrail
        # ---------------------------------------------------------
        gr_result = await guardrail_engine.check(req.current_message)
        if gr_result["is_blocked"]:
            print(f"   [ Step 0 ] Guardrail TRIGGERED: {gr_result['reason']}")
            if gr_result.get("flagged_words"):
                print(f"      |-- Flagged Words       : {gr_result['flagged_words']}")
            
            # 1. 建立 Blocked 狀態與診斷資訊 (統一使用 critical_override)
            fake_state = RiskState(sexual_boundary=1.0) 
            diag = {
                "reason": "critical_override", 
                "composite_score": 1.0, 
                "max_score": 1.0,
                "spread_score": 0.2,
                "trend_score": 0.0
            }
            
            # 2. 呼叫介入引擎
            intervention_cmd = await intervention_engine.execute(
                risk_level="blocked", risk_state=fake_state.model_dump(), 
                diagnosis=diag, conv_id=req.conversation_id, sender_id=req.sender_id, 
                receiver_id=req.receiver_id, msg_id=real_msg_id, decision_reason="critical_override",
                chat_log_service=chat_log_service
            )
            
            # 3. 完整審計日誌寫入 (補齊紀錄，但不更新 relationship memory)
            # A. 寫入 messages
            await chat_log_service.log_message(req, msg_id=real_msg_id, is_blocked=True, delivery_status="blocked")
            # B. 寫入風險歷史
            await chat_log_service.save_risk_state_history(
                req.conversation_id, req.sender_id, real_msg_id, 
                fake_state, "blocked", fake_state, decay_applied=False
            )
            # C. 寫入介入日誌
            await chat_log_service.log_intervention(
                req.conversation_id, real_msg_id, req.sender_id, req.receiver_id,
                "blocked", fake_state, diag, "critical_override", "sexual_boundary",
                intervention_cmd["sender_directive"]["action"], intervention_cmd["receiver_directive"]["action"],
                cooldown_seconds=intervention_cmd["sender_directive"].get("cooldown_seconds", 0)
            )
            
            response = RiskDetectionResponse(
                conversation_id=req.conversation_id, risk_level="blocked", should_intervene=True,
                risk_delta_rule=RiskState(), risk_delta_nlp=RiskState(), risk_delta_total=fake_state,
                new_risk_state=fake_state, intervention_command=intervention_cmd,
                nlp_reasoning=f"語意關鍵字阻擋: {gr_result['reason']}",
                triggered_rules=[gr_result['reason']],
                intervention_message=f"訊息因違反安全政策已被攔截 ({gr_result['reason']})",
                diagnostic_signals={
                    "composite": diag["composite_score"],
                    "max": diag["max_score"],
                    "spread": diag["spread_score"],
                    "trend": diag["trend_score"]
                }
            )
            print("="*70 + "\n")
            return response

        if gr_result.get("flagged_words"):
            print(f"   [ Step 0 ] Flagged (not blocked): {gr_result['flagged_words']}")
        if gr_result.get("classifier_flagged"):
            cats = gr_result.get("classifier_categories", "unknown")
            print(f"   [ Step 0 ] Classifier Flagged (not blocked): {cats}")

        # ---------------------------------------------------------
        # 分析前置：建立 Pending 訊息 (貫穿 ID)
        # ---------------------------------------------------------
        await chat_log_service.log_message(req, msg_id=real_msg_id, is_blocked=False, delivery_status="pending_review")

        # ---------------------------------------------------------
        # STEP 1: Context 讀取
        # ---------------------------------------------------------
        memory_ctx = await chat_log_service.rel_service.get_memory_context(req.conversation_id)
        relationship_memory = memory_ctx['metrics']
        last_summary = memory_ctx['summary']

        prior_state, _ = await state_machine.get_user_state(req.conversation_id, req.sender_id)
        
        # 雙歷史來源修正：
        # A. delivered_history: 給 LLM 語意分析與摘要 (確保不含未審核內容)
        delivered_history = await chat_log_service.get_recent_messages(req.conversation_id, limit=20, exclude_msg_id=real_msg_id)
        
        # B. behavior_history: 給 TemporalFeatureService 計算行為特徵 (包含 pending_review)
        behavior_history = await chat_log_service.get_recent_behavior_messages(req.conversation_id, limit=20, exclude_msg_id=real_msg_id)

        print(f"   [ Step 1 ] Context Loaded")
        print(f"      |-- History (delivered) : {len(delivered_history)} msgs")
        print(f"      |-- History (behavior)  : {len(behavior_history)} msgs")
        if relationship_memory:
            print(f"      |-- Familiarity         : {relationship_memory.get('familiarity_score', 0):.3f}")
            print(f"      |-- Balance             : {relationship_memory.get('conversation_balance', 0.5):.3f}")
            print(f"      |-- Total Messages      : {relationship_memory.get('total_messages', 0)}")
            print(f"      |-- Progression Rate    : {relationship_memory.get('intimacy_progression_rate', 0):.4f}")
        if last_summary:
            print(f"      |-- Last Intimacy Level : {last_summary.get('intimacy_level', 0):.3f}")
        
        # 後端主導計算行為特徵 (使用 behavior_history)
        computed_features = TemporalFeatureService.calculate(
            current_content=req.current_message, current_sender=req.sender_id, history=behavior_history
        )

        # ---------------------------------------------------------
        # STEP 2 ~ 8: 核心分析
        # ---------------------------------------------------------
        rule_result = rule_engine.calculate(req.current_message, computed_features)
        print(_pretty_format_risk("Step 2: Rule Engine Delta", rule_result['delta'].model_dump()))
        if rule_result.get('triggered_rules'):
            print(f"      |-- Triggered           : {rule_result['triggered_rules']}")

        nlp_result = nlp_engine.analyze(
            req.current_message, delivered_history, computed_features,
            sender_id=req.sender_id, prior_risk_state=prior_state,
            relationship_memory=relationship_memory, last_summary=last_summary
        )
        print(_pretty_format_risk("Step 3: NLP Engine Delta", nlp_result['delta'].model_dump()))
        print(f"      |-- NLP Confidence      : {nlp_result.get('confidence', 0):.3f}")
        print(f"      |-- NLP Reasoning       : {str(nlp_result.get('reasoning', ''))[:100]}")
        print(f"      |-- NLP Detected Feats  : {nlp_result.get('detected_features', [])}")

        initial_delta = fusion.fuse(rule_result['delta'], nlp_result['delta'], nlp_confidence=nlp_result.get('confidence', 0.0))
        # 時段相關的情境規則以「訊息發送時間」為準；未帶則退回處理當下
        msg_time = None
        if req.message_timestamp:
            try:
                msg_time = datetime.datetime.fromisoformat(
                    req.message_timestamp.replace('Z', '+00:00')
                )
            except ValueError:
                print(f"   [ Warning ] 無法解析 message_timestamp: {req.message_timestamp}")

        bonus_delta, scenarios = scenario_risk_layer.evaluate(
            rule_result, nlp_result, computed_features,
            memory_metrics=relationship_memory, last_summary=last_summary,
            message_time=msg_time
        )
        print(_pretty_format_risk("Step 5: Scenario Bonus Delta", bonus_delta.model_dump()))
        print(f"      |-- Triggered Scenarios : {scenarios if scenarios else 'None'}")
        
        final_delta = fusion.apply_scenario_bonus(initial_delta, bonus_delta)
        print(_pretty_format_risk("Step 6: Total Message Delta", final_delta.model_dump()))

        new_state, risk_level = await state_machine.update(req.conversation_id, req.sender_id, real_msg_id, final_delta)
        diag = getattr(state_machine, 'last_diagnostic', {})
        print(_pretty_format_risk("Step 7: Updated Cumulative State", new_state.model_dump()))
        recal = diag.get('feedback_signal', 'neutral')
        if recal != 'neutral':
            print(f"      |-- Feedback Recalibration : {recal}")
        print(f"   [ Step 8 ] Decision: {risk_level.upper()}")
        print(f"      |-- Composite Score     : {diag.get('composite_score', 0):.4f}")
        print(f"      |-- Max Intensity       : {diag.get('max_score', 0):.4f}")
        print(f"      |-- Risk Spread Signal  : {diag.get('spread_score', 0):.4f}")
        print(f"      |-- Risk Trend Signal   : {diag.get('trend_score', 0):.4f}")
        print(f"      |-- Decision Reason     : {diag.get('reason', 'normal')}")

        # ---------------------------------------------------------
        # STEP 9: 產生介入指令
        # ---------------------------------------------------------
        # 已處置豁免條件④需要「本則 delta 的 max」（不是累積後的 max_score）。
        # final_delta 是本則訊息的風險增量，把它 max 後附進 diag 給 intervention_engine。
        diag["delta_max"] = max(final_delta.model_dump().values()) if final_delta else 0.0

        intervention_cmd = await intervention_engine.execute(
            risk_level=risk_level, risk_state=new_state.model_dump(), diagnosis=diag,
            conv_id=req.conversation_id, sender_id=req.sender_id, receiver_id=req.receiver_id,
            msg_id=real_msg_id, decision_reason=diag.get('reason', 'normal'),
            chat_log_service=chat_log_service
        )

        # 豁免時不鎖訊息：blocked 但已處置過同一件事 → 照送，累積狀態仍留著。
        is_msg_blocked = (risk_level == "blocked"
                          and not intervention_cmd.get("sanction_exempted", False))
        final_delivery_status = "blocked" if is_msg_blocked else "delivered"

        # ---------------------------------------------------------
        # 效能優化：背景執行更新
        # ---------------------------------------------------------
        background_tasks.add_task(chat_log_service.update_message_status, real_msg_id, is_msg_blocked, final_delivery_status)
        background_tasks.add_task(chat_log_service.update_temporal_features, req.conversation_id, req.sender_id, computed_features)
        background_tasks.add_task(
            chat_log_service.log_analysis_detail,
            real_msg_id, req.conversation_id, rule_result, nlp_result,
            final_delta, scenarios, diag, gr_result.get("flagged_words", []),
            {
                "flagged": gr_result.get("classifier_flagged", False),
                "categories": gr_result.get("classifier_categories", ""),
            }
        )

        classifier_flag = {
            "flagged": gr_result.get("classifier_flagged", False),
            "categories": gr_result.get("classifier_categories", ""),
        }
        flagged_words = gr_result.get("flagged_words", [])
        if background_judge_service.should_review(flagged_words, classifier_flag):
            background_tasks.add_task(
                background_judge_service.review_guardrail_context,
                req.conversation_id, req.sender_id, real_msg_id,
                req.current_message, delivered_history, flagged_words, classifier_flag
            )
        
        if final_delivery_status == "delivered":
            background_tasks.add_task(handle_relationship_update, req.conversation_id, req.sender_id, req.receiver_id)

        if risk_level != "safe":
            primary_risk_type = max(new_state.model_dump(), key=new_state.model_dump().get)
            background_tasks.add_task(
                chat_log_service.log_intervention,
                req.conversation_id, real_msg_id, req.sender_id, req.receiver_id,
                risk_level, new_state, diag, diag.get('reason', 'normal'), primary_risk_type,
                intervention_cmd["sender_directive"]["action"], intervention_cmd["receiver_directive"]["action"],
                cooldown_seconds=intervention_cmd["sender_directive"].get("cooldown_seconds", 0)
            )
            print(f"   [ Step 9 ] Sender Action  : {intervention_cmd['sender_directive']['action']}")
            print(f"      |-- Receiver Action     : {intervention_cmd['receiver_directive']['action']}")
            print(f"      |-- Delivery Status     : {final_delivery_status}")

        response = RiskDetectionResponse(
            conversation_id=req.conversation_id,
            risk_delta_rule=rule_result['delta'],
            risk_delta_nlp=nlp_result['delta'],
            risk_delta_total=final_delta,
            new_risk_state=new_state,
            risk_level=risk_level,
            should_intervene=(risk_level != "safe"),
            nlp_reasoning=nlp_result.get('reasoning'),
            triggered_rules=rule_result['triggered_rules'] + scenarios,
            intervention_command=intervention_cmd,
            intervention_message=intervention_cmd["sender_directive"]["content"]["body"] if intervention_cmd["sender_directive"]["content"] else None,
            diagnostic_signals={
                "max": diag.get("max_score", 0.0), "spread": diag.get("spread_score", 0.0), 
                "trend": diag.get("trend_score", 0.0), "composite": diag.get("composite_score", 0.0)
            },
            nlp_confidence=nlp_result.get('confidence', 0.0),
            # NLP 失敗時 _fallback_result() 回傳全 0 的 delta，其結果與「模型判定無風險」
            # 完全無法區分。必須向呼叫端揭露，否則降級判斷會被當成正常的 safe。
            nlp_degraded=str(nlp_result.get('reasoning', '')).startswith('Fallback:'),
            guardrail_degraded=gr_result.get('degraded', False),
        )
        print("="*70 + "\n")
        return response

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/reset")
async def reset_risk_state(req: dict):
    return {"status": "Use reset_db.py for reset"}

@router.get("/state")
async def get_risk_state(conversation_id: str, user_id: str):
    """查詢某使用者在某對話中的當前風險狀態與剩餘冷卻秒數。

    供前端在重新進入聊天室時還原 UI：若不提供此查詢，關閉 App 重開後
    前端的冷卻倒數即消失，冷卻等同未曾施加。
    """
    from appwrite.query import Query
    prior_state, _ = await state_machine.get_user_state(conversation_id, user_id)
    level = "safe"
    try:
        response = chat_log_service.db.list_documents(
            database_id=chat_log_service.db_id,
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
            level = d.get("risk_level", "safe")
    except Exception as e:
        print(f"get_risk_state: 讀取最新風險等級失敗: {e}")
    remaining = await chat_log_service.get_remaining_cooldown(conversation_id, user_id)
    return {
        "conversation_id": conversation_id,
        "user_id": user_id,
        "risk_level": level,
        "risk_state": prior_state.model_dump(),
        "remaining_cooldown": remaining
    }

@router.post("/feedback")
async def submit_feedback(req: FeedbackRequest):
    """
    Receiver/Sender 對某則訊息的介入回饋。
    寫入 intervention_logs.receiver_feedback or sender_feedback。
    """
    if req.role not in ("sender", "receiver"):
        raise HTTPException(status_code=400, detail="role must be 'sender' or 'receiver'")
    if req.feedback not in ("comfortable", "uncomfortable"):
        raise HTTPException(status_code=400, detail="feedback must be 'comfortable' or 'uncomfortable'")

    ok = await chat_log_service.update_intervention_feedback(
        msg_id=req.triggered_by_msg_id,
        role=req.role,
        feedback=req.feedback,
        detail=req.detail,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="intervention log not found")

    print(f"   [ Feedback ] {req.role}={req.feedback} for msg {req.triggered_by_msg_id}"
          + (f"（詳述 {len(req.detail)} 字）" if req.detail else ""))
    return {
        "status": "ok",
        "msg_id": req.triggered_by_msg_id,
        "role": req.role,
        "feedback": req.feedback,
        "detail": req.detail,
    }


@router.post("/appeal")
async def submit_sender_appeal(req: SenderAppealRequest):
    """寄件方對某次介入提出文字申訴（供人工稽核）。

    使用時機：訊息被判定為 restricted 或 blocked 時，讓寄件方說明或表達異議，
    後續由人工稽核與接收方的回饋並列對照，判斷該次介入是否恰當。

    **此內容不進入任何演算法**，不影響風險分數、不影響 feedback_signal。
    """
    result = await chat_log_service.save_sender_appeal(
        msg_id=req.triggered_by_msg_id,
        sender_id=req.sender_id,
        appeal_text=req.appeal_text,
    )

    if not result["ok"]:
        err = result["error"]
        if err == "not_found":
            raise HTTPException(status_code=404, detail="intervention log not found")
        if err == "sender_mismatch":
            raise HTTPException(status_code=403, detail="only the message sender may appeal")
        if err == "attribute_missing":
            raise HTTPException(
                status_code=503,
                detail="Appwrite intervention_logs 尚未建立 sender_appeal_text 屬性（String, size 2000）"
            )
        raise HTTPException(status_code=500, detail="failed to save appeal")

    print(f"   [ Appeal ] sender={req.sender_id} 對 msg {req.triggered_by_msg_id} 提出申訴（{len(req.appeal_text)} 字）")
    return {
        "status": "ok",
        "msg_id": req.triggered_by_msg_id,
        "note": "已記錄，供人工稽核；不影響風險判斷"
    }


@router.post("/report")
async def submit_receiver_report(req: ReceiverReportRequest):
    """收件方對某次介入／該則訊息提出文字回報（供人工稽核）。

    與 /appeal 角色相反、稽核意義相反：/appeal 是被警告者自辯，/report 是被保護者
    陳述。刻意分成兩個端點兩個欄位，混用會使後台無從分辨該次介入是否恰當。

    **此內容不進入任何演算法**，不影響風險分數、不影響 feedback_signal。
    """
    result = await chat_log_service.save_receiver_report(
        msg_id=req.triggered_by_msg_id,
        receiver_id=req.receiver_id,
        report_text=req.report_text,
    )

    if not result["ok"]:
        err = result["error"]
        if err == "not_found":
            raise HTTPException(status_code=404, detail="intervention log not found")
        if err == "receiver_mismatch":
            raise HTTPException(status_code=403, detail="only the message receiver may report")
        if err == "attribute_missing":
            raise HTTPException(
                status_code=503,
                detail="Appwrite intervention_logs 尚未建立 receiver_report_text 屬性（String, size 2000）"
            )
        raise HTTPException(status_code=500, detail="failed to save report")

    print(f"   [ Report ] receiver={req.receiver_id} 對 msg {req.triggered_by_msg_id} 提出回報（{len(req.report_text)} 字）")
    return {
        "status": "ok",
        "msg_id": req.triggered_by_msg_id,
        "note": "已記錄，供人工稽核；不影響風險判斷"
    }
