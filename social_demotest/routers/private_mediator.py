"""Private mediator HTTP adapters and legacy/private-V2 orchestration."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse

from database import matches_coll, messages_coll, profiles_coll
from models import MediatorPrivateRequest
from services.ai_service import generate_chat_completion, orchestrate_date_coordination
from services.ayue_agent.private_runtime import (
    PrivateAgentTurnContext,
    private_agent_mode_for_user,
    run_private_agent_turn,
)
from services.ayue_agent.private_contracts import PrivateClientAction
from services.ayue_agent.private_v2 import private_v2_mode_for_user, run_private_agent_turn_v2
from services.ayue_agent.product_identity import PRIVATE_RUNTIME_FALLBACK_REPLY
from services.chat_service import generate_room_id, save_message
from services.mediator_context_service import (
    MEDIATOR_PERSONA,
    latest_shared_chat,
    mediator_profile_context,
    mediator_style,
)
from services.mediator_event_service import queue_mediator_event
from services.profile_task_service import queue_profile_skills
from services.relationship_engagement_service import (
    consume_pending_probe_answer,
    find_accepted_match,
    generate_mediator_private_room_id,
    participant_probe_state,
    relationship_unread_field,
)
from services.semantic_plan_service import get_relationship_semantic_context

router = APIRouter()

@router.get("/mediator/private/{other_id}")

def get_mediator_private_messages(other_id: str, user_id: str):

    match_doc = find_accepted_match(user_id, other_id)

    if not match_doc:

        raise HTTPException(status_code=403, detail="只能查看已接受配對的媒人私聊")

    room_id = generate_mediator_private_room_id(user_id, other_id)

    if messages_coll.count_documents({"room_id": room_id}) == 0:

        save_message(room_id, "ai_assistant", f"這裡可以私下跟我打聽 {other_id}，我會盡量只講有根據的事。")

    unread_field = relationship_unread_field(match_doc, user_id)

    unread_count = int((match_doc.get("private_unread", {}) or {}).get(

        "from" if match_doc.get("from_user") == user_id else "to", 0

    ))

    matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {unread_field: 0}})

    user_doc = profiles_coll.find_one({"user_id": user_id}) or {}

    pending = user_doc.get("pending_private_feedback") or {}

    if pending.get("other_id") != other_id:

        pending = {}

    pending_date = user_doc.get("pending_date_coordination") or {}

    if pending_date.get("other_id") != other_id:

        pending_date = {}

    msgs = list(messages_coll.find({"room_id": room_id}, {"_id": 0}).sort("timestamp", 1))

    return {

        "messages": msgs,

        "other_id": other_id,

        "unread_count": unread_count,

        "pending_step": pending.get("stage") or (

            "date_" + pending_date.get("stage") if pending_date.get("stage") else None

        ),

        "probe_state": participant_probe_state(match_doc, user_id),

        "other_probe_state": participant_probe_state(match_doc, other_id),

        "mediator_tone": user_doc.get("mediator_tone", "friend")

    }



def consent_intent(message: str):

    try:

        prompt = f"""

請判斷使用者是否同意媒人把訊息/回饋轉述或分享給另一位配對對象。

只輸出 JSON：{{"consent":true|false|null,"confidence":0.0}}

使用者訊息：{message}

"""

        result = json.loads(generate_chat_completion(prompt, temperature=0, json_output=True).content)

        if float(result.get("confidence", 0)) < 0.55:

            return None

        consent = result.get("consent")

        if consent is True:

            return True

        if consent is False:

            return False

    except Exception as e:

        print(f"Consent intent classification failed: {e}")

    return None



def save_private_mediator_reply(room_id: str, reply: str, event_type="text", actions=None, handoff=None):

    message_type = "mediator_card" if actions else "text"
    metadata = {"event_type": event_type, "actions": actions or []}
    if handoff:
        metadata["handoff"] = handoff

    save_message(

        room_id, "ai_assistant", reply, message_type=message_type,

        metadata=metadata

    )


def _run_private_v2_saved_turn(req: MediatorPrivateRequest, match_doc: dict, room_id: str, on_progress=None, agent_run_id: str | None = None) -> dict:
    """Persist exactly one private V2 final after the owner message is saved."""
    result = run_private_agent_turn_v2(
        user_id=req.user_id, other_id=req.other_id, message=req.message,
        match_doc=match_doc, on_progress=on_progress, agent_run_id=agent_run_id,
    )
    reply = result.reply or PRIVATE_RUNTIME_FALLBACK_REPLY
    handoff = result.handoff.model_dump() if getattr(result, "handoff", None) else None
    actions = []
    if handoff:
        actions = [PrivateClientAction(
            kind="navigate_public_prefill",
            label="回阿月主聊天室 →",
            value=handoff["original_message"],
        ).model_dump()]
    event_type = "agentic_private_redirect" if handoff else "agentic_private_v2"
    save_private_mediator_reply(room_id, reply, event_type, actions=actions, handoff=handoff)
    return {
        "reply": reply, "pending_step": None, "agent_run_id": result.agent_run_id,
        "agent_mode": "v2", "agent_version": "v2", "conversation_intent": result.conversation_intent,
        "handoff": handoff,
        "actions": actions,
    }



@router.post("/mediator/private")

def mediator_private_chat(req: MediatorPrivateRequest, background_tasks: BackgroundTasks):

    match_doc = find_accepted_match(req.user_id, req.other_id)

    if not match_doc:

        raise HTTPException(status_code=403, detail="只能在已接受配對中私聊媒人")



    room_id = generate_mediator_private_room_id(req.user_id, req.other_id)

    user_message = save_message(room_id, req.user_id, req.message)

    profiles_coll.update_one(

        {"user_id": req.user_id},

        {"$set": {"last_user_activity_at": time.time()}},

        upsert=True,

    )

    queue_profile_skills(
        background_tasks, req.user_id, req.message, user_message.get("message_id"),
        "relationship_private", str(match_doc["_id"]),
    )

    user_doc = profiles_coll.find_one({"user_id": req.user_id}) or {}
    probe_reply = consume_pending_probe_answer(match_doc, user_doc, req.user_id, req.other_id, req.message)
    if probe_reply is not None:
        save_private_mediator_reply(room_id, probe_reply)
        return {"reply": probe_reply, "pending_step": None}

    # V2 owns the entire private turn when explicitly enabled. It must not
    # fall through into legacy keyword branches after a planner/tool failure.
    if private_v2_mode_for_user(req.user_id) == "on":
        return _run_private_v2_saved_turn(req, match_doc, room_id)

    # Date coordination is now a shared, consent-based flow. Private prompts can
    # start the invitation, but never create an additional form/card by themselves.
    current_coordination = match_doc.get("date_coordination") or {}
    if "幫我們協調約會" in req.message:
        from services.date_coordination_service import create_invite
        created = create_invite(match_doc, req.user_id, req.other_id)
        if created:
            reply = "我先問問對方願不願意一起協調；對方同意後，共同聊天室會出現同一張約會表單。"
        elif current_coordination.get("status") == "pending_partner":
            reply = "這次約會邀請還在等對方回覆，我先不重複開新表單。"
        else:
            reply = "你們目前已有一張約會表單，直接到共同聊天室一起更新就好。"
        save_private_mediator_reply(room_id, reply, "date_coordination_invite")
        return {"reply": reply, "pending_step": None}

    if current_coordination.get("status") in {"pending_partner", "active"}:
        reply = "約會協調正在共同聊天室進行中；我會同步整理你們在那裡確認的時間與地點。"
        save_private_mediator_reply(room_id, reply, "date_coordination_redirect")
        return {"reply": reply, "pending_step": None}

    private_agent_mode = private_agent_mode_for_user(req.user_id)
    if private_agent_mode != "off":
        private_agent_result = run_private_agent_turn(
            PrivateAgentTurnContext(
                user_id=req.user_id,
                other_id=req.other_id,
                room_id=room_id,
                message=req.message,
                match_doc=match_doc,
                user_profile=user_doc,
            ),
            mode=private_agent_mode,
        )
        if private_agent_mode == "on" and private_agent_result.handled:
            reply = private_agent_result.reply or "我先幫你整理一下。"
            save_private_mediator_reply(room_id, reply, "agentic_private")
            return {
                "reply": reply,
                "pending_step": None,
                "agent_run_id": private_agent_result.agent_run_id,
                "agent_mode": private_agent_result.agent_mode,
                "conversation_intent": private_agent_result.conversation_intent,
            }

    date_coordination = match_doc.get("date_coordination", {}) or {}

    is_active_date = date_coordination.get("status") in ["active", "gathering"]



    if "幫我們協調約會" in req.message and not is_active_date:

        date_coordination = {

            "status": "gathering",

            "form": {"date": "", "time": "", "activity": "", "budget": ""},

            "confirmations": {},

            "room_id": str(match_doc["_id"]),

            "established_at": time.time()

        }

        matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {"date_coordination": date_coordination}})

        is_active_date = True



    if is_active_date:

        pair_room_id = generate_room_id(match_doc["from_user"], match_doc["to_user"])

        semantic_context = get_relationship_semantic_context(match_doc, pair_room_id)

        relationship_context = {

            "viewer": {"user_id": req.user_id, "profile": user_doc.get("deep_profile")},

            "partner": {"user_id": req.other_id},

            "relationship": {

                "match_id": str(match_doc["_id"]),

                "shared_chat_summary": match_doc.get("relationship_memory", {}),

                "semantic_plan": semantic_context.get("semantic_plan", {}),

                "chat_knowledge_graph": semantic_context.get("knowledge_graph_triples", []),

            }

        }



        result = orchestrate_date_coordination(req.message, date_coordination, relationship_context)

        reply = result.get("reply", "我了解了。")

        save_private_mediator_reply(room_id, reply, "date_coordination_chat")



        if result.get("form"):

            date_coordination["form"] = result["form"]

            date_coordination["confirmations"] = {}

            matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {"date_coordination": date_coordination}})



        if result.get("show_form"):

            date_coordination["status"] = "active"

            matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {"date_coordination.status": "active"}})



            form_payload = date_coordination["form"]

            card_metadata = {

                "event_type": "date_coordination_form",

                "match_id": str(match_doc["_id"]),

                "form": form_payload,

                "confirmations": date_coordination["confirmations"]

            }



            queue_mediator_event(

                req.other_id,

                "對方正在提議約會時間跟地點，你來看看適不適合！",

                "date_coordination_form",

                match_id=str(match_doc["_id"]),

                other_id=req.user_id,

                form=form_payload,

                confirmations=date_coordination["confirmations"]

            )

            save_message(pair_room_id, "ai_assistant", "阿月幫你們整理了約會表單，雙方確認後就成立囉！", message_type="mediator_card", metadata=card_metadata)



        return {"reply": reply, "pending_step": None}



    feedback = match_doc.get("private_feedback", {}) or {}

    consented_signal = None

    other_feedback = feedback.get(req.other_id, {}) or {}

    if other_feedback.get("share_consent") is True:

        consented_signal = other_feedback.get("sentiment")

    pair_room_id = generate_room_id(match_doc["from_user"], match_doc["to_user"])

    semantic_context = get_relationship_semantic_context(match_doc, pair_room_id)

    relationship_context = {

        "viewer": mediator_profile_context(req.user_id, req.message),

        "partner": mediator_profile_context(req.other_id, req.message),

        "relationship": {

            "match_id": str(match_doc["_id"]),

            "match_reason_for_viewer": (

                match_doc.get("reason") if match_doc.get("from_user") == req.user_id

                else match_doc.get("receiver_reason", match_doc.get("reason"))

            ),

            "validated_reason_items": match_doc.get("reason_items", []),

            "shared_chat_summary": match_doc.get("relationship_memory", {}),

            "latest_shared_chat": latest_shared_chat(match_doc, 16),

            "viewer_mediator_state": participant_probe_state(match_doc, req.user_id),

            "partner_mediator_state": participant_probe_state(match_doc, req.other_id),

            "partner_consented_signal": consented_signal,

            "semantic_plan": semantic_context.get("semantic_plan", {}),

            "chat_knowledge_graph": semantic_context.get("knowledge_graph_triples", []),

        },

    }

    prompt = f"""

{MEDIATOR_PERSONA}

媒人語氣：{mediator_style(req.user_id)}



你現在是使用者的一位「共同朋友」，正在私下跟他聊他的配對對象。請展現出朋友的【主觀與同理心】 (Agency & Empathy)，而不是一個客觀冰冷的分析工具。

回答守則：

1. **要有主觀意見與同理心**：請適度偏袒使用者，給予情感上的安撫與支持。使用非常口語化的語氣（例如：「我覺得」、「老實說」、「我懂！」），遇到看不懂的情況可以直接說不知道。

2. **具體破冰與延續話題建議**：當察覺到尷尬或卡關時，主動提供具體的聊天方向或話題包裝方式。

3. **微量透漏小秘密**：偶爾可以從 `chat_knowledge_graph` 中提取對方的一些無傷大雅的正面喜好或習慣（例如「偷偷跟你說，她有跟我提到滿喜歡貓的喔」）來助攻。

4. **保留神秘感與邊界**：絕對不要直接告訴使用者對方極度私密的心意、地雷或核心感情決策。鼓勵使用者自己去探索。

5. **動態角色扮演**：嚴格遵守 semantic_plan 裡的 current_role：

   - FRIEND (朋友): 建立共鳴點，提供輕鬆好接的話題開場白。

   - ADVISER (顧問): 給出戰術建議，告訴使用者該怎麼轉移話題或如何包裝回覆。

   - MENTOR (導師): 提出模糊、引導思考的提問，問使用者他們真正想達成的目標是什麼。不要給出具體答案。

   - FACILITATOR (引導員): 提供一個輕微的推動，維持當前話題的熱度。



資料：

{json.dumps(relationship_context, ensure_ascii=False)}



使用者訊息：{req.message}

"""

    try:

        reply = generate_chat_completion(prompt, temperature=0.35, json_output=False).content

    except Exception as e:

        print(f"Mediator private chat error: {e}")

        reply = "我剛剛有點卡住，等我一下再幫你整理得更清楚。"



    save_private_mediator_reply(room_id, reply)

    return {"reply": reply, "pending_step": None}





@router.post("/mediator/private/stream")
def mediator_private_chat_stream(req: MediatorPrivateRequest, background_tasks: BackgroundTasks):
    """NDJSON private-V2 stream; legacy private chat remains JSON-only."""
    if private_v2_mode_for_user(req.user_id) != "on":
        raise HTTPException(status_code=409, detail="悄悄話 V2 尚未啟用")
    match_doc = find_accepted_match(req.user_id, req.other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只能在已接受配對中私聊媒人")
    event_queue: queue.Queue[dict | None] = queue.Queue(maxsize=16)
    fallback_run_id = uuid.uuid4().hex

    def emit(event: dict) -> None:
        if event.get("type") not in {"run_started", "tool_started", "tool_finished"}:
            return
        safe = {key: event[key] for key in ("type", "agent_run_id", "step_id", "text", "outcome") if key in event}
        try:
            event_queue.put_nowait(safe)
        except queue.Full:
            pass

    def worker() -> None:
        worker_tasks = BackgroundTasks()
        try:
            room_id = generate_mediator_private_room_id(req.user_id, req.other_id)
            user_message = save_message(room_id, req.user_id, req.message)
            queue_profile_skills(worker_tasks, req.user_id, req.message, user_message.get("message_id"), "relationship_private", str(match_doc["_id"]))
            emit({"type": "run_started", "agent_run_id": fallback_run_id})
            response = _run_private_v2_saved_turn(req, match_doc, room_id, emit, fallback_run_id)
            event_queue.put({"type": "final", "response": response})
        except Exception:
            event_queue.put({"type": "error", "agent_run_id": fallback_run_id, "reply": PRIVATE_RUNTIME_FALLBACK_REPLY})
        finally:
            try:
                asyncio.run(worker_tasks())
            except Exception:
                pass
            event_queue.put(None)

    def event_stream():
        while True:
            event = event_queue.get()
            if event is None:
                break
            yield json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"

    threading.Thread(target=worker, name="ayue-private-stream", daemon=True).start()
    return StreamingResponse(event_stream(), media_type="application/x-ndjson")
