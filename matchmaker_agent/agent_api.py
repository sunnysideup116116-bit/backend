import json
import os
import hashlib
import math
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


def destructive_tools_enabled() -> bool:
    return os.getenv("DEMO_DESTRUCTIVE_TOOLS_ENABLED", "off").strip().lower() in {"1", "true", "on"}

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

class ProactiveEventMatchRequest(BaseModel):
    user_id: str
    excluded_user_ids: list[str] = []

class EventIngestRequest(BaseModel):
    region: str = "高雄"
    window_days: int = 30
    max_events: int = 6
    search_results: list[dict] = []

class EventInventoryReconcileRequest(BaseModel):
    categories: list[str] = []
    max_per_category: int = 6

class EventRelevanceProjectionRequest(BaseModel):
    event_ids: list[str] = []
    links: list[dict] = []
    model: str = ""
    generated_at: float = 0.0

class ConceptEmbeddingProjectionRequest(BaseModel):
    concepts: list[dict] = []


def ingest_event_search_results(req: EventIngestRequest):
    safe_region = re.sub(r"\s+", " ", str(req.region or "").strip())[:40] or "高雄"
    bounded_results = []
    for item in (req.search_results or [])[:20]:
        if not isinstance(item, dict):
            continue
        bounded_results.append({
            "title": re.sub(r"\s+", " ", str(item.get("title") or "").strip())[:180],
            "snippet": re.sub(r"\s+", " ", str(item.get("snippet") or "").strip())[:1500],
            "source_url": str(item.get("source_url") or "").strip()[:500],
            "discovery_category": re.sub(
                r"\s+", " ", str(item.get("discovery_category") or "").strip()
            )[:30],
            "skill_name": re.sub(r"[^a-z0-9-]", "", str(item.get("skill_name") or "").lower())[:60],
            "skill_version": re.sub(r"[^0-9.]", "", str(item.get("skill_version") or ""))[:20],
            "region": safe_region,
        })
    if not bounded_results:
        return {"status": "empty", "region": safe_region, "ingested_count": 0, "events": []}
    # Expiry deletion belongs to the lifecycle worker. Running cleanup before
    # every category batch can make one discovery run mutate earlier batches.
    result = agent.extract_and_ingest_search_results(
        bounded_results,
        region=safe_region,
        window_days=max(1, min(int(req.window_days or 30), 60)),
        max_events=max(1, min(int(req.max_events or 6), 6)),
    )
    return {
        "status": "success",
        "region": safe_region,
        "search_count": len(bounded_results),
        "ingested_count": result.get("ingested_count", 0),
        "validation_counts": result.get("validation_counts", {}),
        "events": result.get("events", []),
    }


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
                    MATCH (u:User {id: $user_id})-[r:PREFERS|AVOIDS]->(c:Concept)
                    RETURN CASE type(r) WHEN 'AVOIDS' THEN 'DISLIKES_TRAIT' ELSE 'LIKES_TRAIT' END AS type,
                           coalesce(c.label, c.key) AS trait, '' AS reason
                    """,
                    user_id=user_id,
                )
                memory_lines = [
                    f"[{record['type']}] 偏好/地雷「{record['trait']}」"
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

        if not isinstance(parsed_data, dict):
            raise HTTPException(status_code=502, detail={"code": "matchmaker_invalid_response"})
        if parsed_data.get("error"):
            # A provider/runtime failure is not evidence that no candidate is
            # suitable.  Surface a service failure so the caller can preserve
            # the distinction in match state and user messaging.
            raise HTTPException(status_code=502, detail={"code": "matchmaker_provider_error"})
        
        # ?? ?????舀 matches ????澆?
        if parsed_data.get("outcome") == "no_suitable_candidate":
            return {"outcome": "no_suitable_candidate", "matches": []}
        if "matches" in parsed_data and isinstance(parsed_data["matches"], list):
            parsed_data["matches"] = parsed_data["matches"][:1]
            if not parsed_data["matches"]:
                raise HTTPException(status_code=502, detail={"code": "matchmaker_invalid_response"})
            if any(
                not isinstance(item, dict) or not isinstance(item.get("matched_user_id"), str)
                or not item.get("matched_user_id", "").strip()
                for item in parsed_data["matches"]
            ):
                raise HTTPException(status_code=502, detail={"code": "matchmaker_invalid_response"})
            parsed_data["outcome"] = "selected"
            match_ids = [m.get("matched_user_id", "?") for m in parsed_data["matches"]]
            print(f"Agent matched ids: {match_ids}")
            print(f"[TIMING][9001 /api/match] parse/return matches: {time.perf_counter() - step_start:.3f}s")
            print(f"[TIMING][9001 /api/match] total: {time.perf_counter() - total_start:.3f}s")
            return parsed_data
        else:
            print("⚠️ LLM output did not include a valid match.")
            raise HTTPException(status_code=502, detail={"code": "matchmaker_invalid_response"})
        
    except json.JSONDecodeError:
        print("⚠️ 媒婆沒有照格式輸出 JSON，啟動防呆機制！")
        print(f"原始回覆: {raw_response}")
        
        print(f"[TIMING][9001 /api/match] parse failed; no fallback candidate is created. total: {time.perf_counter() - total_start:.3f}s")
        raise HTTPException(status_code=502, detail="Invalid matchmaker response")



# ??agent_api.py 銝剜憓挾

@app.post("/api/proactive_event_match")
def proactive_event_match(req: ProactiveEventMatchRequest):
    user_id = re.sub(r"\s+", "", str(req.user_id or ""))[:80]
    if not user_id:
        return {"status": "error", "message": "user_id is required"}
    started = time.perf_counter()
    try:
        deleted = agent.clean_expired_events()
        excluded_user_ids = list(dict.fromkeys(
            re.sub(r"\s+", "", str(value or ""))[:80]
            for value in req.excluded_user_ids[:100]
            if str(value or "").strip()
        ))
        matches = agent.find_event_matches(user_id, excluded_user_ids=excluded_user_ids)
        if not matches:
            return {
                "status": "no_match",
                "user_id": user_id,
                "expired_events_deleted": deleted,
                "message": "目前沒有同時通過活動連結與地雷過濾的人選。",
            }
        selected = matches[0]
        invitation_order = agent.choose_event_invitation_order(selected)
        first_user_id = (
            selected.get("user_id")
            if invitation_order.get("first") == "target"
            else selected.get("candidate_id")
        )
        hook_match = selected
        if invitation_order.get("first") == "candidate":
            hook_match = {
                **selected,
                "user_id": selected.get("candidate_id"),
                "user_name": selected.get("candidate_name"),
                "candidate_id": selected.get("user_id"),
                "candidate_name": selected.get("user_name"),
                "target_links": selected.get("candidate_links") or [],
                "candidate_links": selected.get("target_links") or [],
                "target_user_concepts": selected.get("candidate_user_concepts") or [],
                "candidate_user_concepts": selected.get("target_user_concepts") or [],
                "target_source_kinds": selected.get("candidate_source_kinds") or [],
                "candidate_source_kinds": selected.get("target_source_kinds") or [],
            }
        hook = agent.generate_proactive_event_hook(first_user_id, hook_match)
        second_user_id = (
            selected.get("candidate_id")
            if first_user_id == selected.get("user_id")
            else selected.get("user_id")
        )
        second_hook_match = selected
        if second_user_id == selected.get("candidate_id"):
            second_hook_match = {
                **selected,
                "user_id": selected.get("candidate_id"),
                "user_name": selected.get("candidate_name"),
                "candidate_id": selected.get("user_id"),
                "candidate_name": selected.get("user_name"),
                "target_links": selected.get("candidate_links") or [],
                "candidate_links": selected.get("target_links") or [],
                "target_user_concepts": selected.get("candidate_user_concepts") or [],
                "candidate_user_concepts": selected.get("target_user_concepts") or [],
                "target_source_kinds": selected.get("candidate_source_kinds") or [],
                "candidate_source_kinds": selected.get("target_source_kinds") or [],
            }
        second_hook = agent.generate_proactive_event_hook(second_user_id, second_hook_match)
        print(
            f"[TIMING][9001 /api/proactive_event_match] total="
            f"{time.perf_counter() - started:.3f}s user={user_id}"
        )
        return {
            "status": "success",
            "user_id": user_id,
            "expired_events_deleted": deleted,
            "match": selected,
            "first_user_id": first_user_id,
            "second_user_id": second_user_id,
            "invitation_order": invitation_order,
            "hook": hook,
            "first_hook": hook,
            "second_hook": second_hook,
        }
    except Exception as exc:
        print(f"[PROACTIVE_EVENT] match failed user={user_id} error={exc}")
        return {"status": "error", "user_id": user_id, "message": str(exc)}


@app.post("/api/events/lifecycle/cleanup")
def cleanup_event_lifecycle():
    """Internal bounded cleanup; consent state remains owned by port 8000."""
    try:
        result = agent.clean_expired_events(include_ids=True)
        return {"status": "success", **result}
    except Exception as exc:
        print(f"[EVENT_LIFECYCLE] cleanup failed error={type(exc).__name__}")
        raise HTTPException(status_code=503, detail="Event lifecycle cleanup unavailable") from exc


@app.post("/api/events/ingest")
def ingest_events(req: EventIngestRequest):
    try:
        return ingest_event_search_results(req)
    except Exception as exc:
        print(f"[PROACTIVE_EVENT] event ingestion failed error={type(exc).__name__}")
        raise HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "error_code": type(exc).__name__,
                "message": "event_ingestion_failed",
            },
        ) from exc


@app.post("/api/events/reconcile")
def reconcile_event_inventory(req: EventInventoryReconcileRequest):
    """Internal Event-only reconciliation; User and preference nodes are untouched."""
    try:
        return agent.reconcile_event_inventory(
            categories=req.categories,
            max_per_category=req.max_per_category,
        )
    except Exception as exc:
        print(f"[PROACTIVE_EVENT] inventory reconcile failed error={type(exc).__name__}")
        raise HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "error_code": type(exc).__name__,
                "message": "event_inventory_reconcile_failed",
            },
        ) from exc


@app.get("/api/events/active")
def list_active_events_for_relevance(limit: int = 20):
    """Internal typed projection used to rebuild disposable semantic links."""
    URI, AUTH, DATABASE = _neo4j_config()
    safe_limit = max(1, min(int(limit or 20), 100))
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            with driver.session(database=DATABASE) as session:
                rows = session.run("""
                    MATCH (event:Event)
                    WHERE event.status = 'active' AND event.expires_at > $now
                    RETURN event.id AS event_id,
                           event.title AS title,
                           event.summary AS summary,
                           event.category AS category,
                           event.starts_at AS starts_at,
                           event.ends_at AS ends_at,
                           coalesce(event.session_starts, []) AS session_starts,
                           [(event)-[:HAS_TAG]->(tag:Concept) | coalesce(tag.label, tag.key)] AS tags,
                           [(event)-[:HAS_VIBE]->(vibe:Concept) | coalesce(vibe.label, vibe.key)] AS vibes
                    ORDER BY event.starts_at ASC
                    LIMIT $limit
                """, now=time.time(), limit=safe_limit)
                events = [dict(row) for row in rows]
                user_record = session.run(
                    "MATCH (user:User) RETURN count(user) AS count"
                ).single()
                user_count = int(user_record["count"] if user_record else 0)
        return {
            "status": "success", "events": events,
            "event_count": len(events), "user_count": user_count,
        }
    except Exception as exc:
        print(f"[EVENT_RELEVANCE] active event read failed error={type(exc).__name__}")
        return {"status": "error", "events": [], "event_count": 0}


@app.post("/api/events/reset")
def reset_event_graph(confirm: bool = False):
    """Demo-only scoped reset for Event nodes and newly orphaned Concepts."""
    if not confirm:
        raise HTTPException(status_code=400, detail="confirm=true is required")
    URI, AUTH, DATABASE = _neo4j_config()
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            with driver.session(database=DATABASE) as session:
                event_record = session.run(
                    "MATCH (event:Event) RETURN count(event) AS count"
                ).single()
                event_count = int(event_record["count"] if event_record else 0)
                event_id_record = session.run("""
                    MATCH (event:Event)
                    RETURN collect(DISTINCT event.id)[0..500] AS event_ids
                """).single()
                event_ids = [
                    str(value)[:80]
                    for value in list((event_id_record or {}).get("event_ids") or [])
                    if str(value or "").strip()
                ]
                concept_record = session.run("""
                    MATCH (:Event)-[:HAS_TAG|HAS_VIBE]->(concept:Concept)
                    RETURN collect(DISTINCT elementId(concept)) AS concept_ids
                """).single()
                event_concept_ids = list(
                    (concept_record or {}).get("concept_ids") or []
                )
                session.run("MATCH (event:Event) DETACH DELETE event").consume()
                orphan_record = session.run("""
                    MATCH (concept:Concept)
                    WHERE elementId(concept) IN $concept_ids
                      AND NOT (concept)--()
                    RETURN count(concept) AS count
                """, concept_ids=event_concept_ids).single()
                orphan_count = int(orphan_record["count"] if orphan_record else 0)
                session.run("""
                    MATCH (concept:Concept)
                    WHERE elementId(concept) IN $concept_ids
                      AND NOT (concept)--()
                    DELETE concept
                """, concept_ids=event_concept_ids).consume()
        result = {
            "status": "success",
            "events_deleted": event_count,
            "orphan_concepts_deleted": orphan_count,
            "event_ids": event_ids,
        }
        print(f"[EVENT RESET] scoped graph reset complete result={result}")
        return result
    except Exception as exc:
        print(f"[EVENT RESET] failed error={type(exc).__name__}")
        raise HTTPException(status_code=503, detail="Event graph reset failed") from exc


def _event_relevance_limit() -> int:
    try:
        value = int(os.getenv("EVENT_RELEVANCE_MAX_PER_USER", "3") or 3)
    except (TypeError, ValueError):
        value = 3
    return max(1, min(value, 10))


def _prune_event_relevance(session) -> int:
    """Keep only each user's strongest bounded Event retrieval set."""
    record = session.run("""
        MATCH (user:User)-[link:EVENT_RELEVANCE]->(event:Event)
        WITH user, link, event,
             CASE WHEN 'recent' IN coalesce(link.source_kinds, []) THEN 0 ELSE 1 END AS recent_rank
        ORDER BY user.id, recent_rank ASC,
                 coalesce(link.max_similarity, 0.0) DESC,
                 coalesce(event.starts_at, 0) ASC,
                 event.id ASC
        WITH user, collect(link) AS ranked_links
        UNWIND ranked_links[$limit..] AS extra
        DELETE extra
        RETURN count(extra) AS deleted
    """, limit=_event_relevance_limit()).single()
    return int(record["deleted"] if record else 0)


@app.post("/api/events/relevance/project")
def project_event_relevance(req: EventRelevanceProjectionRequest):
    """Replace derived Event relevance links without mutating user preferences."""
    event_ids = list(dict.fromkeys(
        re.sub(r"[^a-zA-Z0-9_-]", "", str(value or ""))[:80]
        for value in req.event_ids[:100]
        if str(value or "").strip()
    ))
    allowed_event_ids = set(event_ids)
    generated_at = min(max(float(req.generated_at or time.time()), 0.0), time.time() + 300)
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for raw in req.links[:5000]:
        if not isinstance(raw, dict):
            continue
        user_id = re.sub(r"\s+", "", str(raw.get("user_id") or ""))[:80]
        event_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(raw.get("event_id") or ""))[:80]
        relation = str(raw.get("relation") or "").lower()
        if not user_id or event_id not in allowed_event_ids or relation not in {"relevance", "avoidance"}:
            continue
        evidence: list[dict] = []
        for item in list(raw.get("evidence") or [])[:3]:
            if not isinstance(item, dict):
                continue
            user_concept = re.sub(r"\s+", " ", str(item.get("user_concept") or "").strip())[:60]
            event_signal = re.sub(r"\s+", " ", str(item.get("event_signal") or "").strip())[:60]
            similarity = max(0.0, min(float(item.get("similarity") or 0.0), 1.0))
            signal_type = str(item.get("signal_type") or "")
            source_kind = str(item.get("source_kind") or "")
            if user_concept and event_signal and signal_type in {"tag", "vibe"} and source_kind in {"recent", "durable"}:
                evidence.append({
                    "user_concept": user_concept,
                    "event_signal": event_signal,
                    "similarity": similarity,
                    "signal_type": signal_type,
                    "source_kind": source_kind,
                })
        if evidence:
            grouped[(user_id, event_id, relation)] = evidence

    relevance_rows, avoidance_rows = [], []
    for (user_id, event_id, relation), evidence in grouped.items():
        row = {
            "user_id": user_id,
            "event_id": event_id,
            "user_concepts": [item["user_concept"] for item in evidence],
            "event_signals": [item["event_signal"] for item in evidence],
            "similarities": [item["similarity"] for item in evidence],
            "signal_types": [item["signal_type"] for item in evidence],
            "source_kinds": [item["source_kind"] for item in evidence],
            "max_similarity": max(item["similarity"] for item in evidence),
        }
        (avoidance_rows if relation == "avoidance" else relevance_rows).append(row)
    avoidance_keys = {
        (row["user_id"], row["event_id"]) for row in avoidance_rows
    }
    relevance_rows = [
        row for row in relevance_rows
        if (row["user_id"], row["event_id"]) not in avoidance_keys
    ]

    URI, AUTH, DATABASE = _neo4j_config()
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            with driver.session(database=DATABASE) as session:
                session.run("""
                    MATCH ()-[old:EVENT_RELEVANCE|EVENT_AVOIDANCE]->(event:Event)
                    WHERE event.id IN $event_ids
                    DELETE old
                """, event_ids=event_ids).consume()
                session.run("""
                    UNWIND $rows AS item
                    MATCH (user:User {id:item.user_id}), (event:Event {id:item.event_id})
                    MERGE (user)-[link:EVENT_RELEVANCE]->(event)
                    SET link.user_concepts=item.user_concepts,
                        link.event_signals=item.event_signals,
                        link.similarities=item.similarities,
                        link.signal_types=item.signal_types,
                        link.source_kinds=item.source_kinds,
                        link.max_similarity=item.max_similarity,
                        link.updated_at=$generated_at
                """, rows=relevance_rows, generated_at=generated_at).consume()
                session.run("""
                    UNWIND $rows AS item
                    MATCH (user:User {id:item.user_id}), (event:Event {id:item.event_id})
                    MERGE (user)-[link:EVENT_AVOIDANCE]->(event)
                    SET link.user_concepts=item.user_concepts,
                        link.event_signals=item.event_signals,
                        link.similarities=item.similarities,
                        link.signal_types=item.signal_types,
                        link.source_kinds=item.source_kinds,
                        link.max_similarity=item.max_similarity,
                        link.updated_at=$generated_at
                """, rows=avoidance_rows, generated_at=generated_at).consume()
                _prune_event_relevance(session)
                final_relevance = session.run("""
                    MATCH ()-[link:EVENT_RELEVANCE]->(event:Event)
                    WHERE event.id IN $event_ids
                    RETURN count(link) AS count
                """, event_ids=event_ids).single()
                final_avoidance = session.run("""
                    MATCH ()-[link:EVENT_AVOIDANCE]->(event:Event)
                    WHERE event.id IN $event_ids
                    RETURN count(link) AS count
                """, event_ids=event_ids).single()
                relevance_count = int(
                    final_relevance["count"] if final_relevance else len(relevance_rows)
                )
                avoidance_count = int(
                    final_avoidance["count"] if final_avoidance else len(avoidance_rows)
                )
        return {
            "status": "success",
            "event_count": len(event_ids),
            "relevance_count": relevance_count,
            "avoidance_count": avoidance_count,
            "link_count": relevance_count + avoidance_count,
        }
    except Exception as exc:
        print(f"[EVENT_RELEVANCE] graph projection failed error={type(exc).__name__}")
        return {"status": "error", "event_count": len(event_ids), "link_count": 0}


def _refresh_semantic_event_links(session) -> dict[str, int]:
    relevance_threshold = max(0.0, min(
        float(os.getenv("EVENT_RELEVANCE_MIN_SIMILARITY", "0.68")), 1.0,
    ))
    avoidance_threshold = max(0.0, min(
        float(os.getenv("EVENT_AVOIDANCE_MIN_SIMILARITY", "0.74")), 1.0,
    ))
    now = time.time()
    session.run("""
        MATCH ()-[old:EVENT_RELEVANCE|EVENT_AVOIDANCE]->(event:Event)
        WHERE event.status = 'active' AND event.expires_at > $now
        DELETE old
    """, now=now).consume()
    avoidance = session.run("""
        MATCH (user:User)-[:AVOIDS]->(user_concept:Concept)
        MATCH (event:Event)-[signal_relation:HAS_TAG|HAS_VIBE]->(event_concept:Concept)
        WHERE event.status = 'active' AND event.expires_at > $now
          AND user_concept.embedding IS NOT NULL
          AND event_concept.embedding IS NOT NULL
          AND user_concept.kind = 'activity'
          AND type(signal_relation) = 'HAS_TAG'
        WITH user, event, user_concept, event_concept,
             vector.similarity.cosine(user_concept.embedding, event_concept.embedding) AS score
        WHERE score >= $threshold
        WITH user, event, user_concept, event_concept, score ORDER BY score DESC
        WITH user, event, collect({user_concept:user_concept.label,
             event_signal:event_concept.label, similarity:score})[0..3] AS evidence
        MERGE (user)-[link:EVENT_AVOIDANCE]->(event)
        SET link.user_concepts=[item IN evidence | item.user_concept],
            link.event_signals=[item IN evidence | item.event_signal],
            link.similarities=[item IN evidence | item.similarity],
            link.source_kinds=['durable'],
            link.max_similarity=reduce(best=0.0, item IN evidence |
                CASE WHEN item.similarity > best THEN item.similarity ELSE best END),
            link.updated_at=$now
        RETURN count(link) AS written
    """, now=now, threshold=avoidance_threshold).single()
    relevance = session.run("""
        MATCH (user:User)-[preference:PREFERS|CURRENTLY_WANTS]->(user_concept:Concept)
        MATCH (event:Event)-[signal_relation:HAS_TAG|HAS_VIBE]->(event_concept:Concept)
        WHERE event.status = 'active' AND event.expires_at > $now
          AND NOT (user)-[:EVENT_AVOIDANCE]->(event)
          AND user_concept.embedding IS NOT NULL
          AND event_concept.embedding IS NOT NULL
          AND ((user_concept.kind = 'activity' AND type(signal_relation) = 'HAS_TAG')
            OR user_concept.kind = 'interest')
        WITH user, event, preference, user_concept, event_concept,
             vector.similarity.cosine(user_concept.embedding, event_concept.embedding) AS score
        WHERE score >= $threshold
        WITH user, event, preference, user_concept, event_concept, score ORDER BY score DESC
        WITH user, event, collect({user_concept:user_concept.label,
             event_signal:event_concept.label, similarity:score,
             source_kind:CASE WHEN type(preference)='CURRENTLY_WANTS'
                THEN 'recent' ELSE 'durable' END})[0..3] AS evidence
        MERGE (user)-[link:EVENT_RELEVANCE]->(event)
        SET link.user_concepts=[item IN evidence | item.user_concept],
            link.event_signals=[item IN evidence | item.event_signal],
            link.similarities=[item IN evidence | item.similarity],
            link.source_kinds=[item IN evidence | item.source_kind],
            link.max_similarity=reduce(best=0.0, item IN evidence |
                CASE WHEN item.similarity > best THEN item.similarity ELSE best END),
            link.updated_at=$now
        RETURN count(link) AS written
    """, now=now, threshold=relevance_threshold).single()
    _prune_event_relevance(session)
    final_relevance = session.run("""
        MATCH ()-[link:EVENT_RELEVANCE]->(event:Event)
        WHERE event.status = 'active' AND event.expires_at > $now
        RETURN count(link) AS count
    """, now=now).single()
    return {
        "relevance_count": int(
            final_relevance["count"] if final_relevance
            else (relevance["written"] if relevance else 0)
        ),
        "avoidance_count": int(avoidance["written"] if avoidance else 0),
    }


@app.get("/api/concepts/missing-embeddings")
def list_missing_concept_embeddings(limit: int = 20):
    URI, AUTH, DATABASE = _neo4j_config()
    safe_limit = max(1, min(int(limit or 20), 50))
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            with driver.session(database=DATABASE) as session:
                rows = session.run("""
                    MATCH (concept:Concept)
                    WHERE (concept)<-[:PREFERS|AVOIDS|CURRENTLY_WANTS]-(:User)
                       OR (concept)<-[:HAS_TAG|HAS_VIBE]-(:Event)
                    WITH DISTINCT concept, CASE
                      WHEN EXISTS { MATCH (:Event)-[:HAS_TAG]->(concept) } THEN 'activity'
                      WHEN EXISTS { MATCH (:Event)-[:HAS_VIBE]->(concept) } THEN 'vibe'
                      WHEN EXISTS { MATCH (:User)-[:CURRENTLY_WANTS]->(concept) } THEN 'activity'
                      ELSE coalesce(concept.kind, 'unknown') END AS suggested_kind
                    WHERE concept.embedding IS NULL OR size(concept.embedding) <> 768
                    RETURN concept.key AS key, concept.label AS label, suggested_kind
                    ORDER BY concept.label LIMIT $limit
                """, limit=safe_limit)
                concepts = [dict(row) for row in rows]
        return {"status": "success", "concepts": concepts, "count": len(concepts)}
    except Exception as exc:
        print(f"[CONCEPT_EMBEDDING] pending read failed error={type(exc).__name__}")
        return {"status": "error", "concepts": [], "count": 0}


@app.post("/api/concepts/embeddings/project")
def project_concept_embeddings(req: ConceptEmbeddingProjectionRequest):
    valid_kinds = {"activity", "interest", "vibe", "partner_trait", "value", "unknown"}
    clean = []
    for item in req.concepts[:50]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()[:100]
        label = re.sub(r"\s+", " ", str(item.get("label") or "").strip())[:60]
        kind = str(item.get("kind") or "unknown")
        vector = item.get("embedding")
        if not key or not label or kind not in valid_kinds or not isinstance(vector, list) or len(vector) != 768:
            continue
        try:
            embedding = [float(value) for value in vector]
        except (TypeError, ValueError):
            continue
        if all(math.isfinite(value) for value in embedding):
            clean.append({"key": key, "label": label, "kind": kind, "embedding": embedding})

    URI, AUTH, DATABASE = _neo4j_config()
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            with driver.session(database=DATABASE) as session:
                session.run("""
                    CREATE VECTOR INDEX concept_embedding_index IF NOT EXISTS
                    FOR (concept:Concept) ON (concept.embedding)
                    OPTIONS {indexConfig: {`vector.dimensions`: 768,
                        `vector.similarity_function`: 'cosine'}}
                """).consume()
                session.run("""
                    UNWIND $concepts AS item
                    MATCH (concept:Concept {key:item.key})
                    SET concept.label=item.label,
                        concept.kind=CASE WHEN item.kind <> 'unknown' THEN item.kind
                            ELSE coalesce(concept.kind, 'unknown') END,
                        concept.embedding=item.embedding,
                        concept.embedded_at=$now
                """, concepts=clean, now=time.time()).consume()
                relation_counts = _refresh_semantic_event_links(session)
                pending = session.run("""
                    MATCH (concept:Concept)
                    WHERE ((concept)<-[:PREFERS|AVOIDS|CURRENTLY_WANTS]-(:User)
                       OR (concept)<-[:HAS_TAG|HAS_VIBE]-(:Event))
                      AND (concept.embedding IS NULL OR size(concept.embedding) <> 768)
                    RETURN count(DISTINCT concept) AS count
                """).single()
        return {"status": "success", "embedded_count": len(clean),
            "pending_count": int(pending["count"] if pending else 0), **relation_counts}
    except Exception as exc:
        print(f"[CONCEPT_EMBEDDING] projection failed error={type(exc).__name__}: {exc}")
        return {"status": "error", "embedded_count": 0, "error_code": type(exc).__name__}


@app.post("/api/trigger_daily_search", include_in_schema=False)
def deprecated_trigger_daily_search():
    raise HTTPException(
        status_code=410,
        detail="Event discovery moved to the port 8000 Tavily adapter.",
    )



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
                    trait_value = str(rel.get("trait") or "").strip()
                    rel_type_value = rel.get("relation_type")
                    if not trait_value or not rel_type_value:
                        print(f"⚠️ Skip invalid graph relationship: {rel}")
                        continue
                    key_value = "concept_" + hashlib.sha256(trait_value.lower().encode("utf-8")).hexdigest()[:16]
                    if rel_type_value == "DISLIKES_TRAIT":
                        session.run(
                            """
                            MERGE (u:User {id: $user_id})
                            MERGE (c:Concept {key: $key})
                            ON CREATE SET c.label = $trait, c.kind = 'preference'
                            WITH u, c
                            OPTIONAL MATCH (u)-[old:PREFERS]->(c)
                            DELETE old
                            MERGE (u)-[:AVOIDS]->(c)
                            """,
                            user_id=req.user_id,
                            key=key_value,
                            trait=trait_value,
                        )
                    else:
                        session.run(
                            """
                            MERGE (u:User {id: $user_id})
                            MERGE (c:Concept {key: $key})
                            ON CREATE SET c.label = $trait, c.kind = 'preference'
                            WITH u, c
                            OPTIONAL MATCH (u)-[old:AVOIDS]->(c)
                            DELETE old
                            MERGE (u)-[:PREFERS]->(c)
                            """,
                            user_id=req.user_id,
                            key=key_value,
                            trait=trait_value,
                        )
                    print(f"✅ Neo4j saved: {req.user_id} -[{rel_type_value}]-> Concept({trait_value})")

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

class ContextProjectionRequest(BaseModel):
    user_id: str
    concepts: list[dict] = []
    expires_at: float
    revision: int = 0

class ChatTripleRequest(BaseModel):
    session_id: str
    match_id: str | None = None
    participants: list[str] = []
    triples: list[dict] = []
    evidence_messages: list[dict] = []


def _neo4j_config():
    return (os.getenv("NEO4J_URI"), (os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD")), os.getenv("NEO4J_DATABASE", "neo4j"))


def _concept_key(label: str) -> str:
    normalized = re.sub(r"\s+", "", str(label or "").strip().lower())
    return "concept_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


@app.post("/api/context/project")
async def project_current_context(req: ContextProjectionRequest):
    """Replace one user's short-lived intent projection without storing raw context."""
    now = time.time()
    expires_at = max(now + 60, min(float(req.expires_at), now + 45 * 86400))
    clean: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in req.concepts[:4]:
        label = re.sub(r"\s+", " ", str(item.get("label") or "").strip())[:40]
        if not label:
            continue
        normalized = re.sub(r"\s+", "", label.lower())
        if normalized in seen:
            continue
        seen.add(normalized)
        key = str(item.get("key") or "").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,50}", key):
            key = _concept_key(label)
        clean.append({"key": key, "label": label})

    URI, AUTH, DATABASE = _neo4j_config()
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            with driver.session(database=DATABASE) as session:
                session.run("""
                    MATCH ()-[expired:CURRENTLY_WANTS]->()
                    WHERE expired.expires_at < $now
                    DELETE expired
                """, now=now).consume()
                session.run("""
                    MERGE (u:User {id:$user_id})
                    WITH u
                    OPTIONAL MATCH (u)-[old:CURRENTLY_WANTS]->()
                    DELETE old
                """, user_id=req.user_id).consume()
                for item in clean:
                    existing = session.run("""
                        MATCH (c:Concept)
                        WHERE toLower(c.label)=toLower($label)
                        RETURN c.key AS key LIMIT 1
                    """, label=item["label"]).single()
                    key = str(existing["key"] if existing else item["key"])
                    session.run("""
                        MATCH (u:User {id:$user_id})
                        MERGE (c:Concept {key:$key})
                        SET c.label=$label, c.kind='activity'
                        MERGE (u)-[r:CURRENTLY_WANTS]->(c)
                        SET r.expires_at=$expires_at
                    """, user_id=req.user_id, key=key, label=item["label"],
                         expires_at=expires_at).consume()
        return {
            "status": "success",
            "user_id": req.user_id,
            "concept_count": len(clean),
            "expires_at": expires_at,
            "revision": req.revision,
        }
    except Exception as exc:
        print(f"[CONTEXT_GRAPH][9001] projection failed user={req.user_id} error={exc}")
        return {"status": "error", "message": type(exc).__name__}


@app.post("/api/clear_graph")
async def clear_graph_endpoint():
    print("🧹 收到清空 Neo4j Graph 請求")
    if not destructive_tools_enabled():
        raise HTTPException(status_code=403, detail={"code": "demo_tools_disabled"})
    URI, AUTH, DATABASE = _neo4j_config()
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            driver.verify_connectivity()
            with driver.session(database=DATABASE) as session:
                session.run("MATCH (n) DETACH DELETE n").consume()
        agent_memory_db.clear()
        print("✅ Neo4j Graph 已清空")
        return {"status": "success", "message": "Neo4j Graph 已清空"}
    except Exception as e:
        print(f"❌ 清空 Neo4j Graph 失敗: {type(e).__name__}")
        raise HTTPException(status_code=503, detail={"code": "graph_cleanup_failed"}) from e


@app.get("/api/graph/health")
async def graph_health_endpoint():
    URI, AUTH, DATABASE = _neo4j_config()
    if not URI:
        return {"status": "not_configured"}
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            driver.verify_connectivity()
        return {"status": "available"}
    except Exception as exc:
        print(f"⚠️ Graph health check failed: {type(exc).__name__}")
        return {"status": "unavailable"}

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
                    if stance in ["want", "currently_wants"]:
                        session.run("""
                            MATCH (u:User {id:$user_id})-[old_w:CURRENTLY_WANTS]->()
                            DELETE old_w
                        """, user_id=req.user_id).consume()
                    session.run("""
                        MERGE (u:User {id:$user_id})
                        MERGE (c:Concept {key:$key})
                        ON CREATE SET c.label=$label, c.kind='preference'
                        ON MATCH SET c.label=coalesce(c.label,$label)
                        WITH u, c
                        OPTIONAL MATCH (u)-[old:PREFERS|AVOIDS|CURRENTLY_WANTS]->(c)
                        DELETE old
                        WITH u, c
                        FOREACH (_ IN CASE WHEN $stance IN ['dislike','avoid'] THEN [1] ELSE [] END |
                            MERGE (u)-[:AVOIDS]->(c)
                        )
                        FOREACH (_ IN CASE WHEN $stance IN ['like','require','prefer'] THEN [1] ELSE [] END |
                            MERGE (u)-[:PREFERS]->(c)
                        )
                        FOREACH (_ IN CASE WHEN $stance IN ['want','currently_wants'] THEN [1] ELSE [] END |
                            MERGE (u)-[w:CURRENTLY_WANTS]->(c)
                            SET w.expires_at = $now + 30 * 86400
                        )
                    """, user_id=req.user_id, key=key, label=label, stance=stance, now=now).consume()
                    clean.append({"key":key,"label":label,"stance":stance,"category":"preference","confidence":confidence,"last_seen_at":now})
        return {"memories": clean, "status": "success"}
    except Exception as exc:
        print(f"[MEMORY][9001 apply] graph_write_failed user={req.user_id} error={exc}")
        return {"memories": [], "status": "error", "error_code": type(exc).__name__}
@app.get("/api/memory/{user_id}")
async def list_memories(user_id: str, limit: int = 12):
    try:
        URI, AUTH, DATABASE = _neo4j_config()
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            with driver.session(database=DATABASE) as session:
                rows = session.run("""
                    MATCH (u:User {id:$user_id})-[r:PREFERS|AVOIDS|CURRENTLY_WANTS]->(c:Concept)
                    RETURN c.key AS key,
                           coalesce(c.label, c.key) AS label,
                           CASE type(r)
                               WHEN 'AVOIDS' THEN 'dislike'
                               WHEN 'PREFERS' THEN 'like'
                               WHEN 'CURRENTLY_WANTS' THEN 'want'
                               ELSE 'like'
                           END AS stance,
                           coalesce(c.kind, 'preference') AS category,
                           1.0 AS confidence,
                           coalesce(r.last_seen_at, 0) AS last_seen_at
                    LIMIT $limit
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
            if req.action == "disable":
                session.run("""
                    MATCH (u:User {id:$user_id})-[r:PREFERS|AVOIDS|CURRENTLY_WANTS]->(c:Concept {key:$key})
                    DELETE r
                """, user_id=req.user_id, key=req.key).consume()
                return {"status":"success"}
            elif req.action == "restore":
                session.run("""
                    MATCH (u:User {id:$user_id}), (c:Concept {key:$key})
                    MERGE (u)-[:PREFERS]->(c)
                """, user_id=req.user_id, key=req.key).consume()
                return {"status":"success"}
            return {"status":"success"}
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

