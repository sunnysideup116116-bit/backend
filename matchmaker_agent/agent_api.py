import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from neo4j import GraphDatabase
from pathlib import Path
from dotenv import load_dotenv

# 撘瑕?? agent_api.py ??函????.env 瑼?
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from matchmaker import MatchmakerAgent

# ????FastAPI ?蝔? (撠望????銝?鈭?)
app = FastAPI()


@app.get("/health")
def health_check():
    """Process-level readiness endpoint; does not read profiles or Neo4j."""
    return {"status": "ok", "service": "matchmaker"}

# ??????慦?憭扯
agent = MatchmakerAgent()

GLOBAL_RULE_LIMIT = max(0, min(int(os.getenv("MATCH_GLOBAL_RULE_LIMIT", "2")), 5))
GLOBAL_RULE_CHAR_LIMIT = max(10, min(int(os.getenv("MATCH_GLOBAL_RULE_CHAR_LIMIT", "30")), 140))
GLOBAL_RULE_SIMILARITY_THRESHOLD = max(
    0.0, min(float(os.getenv("MATCH_GLOBAL_RULE_SIMILARITY_THRESHOLD", "0.38")), 1.0)
)


def compact_global_rule(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(cleaned) <= GLOBAL_RULE_CHAR_LIMIT:
        return cleaned
    return cleaned[:GLOBAL_RULE_CHAR_LIMIT].rstrip("，。；、 ") + "..."


def normalize_rule_text(text: str) -> str:
    return re.sub(r"[\s，。；、,.!?！？:：()（）「」『』\[\]【】\-]+", "", str(text or "").lower())


def rule_ngrams(text: str, n: int = 2) -> set[str]:
    normalized = normalize_rule_text(text)
    if not normalized:
        return set()
    if len(normalized) <= n:
        return {normalized}
    return {normalized[i:i + n] for i in range(len(normalized) - n + 1)}


def rule_similarity(first: str, second: str) -> float:
    first_grams = rule_ngrams(first)
    second_grams = rule_ngrams(second)
    if not first_grams or not second_grams:
        return 0.0
    overlap = len(first_grams & second_grams)
    return overlap / max(len(first_grams), len(second_grams))


def find_similar_global_rule(session, abstract_rule: str, category: str) -> dict | None:
    result = session.run(
        """
        MATCH (rule:GlobalRule)
        RETURN elementId(rule) AS element_id,
               rule.content AS content,
               rule.category AS category
        """
    )
    best = None
    for record in result:
        candidate_content = record["content"] or ""
        candidate_category = record["category"] or ""
        score = rule_similarity(abstract_rule, candidate_content)
        if category and candidate_category == category:
            score += 0.08
        if not best or score > best["similarity"]:
            best = {
                "element_id": record["element_id"],
                "content": candidate_content,
                "category": candidate_category,
                "similarity": score,
            }
    if best and best["similarity"] >= GLOBAL_RULE_SIMILARITY_THRESHOLD:
        return best
    return None


def parse_json_object_from_text(raw_text: str) -> dict:
    raw = (raw_text or "").strip().strip("` \n")
    if raw.lower().startswith("json"):
        raw = raw[4:].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
        raise

class MatchRequest(BaseModel):
    target_user: dict
    candidates: list
    target_deep_profile: dict = {}

def get_user_graph_memory(user_id: str) -> str:
    """Read one user preference memory from Neo4j."""
    step_start = time.perf_counter()
    uri = os.getenv("NEO4J_URI")
    auth = (os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD"))
    database = os.getenv("NEO4J_DATABASE", "neo4j")
    try:
        with GraphDatabase.driver(uri, auth=auth) as driver:
            driver.verify_connectivity()
            print(f"✅ [Neo4j 讀取] 連線驗證成功 (user={user_id})")
            with driver.session(database=database) as session:
                result = session.run(
                    """
                    MATCH (u:User {id: $user_id})-[r:HAS_PREFERENCE]->(t:Trait)
                    WHERE coalesce(r.active, true) = true
                    RETURN coalesce(r.type, CASE WHEN r.stance IN ['dislike','avoid'] THEN 'DISLIKES_TRAIT' ELSE 'LIKES_TRAIT' END) AS type,
                           t.name AS trait, coalesce(r.reason, '') AS reason
                    """,
                    user_id=user_id,
                )
                memory_lines = [
                    f"[{record['type']}] 遇到特質「{record['trait']}」的對象。原因：{record['reason']}"
                    for record in result
                ]
        elapsed = time.perf_counter() - step_start
        print(f"[TIMING][9001 /api/match] Neo4j user memory: {elapsed:.3f}s lines={len(memory_lines)}")
        return "\n".join(memory_lines) if memory_lines else "目前圖庫中尚無該使用者的偏好或地雷紀錄。"
    except Exception as e:
        print(f"[TIMING][9001 /api/match] Neo4j user memory failed after {time.perf_counter() - step_start:.3f}s")
        print(f"Neo4j user memory failed: {e}")
        return "無法讀取圖譜記憶。"


def get_global_rules() -> str:
    """Read top global learned rules from Neo4j."""
    step_start = time.perf_counter()
    uri = os.getenv("NEO4J_URI")
    auth = (os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD"))
    database = os.getenv("NEO4J_DATABASE", "neo4j")
    try:
        with GraphDatabase.driver(uri, auth=auth) as driver:
            driver.verify_connectivity()
            print("✅ [Neo4j 全域法則] 連線驗證成功")
            with driver.session(database=database) as session:
                result = session.run(
                    """
                    MATCH (a:Agent {name: "System"})-[r:LEARNED_RULE]->(rule:GlobalRule)
                    RETURN rule.content AS content, rule.category AS category, r.weight AS weight
                    ORDER BY r.weight DESC
                    LIMIT $limit
                    """,
                    limit=GLOBAL_RULE_LIMIT,
                )
                rules = [
                    f"- [{record['category']}] {compact_global_rule(record['content'])} (w={record['weight']})"
                    for record in result
                ]
        elapsed = time.perf_counter() - step_start
        print(f"[TIMING][9001 /api/match] Neo4j global rules: {elapsed:.3f}s rules={len(rules)}")
        return "\n".join(rules)
    except Exception as e:
        print(f"[TIMING][9001 /api/match] Neo4j global rules failed after {time.perf_counter() - step_start:.3f}s")
        print(f"Neo4j global rules failed: {e}")
        return ""

@app.post("/api/match")
async def match_endpoint(req: MatchRequest):
    total_start = time.perf_counter()
    print(
        f"[TIMING][9001 /api/match] start target={req.target_user.get('user_id')} "
        f"candidates={len(req.candidates)} model={agent.model}"
    )
    print("📥 收到 V1 系統傳來的配對請求！")
    print("🧠 媒婆正在閱讀卷宗與圖譜記憶、進行多維度思考中...")
    
    # 1-2. Neo4j reads are independent, so fetch target memory,
    # candidate memories, and global rules in parallel.
    step_start = time.perf_counter()
    target_user_id = req.target_user.get("user_id")
    candidate_ids = [candidate.get("user_id") for candidate in req.candidates]
    max_workers = max(2, min(8, len(candidate_ids) + 2))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        target_future = executor.submit(get_user_graph_memory, target_user_id)
        global_future = executor.submit(get_global_rules)
        candidate_futures = {
            executor.submit(get_user_graph_memory, candidate_id): candidate_id
            for candidate_id in candidate_ids
        }

        graph_memory = target_future.result()
        global_heuristics = global_future.result()
        candidate_memory_by_id = {}
        for future in as_completed(candidate_futures):
            candidate_memory_by_id[candidate_futures[future]] = future.result()

    print(f"[TIMING][9001 /api/match] parallel Neo4j reads wrapper: {time.perf_counter() - step_start:.3f}s")
    print(f"📂 提煉出的記憶：\n{graph_memory}")

    enriched_candidates = []
    for candidate in req.candidates:
        enriched = dict(candidate)
        enriched["graph_memory"] = candidate_memory_by_id.get(candidate.get("user_id"), "")
        enriched_candidates.append(enriched)

    if global_heuristics:
        print(f"🌐 全域法則：\n{global_heuristics}")
    
    # 3. Agent decision with target graph memory + candidate graph memories + global rules.
    step_start = time.perf_counter()
    raw_response = agent.match(
        req.target_user, enriched_candidates, graph_memory,
        global_heuristics, req.target_deep_profile
    )
    print(f"[TIMING][9001 /api/match] agent.match LLM wrapper: {time.perf_counter() - step_start:.3f}s raw_chars={len(raw_response) if raw_response else 0}")
    
    try:
        step_start = time.perf_counter()
        # ?岫??憍??店閫????皞???(Dict)
        clean_response = raw_response.strip("` \n")
        if clean_response.lower().startswith("json"):
            clean_response = clean_response[4:].strip()
            
        parsed_data = json.loads(clean_response)

        if parsed_data.get("error"):
            # A provider/runtime failure is not evidence that no candidate is
            # suitable.  Surface a service failure so the caller can preserve
            # the distinction in match state and user messaging.
            raise HTTPException(status_code=502, detail="Matchmaker provider failed")
        
        # ?? ?????舀 matches ????澆?
        if parsed_data.get("outcome") == "no_suitable_candidate":
            return {"outcome": "no_suitable_candidate", "matches": []}
        if "matches" in parsed_data and isinstance(parsed_data["matches"], list):
            parsed_data["matches"] = parsed_data["matches"][:1]
            if not parsed_data["matches"]:
                raise HTTPException(status_code=502, detail="Invalid matchmaker response")
            parsed_data["outcome"] = "selected"
            match_ids = [m.get("matched_user_id", "?") for m in parsed_data["matches"]]
            print(f"Agent matched ids: {match_ids}")
            print(f"[TIMING][9001 /api/match] parse/return matches: {time.perf_counter() - step_start:.3f}s")
            print(f"[TIMING][9001 /api/match] total: {time.perf_counter() - total_start:.3f}s")
            return parsed_data
        else:
            print("⚠️ LLM output did not include a valid match.")
            raise HTTPException(status_code=502, detail="Invalid matchmaker response")
        
    except json.JSONDecodeError:
        print("⚠️ 媒婆沒有照格式輸出 JSON，啟動防呆機制！")
        print(f"原始回覆: {raw_response}")
        
        print(f"[TIMING][9001 /api/match] parse failed; no fallback candidate is created. total: {time.perf_counter() - total_start:.3f}s")
        raise HTTPException(status_code=502, detail="Invalid matchmaker response")



# ??agent_api.py 銝剜憓挾

class FeedbackRequest(BaseModel):
    user_id: str
    target_id: str # noqa
    action: str # "accept" ??"decline"
    target_traits: dict # 撠?扳
    explicit_reasons: list[str] = []  # 雿輻??蝣箏?貊?憍??寡釭

# ???豢?靘芋??Agent ???嗅澈 (撖行銝剜?摮??鞈?摨急?撖怠? MongoDB)
agent_memory_db = {} 

@app.post("/api/feedback")
async def receive_feedback(req: FeedbackRequest):
    print(f"📥 Agent 收到 Feedback: {req.user_id} -> {req.target_id}, action={req.action}")
    if req.user_id not in agent_memory_db:
        agent_memory_db[req.user_id] = {"history": [], "agent_reflection": "尚無反思"}

    agent_memory_db[req.user_id]["history"].append({
        "action": req.action,
        "target_traits": req.target_traits,
        "explicit_reasons": req.explicit_reasons,
    })

    history_text = ""
    for item in agent_memory_db[req.user_id]["history"]:
        reasons_str = ""
        if item.get("explicit_reasons"):
            reasons_str = f" | explicit_reasons: {', '.join(item['explicit_reasons'])}"
        history_text += f"- action: {item['action']} | target_traits: {item['target_traits']}{reasons_str}\n"

    try:
        raw_reflection_json = agent.generate_graph_reflection(
            history_text, explicit_reasons=req.explicit_reasons or []
        )
        print(f"🧠 Graph reflection raw JSON:\n{raw_reflection_json}")
        reflection_data = parse_json_object_from_text(raw_reflection_json)

        uri = os.getenv("NEO4J_URI")
        auth = (os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD"))
        database = os.getenv("NEO4J_DATABASE", "neo4j")
        relationships = reflection_data.get("relationships", []) or []
        if not relationships:
            print("⚠️ Graph reflection produced no relationships.")

        with GraphDatabase.driver(uri, auth=auth) as driver:
            driver.verify_connectivity()
            print("✅ [Neo4j Feedback 寫入] 連線驗證成功")
            with driver.session(database=database) as session:
                for rel in relationships:
                    trait_value = rel.get("trait")
                    rel_type_value = rel.get("relation_type")
                    reason_value = rel.get("reason", "")
                    if not trait_value or not rel_type_value:
                        print(f"⚠️ Skip invalid graph relationship: {rel}")
                        continue
                    session.run(
                        """
                        MERGE (u:User {id: $user_id})
                        MERGE (t:Trait {name: $trait})
                        MERGE (u)-[r:HAS_PREFERENCE {type: $rel_type}]->(t)
                        SET r.reason = $reason, r.active = true, r.updated_at = timestamp()
                        """,
                        user_id=req.user_id,
                        trait=trait_value,
                        rel_type=rel_type_value,
                        reason=reason_value,
                    )
                    print(f"✅ Neo4j saved: {req.user_id} -[{rel_type_value}]-> {trait_value}")

        agent_memory_db[req.user_id]["history"] = []
    except json.JSONDecodeError as json_err:
        print(f"❌ Graph reflection JSON parse failed: {json_err}")
        print(f"raw={locals().get('raw_reflection_json', '')}")
    except Exception as e:
        print(f"❌ Feedback graph write failed: {e}")

    return {"status": "success", "message": "Agent feedback processed"}


class GlobalReflectionRequest(BaseModel):
    from_big_five: dict
    from_context: str = ""
    to_big_five: dict
    to_context: str = ""

@app.post("/api/global_reflection")
async def global_reflection_endpoint(req: GlobalReflectionRequest):
    print("🌐 收到全域反思請求，正在歸納通用法則...")
    try:
        raw_response = agent.generate_global_reflection(
            from_big_five=req.from_big_five,
            from_context=req.from_context,
            to_big_five=req.to_big_five,
            to_context=req.to_context,
        )
        print(f"🧠 全域反思原始回覆:\n{raw_response}")
        reflection_data = parse_json_object_from_text(raw_response)
        abstract_rule = reflection_data.get("abstract_rule", "")
        category = reflection_data.get("category", "情境型")
        if not abstract_rule:
            return {"status": "skipped", "message": "沒有產生全域法則"}
        abstract_rule = compact_global_rule(abstract_rule)

        uri = os.getenv("NEO4J_URI")
        auth = (os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD"))
        database = os.getenv("NEO4J_DATABASE", "neo4j")
        with GraphDatabase.driver(uri, auth=auth) as driver:
            driver.verify_connectivity()
            print("✅ [Neo4j 全域法則寫入] 連線驗證成功")
            with driver.session(database=database) as session:
                similar = find_similar_global_rule(session, abstract_rule, category)
                if similar:
                    session.run(
                        """
                        MERGE (a:Agent {name: "System"})
                        MATCH (rule:GlobalRule)
                        WHERE elementId(rule) = $element_id
                        MERGE (a)-[r:LEARNED_RULE]->(rule)
                        ON CREATE SET r.weight = 1
                        ON MATCH SET r.weight = coalesce(r.weight, 0) + 1
                        SET rule.category = coalesce(rule.category, $category),
                            rule.last_observed = $abstract_rule,
                            rule.updated_at = timestamp()
                        """,
                        element_id=similar["element_id"],
                        abstract_rule=abstract_rule,
                        category=category,
                    ).consume()
                    print(
                        "✅ 全域法則已合併："
                        f"[{category}] {abstract_rule} -> {similar['content']} "
                        f"(similarity={similar['similarity']:.2f})"
                    )
                    return {
                        "status": "merged",
                        "abstract_rule": similar["content"],
                        "observed_rule": abstract_rule,
                        "category": category,
                        "similarity": round(similar["similarity"], 3),
                    }
                session.run(
                    """
                    MERGE (a:Agent {name: "System"})
                    MERGE (rule:GlobalRule {content: $abstract_rule})
                    ON CREATE SET rule.category = $category,
                                  rule.created_at = timestamp()
                    MERGE (a)-[r:LEARNED_RULE]->(rule)
                    ON CREATE SET r.weight = 1
                    ON MATCH SET r.weight = coalesce(r.weight, 0) + 1
                    SET rule.updated_at = timestamp()
                    """,
                    abstract_rule=abstract_rule,
                    category=category,
                ).consume()
        print(f"✅ 全域法則已寫入/更新：[{category}] {abstract_rule}")
        return {"status": "success", "abstract_rule": abstract_rule, "category": category}
    except json.JSONDecodeError as e:
        print(f"⚠️ 全域反思 JSON 解析失敗: {e}")
        print(f"raw={locals().get('raw_response', '')}")
        return {"status": "error", "message": "JSON parse failed"}
    except Exception as e:
        print(f"⚠️ 全域反思失敗: {e}")
        return {"status": "error", "message": str(e)}


# === Conversation-derived preference memory ===

class MemoryObserveRequest(BaseModel):
    user_id: str
    text: str
    surface: str = "global"
    match_id: str | None = None
    message_id: str | None = None

class MemoryApplyRequest(BaseModel):
    user_id: str
    memories: list[dict] = []
    surface: str = "global"
    match_id: str | None = None
    message_id: str | None = None

class MemoryActionRequest(BaseModel):
    user_id: str
    key: str
    action: str
    value: str | None = None

class ChatTripleRequest(BaseModel):
    session_id: str
    match_id: str | None = None
    participants: list[str] = []
    triples: list[dict] = []
    evidence_messages: list[dict] = []


def _neo4j_config():
    return (os.getenv("NEO4J_URI"), (os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD")), os.getenv("NEO4J_DATABASE", "neo4j"))

@app.post("/api/clear_graph")
async def clear_graph_endpoint():
    print("🧹 收到清空 Neo4j Graph 請求")
    URI, AUTH, DATABASE = _neo4j_config()
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            driver.verify_connectivity()
            with driver.session(database=DATABASE) as session:
                session.run("MATCH (n) DETACH DELETE n").consume()
        print("✅ Neo4j Graph 已清空")
        return {"status": "success", "message": "Neo4j Graph 已清空"}
    except Exception as e:
        print(f"❌ 清空 Neo4j Graph 失敗: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/memory/apply")
async def apply_memory(req: MemoryApplyRequest):
    """Write trusted, already-validated profile-memory proposals without model extraction."""
    allowed_stances = {"like", "dislike", "require", "avoid"}
    protected = re.compile(r"(?:黑人|白人|黃種人|種族|族裔|宗教|信仰|穆斯林|基督教|同性戀|性傾向|性別認同|跨性別|殘障|身心障礙|疾病|政治立場|國籍|公民身分)", re.I)
    key_re = re.compile(r"^[a-z][a-z0-9_]{1,50}$")
    now, clean = time.time(), []
    URI, AUTH, DATABASE = _neo4j_config()
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            with driver.session(database=DATABASE) as session:
                session.run("CREATE CONSTRAINT memory_observation_message_id IF NOT EXISTS FOR (o:MemoryObservation) REQUIRE o.message_id IS UNIQUE").consume()
                if req.message_id:
                    observed = session.run("""
                        MERGE (o:MemoryObservation {message_id:$message_id})
                        ON CREATE SET o.owner_user_id=$user_id,o.created_at=$now
                        RETURN o.created_at=$now AS created
                    """, message_id=req.message_id, user_id=req.user_id, now=now).single()
                    if not observed or not observed["created"]:
                        return {"memories": [], "status": "duplicate"}
                for item in req.memories[:3]:
                    key = str(item.get("key", "")).strip().lower().replace(" ", "_")
                    label = re.sub(r"^(?:喜歡|討厭|不喜歡|偏好|近期情境)\s*[:：,，]?\s*", "", str(item.get("label") or item.get("label_zh_tw") or "").strip())[:40]
                    stance = str(item.get("stance", ""))
                    confidence = float(item.get("confidence", 0))
                    if not key_re.match(key) or not label or protected.search(label) or stance not in allowed_stances or confidence < 0.75:
                        continue
                    category = str(item.get("category", "lifestyle"))[:30]
                    session.run("""
                        MERGE (u:User {id:$user_id}) MERGE (t:Trait {key:$key})
                        ON CREATE SET t.name=$label,t.category=$category
                        ON MATCH SET t.category=coalesce(t.category,$category)
                        MERGE (u)-[r:HAS_PREFERENCE]->(t)
                        ON CREATE SET r.first_seen_at=$now,r.evidence_count=0
                        SET r.stance=$stance,
                            r.type=CASE WHEN $stance IN ['dislike','avoid'] THEN 'DISLIKES_TRAIT' ELSE 'LIKES_TRAIT' END,
                            r.confidence=CASE WHEN coalesce(r.confidence,0)>$confidence THEN r.confidence ELSE $confidence END,
                            r.evidence_count=coalesce(r.evidence_count,0)+1,r.last_seen_at=$now,
                            r.active=true,r.source=$surface,r.match_id=$match_id,r.display_label_zh_tw=$label
                    """, user_id=req.user_id,key=key,label=label,category=category,stance=stance,
                         confidence=confidence,now=now,surface=req.surface,match_id=req.match_id).consume()
                    clean.append({"key":key,"label":label,"stance":stance,"category":category,"confidence":confidence,"last_seen_at":now})
        return {"memories": clean, "status": "success"}
    except Exception as exc:
        print(f"[MEMORY][9001 apply] graph_write_failed user={req.user_id} error={exc}")
        return {"memories": [], "status": "error", "error_code": type(exc).__name__}
@app.post("/api/memory/observe")
async def observe_memory(req: MemoryObserveRequest):
    """Persist owner-scoped durable preferences; message_id makes retries idempotent."""
    prompt = f"""
請從使用者本人的第一人稱訊息中萃取可長期記憶的偏好或地雷。
只輸出 JSON：
{{"memories":[{{"key":"snake_case_key","label":"2-8字繁體中文標籤","stance":"like|dislike|require|avoid","category":"lifestyle|habit|personality|relationship|activity","confidence":0.0}}]}}
規則：
- 只保留 confidence >= 0.90 的明確、長期偏好。
- 一般寒暄、單次行程、配對／邀請狀態、其他人資訊、帳號和敏感資訊不要記。
- label 不要加「喜歡：」「討厭：」「近期情境：」等前綴。
使用者訊息：{req.text}
"""
    try:
        response = agent.client.chat.completions.create(model=agent.model, messages=[
            {"role": "system", "content": "你是偏好記憶萃取器，只輸出 JSON，所有中文都使用繁體中文。"},
            {"role": "user", "content": prompt}], temperature=0.0)
        items = parse_json_object_from_text(response.choices[0].message.content or "").get("memories", [])
    except Exception as exc:
        print(f"[MEMORY][9001 observe] extraction_failed user={req.user_id} error={exc}")
        return {"memories": []}

    allowed_stances, clean, now = {"like", "dislike", "require", "avoid"}, [], time.time()
    URI, AUTH, DATABASE = _neo4j_config()
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            with driver.session(database=DATABASE) as session:
                if req.message_id:
                    observed = session.run("""
                        MERGE (o:MemoryObservation {message_id:$message_id})
                        ON CREATE SET o.owner_user_id=$user_id,o.created_at=$now
                        RETURN o.created_at=$now AS created
                    """, message_id=req.message_id, user_id=req.user_id, now=now).single()
                    if not observed or not observed["created"]:
                        return {"memories": [], "status": "duplicate"}
                for item in items[:3]:
                    key = str(item.get("key", "")).strip().lower().replace(" ", "_")
                    label = re.sub(r"^(?:喜歡|討厭|不喜歡|偏好|近期情境)\s*[:：,，]?\s*", "", str(item.get("label", "")).strip())[:40]
                    stance = item.get("stance")
                    confidence = float(item.get("confidence", 0))
                    if not key or not label or stance not in allowed_stances or confidence < 0.90:
                        continue
                    category = str(item.get("category", "lifestyle"))[:30]
                    session.run("""
                        MERGE (u:User {id:$user_id}) MERGE (t:Trait {key:$key})
                        ON CREATE SET t.name=$label,t.category=$category
                        ON MATCH SET t.category=coalesce(t.category,$category)
                        MERGE (u)-[r:HAS_PREFERENCE]->(t)
                        ON CREATE SET r.first_seen_at=$now,r.evidence_count=0
                        SET r.stance=$stance,
                            r.type=CASE WHEN $stance IN ['dislike','avoid'] THEN 'DISLIKES_TRAIT' ELSE 'LIKES_TRAIT' END,
                            r.confidence=CASE WHEN coalesce(r.confidence,0)>$confidence THEN r.confidence ELSE $confidence END,
                            r.evidence_count=coalesce(r.evidence_count,0)+1,r.last_seen_at=$now,
                            r.active=true,r.source=$surface,r.display_label_zh_tw=$label
                    """, user_id=req.user_id, key=key, label=label, category=category, stance=stance,
                         confidence=confidence, now=now, surface=req.surface).consume()
                    clean.append({"key": key, "label": label, "stance": stance, "category": category,
                                  "confidence": confidence, "last_seen_at": now})
        return {"memories": clean}
    except Exception as exc:
        print(f"[MEMORY][9001 observe] graph_write_failed user={req.user_id} error={exc}")
        return {"memories": []}

@app.get("/api/memory/{user_id}")
async def list_memories(user_id: str, limit: int = 12):
    try:
        URI, AUTH, DATABASE = _neo4j_config()
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            with driver.session(database=DATABASE) as session:
                rows = session.run("""
                    MATCH (u:User {id:$user_id})-[r:HAS_PREFERENCE]->(t:Trait)
                    WHERE coalesce(r.active,true)=true
                    RETURN coalesce(t.key,toLower(replace(t.name,' ','_'))) AS key,
                           coalesce(r.display_label_zh_tw,t.name) AS label,
                           coalesce(r.stance,CASE WHEN r.type='DISLIKES_TRAIT' THEN 'dislike' ELSE 'like' END) AS stance,
                           t.category AS category, coalesce(r.confidence,0.7) AS confidence,
                           coalesce(r.last_seen_at,0) AS last_seen_at
                    ORDER BY confidence DESC,last_seen_at DESC LIMIT $limit
                """, user_id=user_id, limit=max(1,min(limit,30)))
                return {"memories": [dict(row) for row in rows]}
    except Exception as exc:
        print(f"[MEMORY][9001] graph_read_failed user={user_id} error={exc}")
        return {"memories": []}

@app.post("/api/memory/action")
async def memory_action(req: MemoryActionRequest):
    if req.action not in {"disable","restore","correct"}:
        return {"status":"error","message":"unsupported action"}
    URI, AUTH, DATABASE = _neo4j_config()
    with GraphDatabase.driver(URI, auth=AUTH) as driver:
        with driver.session(database=DATABASE) as session:
            row = session.run("""
                MATCH (u:User {id:$user_id})-[r:HAS_PREFERENCE]->(t:Trait {key:$key})
                SET r.active=$active,r.last_seen_at=$now,
                    r.display_label_zh_tw=CASE WHEN $value IS NULL OR $value='' THEN coalesce(r.display_label_zh_tw,t.name) ELSE $value END
                RETURN t.key AS key,coalesce(r.display_label_zh_tw,t.name) AS label,r.stance AS stance,
                       t.category AS category,r.confidence AS confidence,r.last_seen_at AS last_seen_at
            """, user_id=req.user_id,key=req.key,active=req.action!='disable',now=time.time(),value=req.value).single()
            return {"status":"success","memory":dict(row) if row else None}
@app.post("/api/chat_triples")
async def receive_chat_triples(req: ChatTripleRequest):
    allowed = {
        "IS_A", "HAS", "LIKES", "DISLIKES", "WANTS", "FEELS", "KNOWS",
        "USES", "BELIEVES", "AGREES_WITH", "DISAGREES_WITH", "MENTIONED",
    }
    clean = []
    for item in (req.triples or [])[:16]:
        subject = str(item.get("subject", "")).strip()[:80]
        predicate = str(item.get("predicate", "")).strip().upper()
        obj = str(item.get("object", "")).strip()[:80]
        if subject and obj and predicate in allowed:
            clean.append({"subject": subject, "predicate": predicate, "object": obj})
    if not clean:
        return {"status": "skipped", "written": 0}

    URI, AUTH, DATABASE = _neo4j_config()
    now = time.time()
    evidence = [
        {
            "sender_id": str(message.get("sender_id", "")),
            "content": str(message.get("content", ""))[:240],
            "timestamp": message.get("timestamp"),
        }
        for message in (req.evidence_messages or [])[-12:]
    ]
    with GraphDatabase.driver(URI, auth=AUTH) as driver:
        with driver.session(database=DATABASE) as session:
            for user_id in req.participants or []:
                if user_id:
                    session.run("MERGE (:User {id:$user_id})", user_id=user_id).consume()
            for triple in clean:
                rel_type = triple["predicate"]
                session.run(
                    f"""
                    MERGE (s:ChatEntity {{key:$subject_key}})
                    ON CREATE SET s.name=$subject
                    SET s.name=$subject, s.updated_at=$now
                    MERGE (o:ChatEntity {{key:$object_key}})
                    ON CREATE SET o.name=$object
                    SET o.name=$object, o.updated_at=$now
                    MERGE (s)-[r:{rel_type}]->(o)
                    ON CREATE SET r.first_seen_at=$now, r.evidence_count=0
                    SET r.session_id=$session_id,
                        r.match_id=$match_id,
                        r.participants=$participants,
                        r.evidence=$evidence,
                        r.evidence_count=coalesce(r.evidence_count, 0) + 1,
                        r.last_seen_at=$now
                    """,
                    subject_key=triple["subject"].lower(),
                    subject=triple["subject"],
                    object_key=triple["object"].lower(),
                    object=triple["object"],
                    session_id=req.session_id,
                    match_id=req.match_id,
                    participants=[str(user_id) for user_id in (req.participants or []) if user_id],
                    evidence=evidence,
                    now=now,
                ).consume()
    print(f"[CHAT_TRIPLES] wrote {len(clean)} triples session={req.session_id}")
    return {"status": "success", "written": len(clean)}

@app.get("/api/chat_triples")
async def list_chat_triples(session_id: str, limit: int = 20):
    URI, AUTH, DATABASE = _neo4j_config()
    with GraphDatabase.driver(URI, auth=AUTH) as driver:
        with driver.session(database=DATABASE) as session:
            rows = session.run(
                """
                MATCH (s:ChatEntity)-[r]->(o:ChatEntity)
                WHERE r.session_id = $session_id
                RETURN s.name AS subject,
                       type(r) AS predicate,
                       o.name AS object,
                       coalesce(r.evidence_count, 0) AS evidence_count,
                       coalesce(r.last_seen_at, 0) AS last_seen_at
                ORDER BY last_seen_at DESC
                LIMIT $limit
                """,
                session_id=session_id,
                limit=max(1, min(limit, 50)),
            )
            triples = [dict(row) for row in rows]
    return {"triples": triples}


if __name__ == "__main__":
    import uvicorn
    # 霈?憍???9001 皜臬
    uvicorn.run(app, host="127.0.0.1", port=9001)

