import json
import os
from neo4j import GraphDatabase
from pathlib import Path
from dotenv import load_dotenv

# 優先載入頂層統一 .env
_top_env = Path(__file__).resolve().parent.parent / ".env"
if _top_env.exists():
    load_dotenv(dotenv_path=_top_env)
else:
    env_path = Path(__file__).parent / ".env"
    load_dotenv(dotenv_path=env_path)

from fastapi import APIRouter
from pydantic import BaseModel
from matchmaker import MatchmakerAgent

# 改用 APIRouter（掛載時由主程式指定前綴）
router = APIRouter()

# 初始化我們的媒婆大腦
agent = MatchmakerAgent()

# 定義接收的資料格式
class MatchRequest(BaseModel):
    target_user: dict
    candidates: list
    target_deep_profile: dict = {}

def get_user_graph_memory(user_id: str) -> str:
    """從 Neo4j 讀取使用者的偏好與地雷"""
    URI = os.getenv("NEO4J_URI")
    AUTH = (os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD"))
    DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")
    
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            driver.verify_connectivity()
            print(f"✅ [Neo4j 讀取] 連線驗證成功 (user={user_id})")
            with driver.session(database=DATABASE) as session:
                query = """
                MATCH (u:User {id: $user_id})-[r:HAS_PREFERENCE]->(t:Trait)
                RETURN r.type AS type, t.name AS trait, r.reason AS reason
                """
                result = session.run(query, user_id=user_id)
                
                memory_lines = []
                for record in result:
                    # 例如：[DISLIKES_TRAIT] 遇到特質「高外向」的對象。原因：...
                    memory_lines.append(f"[{record['type']}] 遇到特質「{record['trait']}」的對象。原因：{record['reason']}")
                
                if memory_lines:
                    return "\n".join(memory_lines)
                else:
                    return "目前圖庫中尚無該使用者的偏好或地雷紀錄。"
    except Exception as e:
        print(f"❌ Neo4j 讀取失敗: {e}")
        return "無法讀取過往記憶。"

def get_global_rules() -> str:
    """從 Neo4j 讀取 Top 3 高權重的全域配對法則"""
    URI = os.getenv("NEO4J_URI")
    AUTH = (os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD"))
    DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")
    
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            driver.verify_connectivity()
            print("✅ [Neo4j 全域法則] 連線驗證成功")
            with driver.session(database=DATABASE) as session:
                query = """
                MATCH (a:Agent {name: "System"})-[r:LEARNED_RULE]->(rule:GlobalRule)
                RETURN rule.content AS content, rule.category AS category, r.weight AS weight
                ORDER BY r.weight DESC
                LIMIT 3
                """
                result = session.run(query)
                
                rules = []
                for record in result:
                    rules.append(f"- [{record['category']}] {record['content']} (信心度：{record['weight']})")
                
                if rules:
                    return "\n".join(rules)
                else:
                    return ""
    except Exception as e:
        print(f"❌ Neo4j 讀取全域法則失敗: {e}")
        return ""


# ========================================
# 🔑 核心業務邏輯（供內部直接呼叫）
# ========================================

def do_match(target_user: dict, candidates: list, target_deep_profile: dict = None) -> dict:
    """核心配對邏輯，可被其他模組直接 import 呼叫"""
    print("📥 收到配對請求！")
    print("🧠 媒婆正在閱讀卷宗與圖譜記憶、進行多維度思考中...")
    
    # 1. 喚醒圖譜記憶（個體地雷）
    graph_memory = get_user_graph_memory(target_user.get("user_id"))
    print(f"📂 提煉出的記憶：\n{graph_memory}")
    
    # 2. 喚醒全域經驗法則
    global_heuristics = get_global_rules()
    if global_heuristics:
        print(f"🌐 全域法則：\n{global_heuristics}")
    
    # 3. 交給 Agent 決策
    raw_response = agent.match(target_user, candidates, graph_memory, global_heuristics, target_deep_profile or {})
    
    try:
        clean_response = raw_response.strip("` \n")
        if clean_response.lower().startswith("json"):
            clean_response = clean_response[4:].strip()
            
        parsed_data = json.loads(clean_response)
        
        if "matches" in parsed_data and isinstance(parsed_data["matches"], list):
            match_ids = [m.get("matched_user_id", "?") for m in parsed_data["matches"]]
            print(f"✅ 雙黃蛋配對成功！選中的是: {match_ids}")
            return parsed_data
        else:
            print(f"⚠️ LLM 回傳舊格式，自動包裝為 matches 陣列")
            single_match = {
                "matched_user_id": parsed_data.get("matched_user_id", "未知"),
                "contrast_label": "候選人",
                "recommendation_reason": parsed_data.get("recommendation_reason", ""),
                "receiver_reason": parsed_data.get("receiver_reason", parsed_data.get("recommendation_reason", "")),
                "distinctive_tags": parsed_data.get("distinctive_tags", [])
            }
            return {"matches": [single_match]}
        
    except json.JSONDecodeError:
        print("⚠️ 媒婆沒有照格式輸出 JSON，啟動防呆機制！")
        print(f"原始回覆: {raw_response}")
        fallback_matches = []
        for i, c in enumerate(candidates[:2]):
            fallback_matches.append({
                "matched_user_id": c.get("user_id", f"未知_{i}"),
                "contrast_label": f"候選人 {chr(65+i)}",
                "recommendation_reason": raw_response,
                "receiver_reason": raw_response,
                "distinctive_tags": []
            })
        return {"matches": fallback_matches}


def do_feedback(user_id: str, target_id: str, action: str, target_traits: dict, explicit_reasons: list = None) -> dict:
    """核心回饋邏輯，可被其他模組直接 import 呼叫"""
    print(f"📥 媒婆收到回報：{user_id} 對 {target_id} 選擇了 {action}")
    
    if user_id not in agent_memory_db:
        agent_memory_db[user_id] = {"history": [], "agent_reflection": "目前無特殊偏好。"}
        
    agent_memory_db[user_id]["history"].append({
        "action": action,
        "target_traits": target_traits,
        "explicit_reasons": explicit_reasons or []
    })
    
    if len(agent_memory_db[user_id]["history"]) >= 1:
        print("🤔 媒婆正在翻閱歷史紀錄，進行深度反思...")
        print(f"📋 [Debug] user_id={user_id}, history_count={len(agent_memory_db[user_id]['history'])}, explicit_reasons={explicit_reasons}")
        
        history_text = ""
        for item in agent_memory_db[user_id]["history"]:
            reasons_str = ""
            if item.get("explicit_reasons"):
                reasons_str = f" | 明確婉拒原因：{', '.join(item['explicit_reasons'])}"
            history_text += f"- 行動：{item['action']} | 對方性格：{item['target_traits']}{reasons_str}\n"
            
        latest_explicit_reasons = explicit_reasons if explicit_reasons else []
        print(f"📋 [Debug] latest_explicit_reasons 傳入 LLM: {latest_explicit_reasons}")
        
        try:
            raw_reflection_json = agent.generate_graph_reflection(history_text, explicit_reasons=latest_explicit_reasons)
            print(f"🧠 大腦萃取出的原始 JSON：\n{raw_reflection_json}")
            
            clean_json_str = raw_reflection_json.strip("` \n")
            if clean_json_str.lower().startswith("json"):
                clean_json_str = clean_json_str[4:].strip()
                
            reflection_data = json.loads(clean_json_str)
            
            URI = os.getenv("NEO4J_URI")
            USERNAME = os.getenv("NEO4J_USERNAME")
            PASSWORD = os.getenv("NEO4J_PASSWORD")
            DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")
            
            print("\n🔍 [Debug] 正在檢查 Neo4j 連線金鑰...")
            print(f"   - URI 狀態: {URI}")
            print(f"   - USERNAME 狀態: {USERNAME}")
            print(f"   - DATABASE 狀態: {DATABASE}")
            if PASSWORD is None:
                print("   - ❌ 嚴重錯誤：PASSWORD 讀取不到 (值為 None)！")
            else:
                print(f"   - ✅ PASSWORD 讀取成功，字元長度: {len(PASSWORD)}")
            print("==================================\n")
            
            AUTH = (USERNAME, PASSWORD)
            
            with GraphDatabase.driver(URI, auth=AUTH) as driver:
                driver.verify_connectivity()
                print("✅ [Neo4j 寫入] 連線驗證成功！")
                with driver.session(database=DATABASE) as session:
                    relationships = reflection_data.get("relationships", [])
                    if not relationships:
                        print("⚠️ LLM 回傳的 relationships 為空，沒有地雷需要寫入")
                    for rel in relationships:
                        required_keys = ["trait", "relation_type"]
                        missing_keys = [k for k in required_keys if k not in rel or not rel[k]]
                        if missing_keys:
                            print(f"❌ Neo4j 寫入失敗: LLM 輸出缺少必要欄位 {missing_keys}，原始資料: {rel}")
                            continue
                        
                        trait_value = rel["trait"]
                        rel_type_value = rel["relation_type"]
                        reason_value = rel.get("reason", "")
                        
                        print(f"   🔹 準備寫入: user_id={user_id}, trait={trait_value}, rel_type={rel_type_value}, reason={reason_value}")
                        
                        try:
                            cypher_query = """
                            MERGE (u:User {id: $user_id})
                            MERGE (t:Trait {name: $trait})
                            MERGE (u)-[r:HAS_PREFERENCE {type: $rel_type, reason: $reason}]->(t)
                            """
                            result = session.run(cypher_query, 
                                        user_id=user_id,
                                        trait=trait_value,
                                        rel_type=rel_type_value,
                                        reason=reason_value)
                            result.consume()
                            print(f"✨ 成功在資料庫畫出泡泡：({user_id}) -[{rel_type_value}]-> ({trait_value})")
                        except Exception as neo4j_err:
                            print(f"❌ Neo4j 寫入失敗: {neo4j_err}")
                            print(f"   失敗的參數: user_id={user_id}, trait={trait_value}, rel_type={rel_type_value}")

            agent_memory_db[user_id]["history"] = [] 
            
        except json.JSONDecodeError as json_err:
            print(f"❌ LLM 回傳 JSON 解析失敗: {json_err}")
            print(f"   原始回覆: {raw_reflection_json}")
        except Exception as e:
            print(f"❌ Neo4j 寫入失敗: {e}")
            
    return {"status": "success", "message": "媒婆已將此事記在心上。"}


def do_global_reflection(from_big_five: dict, from_context: str, to_big_five: dict, to_context: str) -> dict:
    """核心全域反思邏輯，可被其他模組直接 import 呼叫"""
    print("🌐 收到全域反思請求！正在從成功配對中歸納通用法則...")
    
    try:
        raw_response = agent.generate_global_reflection(
            from_big_five=from_big_five,
            from_context=from_context,
            to_big_five=to_big_five,
            to_context=to_context
        )
        print(f"🧠 全域反思原始回覆：\n{raw_response}")
        
        clean_response = raw_response.strip("` \n")
        if clean_response.lower().startswith("json"):
            clean_response = clean_response[4:].strip()
            
        reflection_data = json.loads(clean_response)
        abstract_rule = reflection_data.get("abstract_rule", "")
        category = reflection_data.get("category", "情境型")
        
        if not abstract_rule:
            print("⚠️ LLM 未回傳有效的抽象法則，跳過寫入")
            return {"status": "skipped", "message": "無法歸納出有效的配對法則"}
        
        print(f"✨ 歸納出法則：[{category}] {abstract_rule}")
        
        URI = os.getenv("NEO4J_URI")
        AUTH = (os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD"))
        DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")
        
        try:
            with GraphDatabase.driver(URI, auth=AUTH) as driver:
                driver.verify_connectivity()
                print("✅ [Neo4j 全域法則寫入] 連線驗證成功！")
                with driver.session(database=DATABASE) as session:
                    cypher_query = """
                    MERGE (a:Agent {name: "System"})
                    MERGE (rule:GlobalRule {content: $abstract_rule})
                    ON CREATE SET rule.category = $category
                    MERGE (a)-[r:LEARNED_RULE]->(rule)
                    ON CREATE SET r.weight = 1
                    ON MATCH SET r.weight = r.weight + 1
                    """
                    result = session.run(cypher_query,
                               abstract_rule=abstract_rule,
                               category=category)
                    result.consume()
                    print(f"✅ 全域法則已寫入/更新：[{category}] {abstract_rule}")
        except Exception as neo4j_err:
            print(f"❌ Neo4j 寫入失敗 (全域法則): {neo4j_err}")
            return {"status": "error", "message": f"Neo4j 寫入失敗: {neo4j_err}"}
        
        return {"status": "success", "abstract_rule": abstract_rule, "category": category}
        
    except json.JSONDecodeError as e:
        print(f"⚠️ 全域反思 JSON 解析失敗：{e}")
        print(f"原始回覆：{raw_response}")
        return {"status": "error", "message": "JSON 解析失敗"}
    except Exception as e:
        print(f"⚠️ 全域反思處理失敗：{e}")
        return {"status": "error", "message": str(e)}


# ========================================
# 🌐 API 路由（對外暴露，也可直接呼叫上方函數）
# ========================================

@router.post("/match")
async def match_endpoint(req: MatchRequest):
    return do_match(req.target_user, req.candidates, req.target_deep_profile)


class FeedbackRequest(BaseModel):
    user_id: str
    target_id: str # noqa
    action: str # "accept" 或 "decline"
    target_traits: dict # 對方的性格
    explicit_reasons: list[str] = []  # 使用者明確勾選的婉拒特質

# 這個變數暫時用來模擬 Agent 的記憶庫 (實戰中會存在向量資料庫或寫回 MongoDB)
agent_memory_db = {} 

@router.post("/feedback")
async def receive_feedback(req: FeedbackRequest):
    return do_feedback(req.user_id, req.target_id, req.action, req.target_traits, req.explicit_reasons)


class GlobalReflectionRequest(BaseModel):
    from_big_five: dict
    from_context: str = ""
    to_big_five: dict
    to_context: str = ""

@router.post("/global_reflection")
async def global_reflection_endpoint(req: GlobalReflectionRequest):
    return do_global_reflection(req.from_big_five, req.from_context, req.to_big_five, req.to_context)


if __name__ == "__main__":
    from fastapi import FastAPI
    import uvicorn
    app = FastAPI()
    app.include_router(router, prefix="/matchmaker")
    uvicorn.run(app, host="127.0.0.1", port=9001)
