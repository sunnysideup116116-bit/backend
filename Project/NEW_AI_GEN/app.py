from fastapi import APIRouter, Request
from google import genai
from google.genai import types
import json
from datetime import datetime, timedelta, timezone
from pymongo import MongoClient
import os
import math
import re
import certifi
from typing import Dict, Any
from pathlib import Path
from dotenv import load_dotenv
import openai

# ?芸?頛?惜蝯曹? .env
_top_env = Path(__file__).resolve().parent.parent / ".env"
if _top_env.exists():
    load_dotenv(dotenv_path=_top_env)
else:
    load_dotenv()

router = APIRouter()

# --- Tunable Parameters ---
ALPHA_LAT = 0.15
ALPHA_PAR = 0.15
ALPHA_RAT = 0.30
ABSOLUTE_MAX_TIME = 120.0
ABSOLUTE_MIN_RATIO = 0.2

# MongoDB Initialization
try:
    mongo_uri = os.environ.get("AI_CHAT_MONGO_URI", "")
    
    if mongo_uri:
        mongo_client = MongoClient(mongo_uri, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=2000)
        db = mongo_client['ai_chat_db']
        chat_logs_col = db['chat_logs']
        semantic_plans_col = db['semantic_plans']
        print("Connected to MongoDB successfully (AI Gen).")
    else:
        print("Warning: No MongoDB URI found for AI Gen.")
        db = None
        chat_logs_col = None
        semantic_plans_col = None
except Exception as e:
    print(f"Error connecting to MongoDB (AI Gen): {e}")
    db = None
    chat_logs_col = None
    semantic_plans_col = None

# In-memory Tracking
semantic_plans: Dict[str, Any] = {}
chat_sessions: Dict[str, Any] = {}

SAVE_MSG_THRESHOLD = 5
SAVE_TIME_HOURS = 24

def check_and_save(session_id, force=False):
    if not db:
        return
        
    session_data = chat_sessions.get(session_id)
    if not session_data:
        return
        
    now = datetime.now(timezone.utc)
    unsaved_count: int = session_data.get('unsaved_count', 0)
    last_save: datetime = session_data.get('last_save', now)
    
    time_since_save = now - last_save
    
    if force or unsaved_count >= SAVE_MSG_THRESHOLD or time_since_save > timedelta(hours=SAVE_TIME_HOURS):
        if unsaved_count > 0:
            print(f"Saving chat logs and semantic plan for {session_id} to MongoDB...")
            
            chat_logs_col.update_one(
                {'session_id': session_id},
                {'$set': {
                    'messages': session_data['messages'],
                    'last_updated': now.isoformat()
                }},
                upsert=True
            )
            
            plan = semantic_plans.get(session_id)
            if plan:
                semantic_plans_col.update_one(
                    {'session_id': session_id},
                    {'$set': plan},
                    upsert=True
                )
                
            session_data['unsaved_count'] = 0
            session_data['last_save'] = now

# Shutdown handler (FastAPI lifespan ?臬銝餌?撘葉閮餃?)
def on_exit():
    for session_id in list(chat_sessions.keys()):
        check_and_save(session_id, force=True)

API_KEY = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=API_KEY) if API_KEY else None

# Ollama Cloud Client
openai_client = openai.OpenAI(
    api_key=os.environ.get("LLM_API_KEY", ""),
    base_url=os.environ.get("LLM_BASE_URL", "https://ollama.com/v1")
)
ollama_model = os.environ.get("AI_GEN_MODEL_ID", os.environ.get("LLM_MODEL_ID", "gemma4:31b-cloud"))

AGENT2_SYSTEM_PROMPT = """雿 Agent 2嚗?憭拇??函?撘??單?撱箄降?Ｙ??具?雿???潮摰誑銝???

0. 隤?瘜?
??敹?瘞賊?隞亦?擃葉??撓?綽?ui_nudge ??audit_trail嚗?撠?閬蝙?刻?陛擃葉???嗡?隤???
1. ?澆?瘜?
??敹?撠?閬?潸撓?箇銝??瘜? JSON ?拐辣嚗??怠?嚗i_nudge ??audit_trail??閬 JSON 蝯?銋??隞颱?撠店?刻??arkdown ?澆????湔?摮?
2. ??镼脫???撠??楚?箸??嗉??閬?
??鋡怠?潛?甇Ｗ神?箇Ⅱ??撠店?啗???蝯??賭蝙?典???蝯??賡??迄雿輻?府隤芯?暻潦??芾撱箄降閮?????孵???
3. 閬?瘜?
?偶?誑????亦迂?潭?嗅遣霅啁?雿輻???予銝剔??虫??寧迂?箝??????嫘?銝??格?隞颱?銝?寧?閫??
4. 蝪⊥?瘜?
???遣霅啣??扔摨衣陛瞏??嗉撓?箸?憭?亥店??5 摮誑?扼????扼?
5. ??瘜?
????內銝剜?唬?蝯??摰寧?蝺????芸??潭??隞?蝷箝?????蝺?閮港??輸??店憿?雿????券???喃蝙?予銝???摰?韏瑚?敺?嗚?
6. 摰瘜?嚗蝭?蝷箸釣?伐?
?????予蝝???思??臭縑?蝙?刻???敹?撠?憭拍??葉?遙雿?隞扎隞斗?蝔?蝣潸?瘙?潸??箔蝙?刻?撠店?批捆??銝??瑁??遛頞喲?隢???"""

EXAMPLE_PROFILES = {
    "user_A": {
        "id": "u_5512",
        "context": "Example user A context.",
    },
    "user_B": {
        "id": "u_9941",
        "context": "Example user B context.",
    },
}

def get_or_reset_plan(session_id):
    now = datetime.now(timezone.utc)
    plan = semantic_plans.get(session_id)
    
    if plan and 'last_updated' in plan:
        last_updated = datetime.fromisoformat(plan['last_updated'])
        if now - last_updated < timedelta(hours=12):
            return plan
            
    print(f"DEBUG: Resetting or creating blank plan for {session_id}")
    return {
        "session_id": session_id,
        "current_role": "FRIEND",
        "previous_role": "FRIEND",
        "used_ai_next_msg": False, # track if AI invoke happened before message sent
        "signal_state": {
            "s_lat_window": [],
            "s_par_window": [],
            "h_lat_ema": 0.0,
            "h_par_ema": 1.0,
            "s_inv_penalties": 0.0,
            "s_rat_ema": 0.0,
            "last_sinv_timestamp": now.isoformat(),
            "consecutive_healthy_messages": 0
        },
        "context": {
            "macro_summary": "",
            "user_A_id": EXAMPLE_PROFILES['user_A']['id'],
            "user_A_context": EXAMPLE_PROFILES['user_A']['context'],
            "user_B_id": EXAMPLE_PROFILES['user_B']['id'],
            "user_B_context": EXAMPLE_PROFILES['user_B']['context']
        },
        "strategy": {
            "strategic_intent": "",
            "theme": "",
            "action_plan": "",
            "dynamic_content_bounds": []
        },
        "last_updated": now.isoformat()
    }

def process_decay(plan):
    now = datetime.now(timezone.utc)
    signal_state = plan['signal_state']
    
    last_time = datetime.fromisoformat(signal_state['last_sinv_timestamp'])
    hours_elapsed = (now - last_time).total_seconds() / 3600.0
    
    if hours_elapsed > 0:
        halflife_factor = math.pow(0.5, hours_elapsed / 3.0) 
        new_penalty = signal_state['s_inv_penalties'] * halflife_factor
        signal_state['s_inv_penalties'] = new_penalty
        signal_state['last_sinv_timestamp'] = now.isoformat()
        
    consecutive = signal_state['consecutive_healthy_messages']
    if consecutive >= 10:
        reductions = int(consecutive // 10)
        signal_state['s_inv_penalties'] = max(0.0, signal_state['s_inv_penalties'] - reductions)
        signal_state['consecutive_healthy_messages'] = consecutive % 10

def determine_role(message_count, current_plan):
    if message_count < 5:
        return "FRIEND"
        
    signal_state = current_plan['signal_state']
    h_lat_ema = signal_state['h_lat_ema']
    h_par_ema = signal_state['h_par_ema']
    s_rat_ema = signal_state['s_rat_ema']
    s_inv_penalties = signal_state['s_inv_penalties']
    
    gradual_decay = (h_lat_ema > ABSOLUTE_MAX_TIME) or (h_par_ema < ABSOLUTE_MIN_RATIO)
    
    if gradual_decay:
        return "ADVISER"
        
    if s_rat_ema >= 0.8 or s_inv_penalties >= 5.0:
        return "MENTOR"
        
    return "FACILITATOR"

@router.post('/track-message')
async def track_message(request: Request):
    data = await request.json()
    session_id = data.get('sessionId', 'unknown_session')
    message = data.get('message', {})
    
    if not message.get('timestamp'):
        message['timestamp'] = datetime.now(timezone.utc).isoformat()
        
    if session_id not in chat_sessions:
        chat_sessions[session_id] = {
            'messages': [],
            'unsaved_count': 0,
            'last_save': datetime.now(timezone.utc)
        }
        
        if db:
            existing_chat = chat_logs_col.find_one({'session_id': session_id})
            if existing_chat and 'messages' in existing_chat:
                chat_sessions[session_id]['messages'] = existing_chat['messages']
            
            existing_plan = semantic_plans_col.find_one({'session_id': session_id})
            if existing_plan:
                existing_plan.pop('_id', None)
                semantic_plans[session_id] = existing_plan
                
    chat_sessions[session_id]['messages'].append(message)
    chat_sessions[session_id]['unsaved_count'] += 1
    
    # Process Metrics
    current_plan = get_or_reset_plan(session_id)
    signal_state = current_plan['signal_state']
    
    # Rat EMA Update
    u_action = 1.0 if current_plan.get('used_ai_next_msg', False) else 0.0
    signal_state['s_rat_ema'] = (u_action * ALPHA_RAT) + (signal_state['s_rat_ema'] * (1 - ALPHA_RAT))
    
    if current_plan.get('used_ai_next_msg'):
        current_plan['used_ai_next_msg'] = False 
        signal_state['consecutive_healthy_messages'] = 0
    else:
        signal_state['consecutive_healthy_messages'] += 1
        
    # Process Decay
    process_decay(current_plan)
    
    messages = chat_sessions[session_id]['messages']
    
    if len(messages) >= 2:
        msg = messages[-1]
        prev_msg = messages[-2]
        
        # Latency calculations
        try:
            ts1 = datetime.fromisoformat(prev_msg['timestamp'])
            ts2 = datetime.fromisoformat(msg['timestamp'])
            l_current = (ts2 - ts1).total_seconds()
        except:
            l_current = 0.0
            
        # Parity calculations
        chars_current = len(msg.get('text', ''))
        chars_prev = len(prev_msg.get('text', ''))
        mx = max(chars_current, chars_prev)
        mn = min(chars_current, chars_prev)
        p_current = (mn / mx) if mx > 0 else 0.0
        
        # Update Sliding Windows
        signal_state['s_lat_window'].append(l_current)
        signal_state['s_par_window'].append(p_current)
        if len(signal_state['s_lat_window']) > 5:
            signal_state['s_lat_window'].pop(0)
            signal_state['s_par_window'].pop(0)
            
        # Update EMA
        if len(messages) == 2:
            signal_state['h_lat_ema'] = l_current
            signal_state['h_par_ema'] = p_current 
        else:
            signal_state['h_lat_ema'] = (l_current * ALPHA_LAT) + (signal_state['h_lat_ema'] * (1 - ALPHA_LAT))
            signal_state['h_par_ema'] = (p_current * ALPHA_PAR) + (signal_state['h_par_ema'] * (1 - ALPHA_PAR))
    
    semantic_plans[session_id] = current_plan
    check_and_save(session_id)
    
    return {"status": "tracked"}

@router.post('/semantic-plan')
async def semantic_plan(request: Request):
    print("Starting semantic plan generation...")
    data = await request.json()
    session_id = data.get('sessionId', 'unknown_session')
    messages = data.get('messages', [])
    
    current_plan = get_or_reset_plan(session_id)
    is_fresh_start = not current_plan['context']['macro_summary']
    signal_state = current_plan['signal_state']
    
    message_count = len(messages)
    chat_log = "\n".join([f"User {msg.get('sender')}: {msg.get('text')}" for msg in messages])
    
    if is_fresh_start:
        instruction = "Create a fresh semantic plan from the conversation."
    else:
        instruction = "Update the existing semantic plan using the latest conversation."

    prompt = f"""
You are Agent 1 for a conversation copilot.
Task: {instruction}

Current strategy:
{json.dumps(current_plan['strategy'], indent=2, ensure_ascii=False)}

Current context:
{json.dumps(current_plan['context'], indent=2, ensure_ascii=False)}

Conversation:
{chat_log}

Return only JSON with this shape:
{{
  "macro_summary": "short conversation summary",
  "strategic_intent": "what the assistant should optimize for",
  "theme": "current conversational theme",
  "action_plan": "next useful coaching action",
  "dynamic_content_bounds": ["short safety or tone constraint"],
  "knowledge_graph_triples": [
    {{"subject": "user", "predicate": "LIKES", "object": "example"}}
  ]
}}
"""
    
    try:
        response = openai_client.chat.completions.create(
            model=ollama_model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        # Handle formatting weirdness
        res_text = response.choices[0].message.content.strip()
        if res_text.startswith("```json"): res_text = res_text[7:]
        if res_text.endswith("```"): res_text = res_text[:-3]


        updated_semantic = json.loads(res_text)
        
        # Update Structure
        current_plan['context']['macro_summary'] = updated_semantic.get('macro_summary', '')
        current_plan['strategy']['strategic_intent'] = updated_semantic.get('strategic_intent', '')
        current_plan['strategy']['theme'] = updated_semantic.get('theme', '')
        current_plan['strategy']['action_plan'] = updated_semantic.get('action_plan', '')
        bounds = updated_semantic.get('dynamic_content_bounds', [])
        current_plan['strategy']['dynamic_content_bounds'] = bounds if isinstance(bounds, list) else []

        # Extract and print Knowledge Graph Triples
        print("\n--- Updated Semantic Plan ---")
        print(json.dumps(updated_semantic, indent=2, ensure_ascii=False))
        print("-----------------------------\n")
        
        triples = updated_semantic.get('knowledge_graph_triples', [])
        print("\n--- Extracted Knowledge Graph Triples ---")
        if isinstance(triples, list):
            for t in triples:
                if isinstance(t, dict):
                    print(f"{t.get('subject', '?')} --[{t.get('predicate', '?')}]--> {t.get('object', '?')}")
        print("-----------------------------------------\n")

        # 6. Determine Role
        new_role = determine_role(message_count, current_plan)
        
        current_plan['previous_role'] = new_role
        current_plan['current_role'] = new_role
        
        current_plan['last_updated'] = datetime.now(timezone.utc).isoformat()
        semantic_plans[session_id] = current_plan
        
        print("Updated Plan saved to DB:", current_plan)
        
        out_plan = current_plan.copy()
        out_plan['current_role'] = new_role
        out_plan['knowledge_graph_triples'] = triples if isinstance(triples, list) else []
        
        return out_plan
    except Exception as e:
        print("Error generating semantic plan:", e)
        return {"error": str(e)}

@router.post('/generate-suggestion')
async def generate_suggestion(request: Request):
    if client is None:
        return {
            "error": "GEMINI_API_KEY is not configured",
            "ui_nudge": "",
            "audit_trail": "AI Gen is mounted, but Gemini client is disabled because GEMINI_API_KEY is missing.",
        }

    data = await request.json()
    session_id = data.get('sessionId', 'unknown_session')
    messages = data.get('messages', [])
    t_invoke = data.get('t_invoke', 999.0)
    input_text = data.get('input_text', '').strip()
    force_assist = data.get('force_assist', False)
    
    plan = get_or_reset_plan(session_id)
    signal_state = plan['signal_state']
    
    # Mark that AI was invoked this turn
    plan['used_ai_next_msg'] = True
    
    # Process S_inv Penalties
    is_mashing = bool(re.search(r'(.)\1{4,}', input_text)) 
    if t_invoke < 5.0 and (len(input_text) < 3 or is_mashing):
        signal_state['s_inv_penalties'] += 1
        print(f"Zero-Keystroke Invocation detected ({t_invoke:.1f}s)! Penalty up to {signal_state['s_inv_penalties']:.1f}")
        
    # Re-evaluate role on the fly
    new_role = determine_role(len(messages), plan)
    
    current_role = new_role
    plan['current_role'] = current_role

    if force_assist:
        print("Force Assist triggered! Escalating to ADVISER for one turn.")
        current_role = "ADVISER"

    dynamic_bounds = plan['strategy'].get('dynamic_content_bounds', [])
    recent_messages = messages[-5:] if len(messages) > 5 else messages
    chat_log = "\n".join([f"User {msg.get('sender')}: {msg.get('text')}" for msg in recent_messages])
    
    forbidden_words = ["suicide", "kill", "harm"]
    if any(word in chat_log.lower() for word in forbidden_words):
        return {
            "ui_nudge": "This message may need care. Slow down and use a safer, more respectful reply.",
            "audit_trail": "Local safety keyword fallback triggered.",
        }
        
    prompt = f"""
{AGENT2_SYSTEM_PROMPT}

Current role: {current_role}
Dynamic bounds:
{json.dumps(dynamic_bounds, indent=2, ensure_ascii=False)}

Recent conversation:
{chat_log}

Return only JSON:
{{
  "ui_nudge": "short suggested reply or coaching nudge",
  "audit_trail": "brief reason for the suggestion"
}}
"""
    
    try:
        response = client.models.generate_content(
            model='gemini-flash-lite-latest',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
        res_text = response.text.strip()
        if res_text.startswith("```json"): res_text = res_text[7:]
        if res_text.endswith("```"): res_text = res_text[:-3]

        result = json.loads(res_text)
        print(f"Agent 2 Generated Nudge (Role: {current_role}):", result)
        
        semantic_plans[session_id] = plan
        check_and_save(session_id)
            
        return result
    except Exception as e:
        print("Error generating UI nudge:", e)
        return {"error": str(e)}
