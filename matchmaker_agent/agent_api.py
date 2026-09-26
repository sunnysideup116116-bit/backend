import json
import asyncio
import os
import hashlib
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from neo4j import GraphDatabase, Query
from pathlib import Path
from dotenv import load_dotenv

# Resolve shared signing configuration before any service-specific dotenv or
# lazy quota-store initialization can change the credential lookup order.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_quota.signing_config import load_signing_config, validate_signing_config
load_signing_config()

# 撘瑕?? agent_api.py ??函????.env 瑼?
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from registration_graph import (
    RegistrationProjection, project_identity, seed_registration,
    assert_existing_preference_identities,
)
from concept_identity import (
    HARD_DURABLE_MEMORY_LIMIT,
    MAX_PREFERENCE_TEXT_CHARS,
    MAX_OWNER_MEMORY_QUERY_CHARS,
    MAX_PREFERENCE_KEY_CHARS,
    MAX_PREFERENCE_EVIDENCE_CHARS,
    PreferenceTextError,
    canonicalize_concept,
    canonicalize_fresh_concept,
    canonicalize_concept_v1,
    is_v2_preference_key,
    durable_memory_limit,
    has_mixed_preference_polarity,
    normalize_preference_text,
    normalize_fresh_preference_text,
    stored_concept_identity,
    verified_legacy_identity,
    split_compound_concept_label,
    split_explicit_preference_enumeration,
)
from matchmaker import (
    MatchmakerAgent,
    MatchEvaluationError,
    provider_search_context,
    safe_search_context,
)

# ????FastAPI ?蝔? (撠望????銝?鈭?)
# Shared accounting is importable when invoked through start_all.sh or tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'social'))
from agent_quota.internal import MatchmakerQuotaMiddleware, start_worker, stop_worker
from matchmaker_agent.preference_write_fence import lock_preferences, bump_preferences, PreferenceFenceError, fence_error_result
from matchmaker_agent.preference_mutations import write_preference_edges
app = FastAPI()
from matchmaker_agent.preference_bootstrap_api import router as preference_bootstrap_router
app.include_router(preference_bootstrap_router)
app.add_middleware(MatchmakerQuotaMiddleware)
app.router.add_event_handler('startup', validate_signing_config)
app.router.add_event_handler('startup', start_worker)
app.router.add_event_handler('shutdown', stop_worker)


@app.get("/health")
def health_check():
    """Process-level readiness endpoint; does not read profiles or Neo4j."""
    return {"status": "ok", "service": "matchmaker"}


def destructive_tools_enabled() -> bool:
    return os.getenv("DEMO_DESTRUCTIVE_TOOLS_ENABLED", "off").strip().lower() in {"1", "true", "on"}

# ??????慦?憭扯
agent = MatchmakerAgent()
MATCH_REQUEST_TIMEOUT_SECONDS = 90.0  # Must remain below Social's 120s HTTP deadline.
MATCH_GRAPH_TIMEOUT_SECONDS = 15.0
_MATCH_GRAPH_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="match-graph")

GLOBAL_RULE_LIMIT = max(0, min(int(os.getenv("MATCH_GLOBAL_RULE_LIMIT", "2")), 5))
GLOBAL_RULE_CHAR_LIMIT = max(10, min(int(os.getenv("MATCH_GLOBAL_RULE_CHAR_LIMIT", "30")), 140))
GLOBAL_RULE_SIMILARITY_THRESHOLD = max(
    0.0, min(float(os.getenv("MATCH_GLOBAL_RULE_SIMILARITY_THRESHOLD", "0.38")), 1.0)
)
PREFERENCE_SEMANTIC_INDEX_NAME = "concept_embedding_index"
PREFERENCE_SEMANTIC_DIMENSIONS = 768
PREFERENCE_SEMANTIC_GRAPH_TIMEOUT_SECONDS = 3.0


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
    search_context: dict | None = None
    request_budget_seconds: float | None = Field(default=None, gt=0, le=120, allow_inf_nan=False)

    @field_validator("search_context", mode="before")
    @classmethod
    def _bound_search_context(cls, value):
        if value is None:
            return None
        return safe_search_context(value)

class ProactiveEventMatchRequest(BaseModel):
    user_id: str
    excluded_user_ids: list[str] = []

class EventIngestRequest(BaseModel):
    region: str = "高雄"
    window_days: int = 30
    max_events: int = 6
    search_results: list[dict] = []
    write_deadline: float | None = Field(
        default=None, gt=0, allow_inf_nan=False,
    )

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
    write_deadline = None
    if req.write_deadline is not None:
        write_deadline = min(float(req.write_deadline), time.time() + 900)
    result = agent.extract_and_ingest_search_results(
        bounded_results,
        region=safe_region,
        window_days=max(1, min(int(req.window_days or 30), 60)),
        max_events=max(1, min(int(req.max_events or 6), 6)),
        write_deadline=write_deadline,
    )
    return {
        "status": "success",
        "region": safe_region,
        "search_count": len(bounded_results),
        "ingested_count": result.get("ingested_count", 0),
        "validation_counts": result.get("validation_counts", {}),
        "events": result.get("events", []),
    }


def get_user_graph_memory(user_id: str, *, strict: bool = False) -> str:
    """Read one user preference memory from Neo4j."""
    step_start = time.perf_counter()
    uri = os.getenv("NEO4J_URI")
    auth = (os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD"))
    database = os.getenv("NEO4J_DATABASE", "neo4j")
    try:
        with GraphDatabase.driver(uri, auth=auth, connection_timeout=5, connection_acquisition_timeout=5, max_transaction_retry_time=0) as driver:
            driver.verify_connectivity()
            print(f"✅ [Neo4j 讀取] 連線驗證成功 (user={user_id})")
            with driver.session(database=database) as session:
                result = session.run(
                    Query("""
                    MATCH (u:User {id: $user_id})-[r:PREFERS|AVOIDS]->(c:Concept)
                    RETURN CASE type(r) WHEN 'AVOIDS' THEN 'DISLIKES_TRAIT' ELSE 'LIKES_TRAIT' END AS type,
                           coalesce(c.label, c.key) AS trait, '' AS reason
                    """, timeout=5),
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
        if strict:
            raise MatchEvaluationError("matchmaker_graph_unavailable") from None
        print(f"Neo4j user memory failed: {type(e).__name__}")
        return "無法讀取圖譜記憶。"


def get_global_rules(*, strict: bool = False) -> str:
    """Read top global learned rules from Neo4j."""
    step_start = time.perf_counter()
    uri = os.getenv("NEO4J_URI")
    auth = (os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD"))
    database = os.getenv("NEO4J_DATABASE", "neo4j")
    try:
        with GraphDatabase.driver(uri, auth=auth, connection_timeout=5, connection_acquisition_timeout=5, max_transaction_retry_time=0) as driver:
            driver.verify_connectivity()
            print("✅ [Neo4j 全域法則] 連線驗證成功")
            with driver.session(database=database) as session:
                result = session.run(
                    Query("""
                    MATCH (a:Agent {name: "System"})-[r:LEARNED_RULE]->(rule:GlobalRule)
                    RETURN rule.content AS content, rule.category AS category, r.weight AS weight
                    ORDER BY r.weight DESC
                    LIMIT $limit
                    """, timeout=5),
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
        if strict:
            raise MatchEvaluationError("matchmaker_graph_unavailable") from None
        print(f"Neo4j global rules failed: {type(e).__name__}")
        return ""

@app.post("/api/match")
async def match_endpoint(req: MatchRequest):
    try:
        budget = min(MATCH_REQUEST_TIMEOUT_SECONDS, req.request_budget_seconds) if req.request_budget_seconds is not None else MATCH_REQUEST_TIMEOUT_SECONDS
        async with asyncio.timeout(budget):
            return await _evaluate_match_request(req)
    except TimeoutError:
        raise HTTPException(status_code=504, detail={"code": "matchmaker_timeout"}) from None
    except MatchEvaluationError as exc:
        status = 504 if exc.code in {"matchmaker_timeout", "matchmaker_graph_timeout"} else 502
        raise HTTPException(status_code=status, detail={"code": exc.code}) from None


async def _evaluate_match_request(req: MatchRequest):
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
    loop = asyncio.get_running_loop()
    futures = [
        loop.run_in_executor(_MATCH_GRAPH_POOL, partial(get_user_graph_memory, target_user_id, strict=True)),
        loop.run_in_executor(_MATCH_GRAPH_POOL, partial(get_global_rules, strict=True)),
        *[loop.run_in_executor(_MATCH_GRAPH_POOL, partial(get_user_graph_memory, candidate_id, strict=True)) for candidate_id in candidate_ids],
    ]
    try:
        values = await asyncio.wait_for(asyncio.gather(*futures), timeout=MATCH_GRAPH_TIMEOUT_SECONDS)
    except TimeoutError:
        raise MatchEvaluationError("matchmaker_graph_timeout") from None
    finally:
        for future in futures:
            if not future.done():
                future.cancel()
    graph_memory, global_heuristics = values[:2]
    candidate_memory_by_id = dict(zip(candidate_ids, values[2:]))

    print(f"[TIMING][9001 /api/match] parallel Neo4j reads wrapper: {time.perf_counter() - step_start:.3f}s")

    enriched_candidates = []
    for candidate in req.candidates:
        enriched = dict(candidate)
        enriched["graph_memory"] = candidate_memory_by_id.get(candidate.get("user_id"), "")
        enriched_candidates.append(enriched)

    
    # 3. Agent decision with target graph memory + candidate graph memories + global rules.
    step_start = time.perf_counter()
    raw_response = await agent.match_async(
        req.target_user, enriched_candidates, graph_memory,
        global_heuristics, req.target_deep_profile,
        search_context=provider_search_context(req.search_context),
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
        
        print(f"[TIMING][9001 /api/match] parse failed; no fallback candidate is created. total: {time.perf_counter() - total_start:.3f}s")
        raise HTTPException(status_code=502, detail={"code": "matchmaker_invalid_response"}) from None



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
          AND (type(preference) <> 'CURRENTLY_WANTS'
               OR coalesce(preference.expires_at, 0) > $now)
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
                    RETURN concept.key AS key, concept.label AS label, suggested_kind,
                           concept.semantic_text AS semantic_text,
                           concept.display_label AS display_label,
                           coalesce(concept.canonicalization_version, 'legacy_unknown') AS canonicalization_version,
                           concept.semantic_input_hash AS semantic_input_hash,
                           concept.fidelity_status AS fidelity_status
                    ORDER BY concept.label LIMIT $limit
                """, limit=safe_limit)
                concepts = [dict(row) for row in rows]
                for concept in concepts:
                    identity = stored_concept_identity(concept)
                    if identity:
                        concept.update(identity.as_dict())
                    elif concept.get("canonicalization_version") == "v2":
                        concept["fidelity_status"] = "invalid"
        return {"status": "success", "concepts": concepts, "count": len(concepts)}
    except Exception as exc:
        print(f"[CONCEPT_EMBEDDING] pending read failed error={type(exc).__name__}")
        return {"status": "error", "concepts": [], "count": 0}


def _validate_embedding_targets(tx, concepts):
    """Indexed, bounded proof check before any schema/vector mutation."""
    expected = {item["key"]: item for item in concepts}
    if len(expected) != len(concepts):
        raise ValueError("embedding_duplicate_key")
    rows = list(tx.run("""
        UNWIND $keys AS key
        MATCH (concept:Concept {key:key})
        RETURN concept.key AS key,concept.label AS label,
               concept.semantic_text AS semantic_text,
               concept.canonicalization_version AS canonicalization_version,
               concept.semantic_input_hash AS semantic_input_hash,
               concept.fidelity_status AS fidelity_status
    """, keys=sorted(expected)))
    if len(rows) != len(expected):
        raise ValueError("embedding_identity_mismatch")
    for row in rows:
        item = expected.get(row.get("key"))
        if item is None:
            raise ValueError("embedding_identity_mismatch")
        if item["canonicalization_version"] == "v2":
            identity = stored_concept_identity(dict(row))
            if (not identity or identity.semantic_text != item["label"]
                    or identity.semantic_input_hash != item["semantic_input_hash"]):
                raise ValueError("embedding_identity_mismatch")
        elif row.get("canonicalization_version") == "v2" or row.get("label") != item["label"]:
            raise ValueError("embedding_identity_mismatch")


def _write_concept_embedding_batch(tx, concepts, now):
    _validate_embedding_targets(tx, concepts)
    written = tx.run("""
        UNWIND $concepts AS item
        MATCH (concept:Concept {key:item.key})
        WHERE (item.canonicalization_version='v2'
               AND concept.canonicalization_version='v2'
               AND concept.semantic_input_hash=item.semantic_input_hash
               AND concept.semantic_text=item.label)
           OR (item.canonicalization_version='legacy_unknown'
               AND coalesce(concept.canonicalization_version, '') <> 'v2'
               AND concept.label=item.label)
        SET concept.kind=CASE WHEN item.kind <> 'unknown' THEN item.kind
                ELSE coalesce(concept.kind, 'unknown') END,
            concept.embedding=item.embedding,
            concept.embedding_source_hash=item.semantic_input_hash,
            concept.embedding_model=item.embedding_model,
            concept.embedding_task=item.embedding_task,
            concept.embedded_at=$now
        RETURN count(concept) AS written
    """, concepts=concepts, now=now).single()
    if not written or int(written["written"]) != len(concepts):
        # An intervening mutation must roll back every vector in this batch.
        raise ValueError("embedding_identity_mismatch")
    return len(concepts)


@app.post("/api/v2/concepts/embeddings/project")
@app.post("/api/concepts/embeddings/project")
def project_concept_embeddings(req: ConceptEmbeddingProjectionRequest):
    valid_kinds = {"activity", "interest", "vibe", "partner_trait", "value", "unknown"}
    clean = []
    if len(req.concepts) > 50:
        return {"status": "error", "embedded_count": 0, "error_code": "embedding_batch_limit_exceeded", "retryable": False}
    for item in req.concepts:
        if not isinstance(item, dict):
            return {"status": "error", "embedded_count": 0, "error_code": "invalid_embedding_projection", "retryable": False}
        key = str(item.get("key") or "").strip()
        try:
            label = normalize_preference_text(item.get("semantic_text") or item.get("label") or "")
        except PreferenceTextError as exc:
            return {"status": "error", "embedded_count": 0, "error_code": exc.code}
        identity = stored_concept_identity(item)
        if (is_v2_preference_key(key) or item.get("canonicalization_version") == "v2") and not identity:
            return {"status": "error", "embedded_count": 0, "error_code": "embedding_identity_unverified"}
        kind = str(item.get("kind") or "unknown")
        vector = item.get("embedding")
        if (not key or len(key) > 100 or not label or kind not in valid_kinds
                or not isinstance(vector, list) or len(vector) != 768):
            return {"status": "error", "embedded_count": 0, "error_code": "invalid_embedding_projection", "retryable": False}
        try:
            embedding = [float(value) for value in vector]
        except (TypeError, ValueError):
            return {"status": "error", "embedded_count": 0, "error_code": "invalid_embedding_projection", "retryable": False}
        if not all(math.isfinite(value) for value in embedding) or not any(embedding):
            return {"status": "error", "embedded_count": 0, "error_code": "invalid_embedding_projection", "retryable": False}
        clean.append({"key": key, "label": label, "kind": kind, "embedding": embedding,
                          "canonicalization_version": "v2" if identity else "legacy_unknown",
                          "semantic_input_hash": identity.semantic_input_hash if identity else "",
                          "embedding_model": str(item.get("embedding_model") or ""),
                          "embedding_task": str(item.get("embedding_task") or "")})

    URI, AUTH, DATABASE = _neo4j_config()
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            with driver.session(database=DATABASE) as session:
                session.execute_write(_validate_embedding_targets, clean)
                session.run("""
                    CREATE VECTOR INDEX concept_embedding_index IF NOT EXISTS
                    FOR (concept:Concept) ON (concept.embedding)
                    OPTIONS {indexConfig: {`vector.dimensions`: 768,
                        `vector.similarity_function`: 'cosine'}}
                """).consume()
                written = session.execute_write(_write_concept_embedding_batch, clean, time.time())
                relation_counts = _refresh_semantic_event_links(session)
                pending = session.run("""
                    MATCH (concept:Concept)
                    WHERE ((concept)<-[:PREFERS|AVOIDS|CURRENTLY_WANTS]-(:User)
                       OR (concept)<-[:HAS_TAG|HAS_VIBE]-(:Event))
                      AND (concept.embedding IS NULL OR size(concept.embedding) <> 768)
                    RETURN count(DISTINCT concept) AS count
                """).single()
        return {"status": "success", "embedded_count": written,
            "pending_count": int(pending["count"] if pending else 0), **relation_counts}
    except Exception as exc:
        if str(exc) in {"embedding_identity_mismatch", "embedding_duplicate_key"}:
            return {"status": "error", "embedded_count": 0, "error_code": str(exc), "retryable": False}
        print(f"[CONCEPT_EMBEDDING] projection failed error={type(exc).__name__}")
        return {"status": "error", "embedded_count": 0, "error_code": type(exc).__name__}


@app.post("/api/trigger_daily_search", include_in_schema=False)
def deprecated_trigger_daily_search():
    raise HTTPException(
        status_code=410,
        detail="Event discovery moved to the port 8000 Tavily adapter.",
    )



class FeedbackRequest(BaseModel):
    user_id: str
    source_created_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    target_id: str # noqa
    action: str # "accept" ??"decline"
    target_traits: dict # 撠?扳
    explicit_reasons: list[str] = Field(
        default_factory=list, max_length=HARD_DURABLE_MEMORY_LIMIT,
    )  # 雿輻??蝣箏?貊?憍??寡釭

# ???豢?靘芋??Agent ???嗅澈 (撖行銝剜?摮??鞈?摨急?撖怠? MongoDB)
agent_memory_db = {} 


def _grounded_feedback_identities(reasons):
    """Ground only exact normalized selections/closed atomic enumerations."""
    identities = {}
    for reason in reasons:
        source = normalize_fresh_preference_text(reason)
        # These are UI category headers, not semantic role/action words.
        source = re.sub(r"^(?:個性|近期情境|價值觀|興趣)\s*[:：]\s*", "", source)
        if has_mixed_preference_polarity(source):
            raise PreferenceTextError("feedback_mixed_polarity")
        labels = (split_explicit_preference_enumeration(source, limit=HARD_DURABLE_MEMORY_LIMIT)
                  or split_compound_concept_label(source, limit=HARD_DURABLE_MEMORY_LIMIT)
                  or [source])
        for label in labels:
            identity = canonicalize_concept(label)
            if identity:
                identities[identity.key] = identity
    if len(identities) > durable_memory_limit():
        raise PreferenceTextError("feedback_memory_limit_exceeded")
    return identities

@app.post("/api/v2/feedback")
@app.post("/api/feedback")
async def receive_feedback(req: FeedbackRequest):
    """Normalize this explicit selection, then use the existing Concept writer.

    A bare decision is not preference consent. Neither unselected target traits
    nor a previous request's in-memory history may supply additional dislikes.
    """
    source_created_at = req.source_created_at
    if req.action not in {"accept", "decline"}:
        raise HTTPException(status_code=400, detail={"code": "invalid_feedback_action"})
    if not any(reason.strip() for reason in req.explicit_reasons):
        return {"status": "skipped", "memories": [], "message": "No reasons selected"}
    if len(req.explicit_reasons) > durable_memory_limit():
        raise HTTPException(status_code=422, detail={"code": "feedback_memory_limit_exceeded"})
    try:
        grounded = _grounded_feedback_identities(req.explicit_reasons)
    except PreferenceTextError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code}) from None
    history_text = json.dumps({
        "action": req.action, "explicit_reasons": req.explicit_reasons,
    }, ensure_ascii=False)
    try:
        raw_reflection_json = agent.generate_graph_reflection(
            history_text, explicit_reasons=req.explicit_reasons,
        )
        reflection_data = parse_json_object_from_text(raw_reflection_json)
        relationships = reflection_data.get("relationships")
        if not isinstance(relationships, list):
            raise ValueError("Missing feedback relationships")
        expected_relation = "DISLIKES_TRAIT" if req.action == "decline" else "LIKES_TRAIT"
        memories = []
        for rel in relationships:
            if not isinstance(rel, dict) or rel.get("relation_type") != expected_relation:
                raise ValueError("Invalid feedback relationship")
            label = rel.get("trait")
            if not isinstance(label, str) or not label.strip():
                raise ValueError("Missing feedback concept")
            label = label.strip()
            identity = canonicalize_fresh_concept(label)
            if not identity:
                raise ValueError("Missing feedback concept")
            if identity.key not in grounded:
                raise PreferenceTextError("feedback_ungrounded_concept")
            # Preserve the owner's complete normalized text, not model spelling.
            identity = grounded[identity.key]
            memories.append({
                **identity.as_dict(),
                "stance": "avoid" if req.action == "decline" else "like",
                "confidence": 1.0,
            })
    except Exception as exc:
        print(f"[FEEDBACK] normalization failed: {type(exc).__name__}")
        if isinstance(exc, PreferenceTextError) and exc.code == "feedback_ungrounded_concept":
            raise HTTPException(status_code=502, detail={"code": exc.code}) from None
        raise HTTPException(status_code=502, detail={"code": "feedback_normalization_failed"}) from None

    # The canonical memory writer owns validation and PREFERS/AVOIDS writes.
    # Batch without dropping a later user-selected reason.
    saved = []
    batch_limit = durable_memory_limit()
    if len(memories) > batch_limit:
        raise HTTPException(
            status_code=422,
            detail={"code": "feedback_memory_limit_exceeded"},
        )
    for offset in range(0, len(memories), batch_limit):
        outcome = await apply_memory(MemoryApplyRequest(
            user_id=req.user_id, memories=memories[offset:offset + batch_limit],
            surface="match_feedback",
            source_created_at=source_created_at,
        ))
        if outcome.get("status") != "success":
            raise HTTPException(status_code=503, detail={"code": "feedback_graph_unavailable"})
        saved.extend(outcome.get("memories") or [])
    # Social persists these already-normalized facts with match/source evidence.
    return {
        "status": "success" if saved else "no_preferences",
        "memories": saved,
        "message": "Agent feedback processed",
    }


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
    memories: list[dict] = Field(
        default_factory=list, max_length=HARD_DURABLE_MEMORY_LIMIT,
    )
    surface: str = "global"
    match_id: str | None = None
    message_id: str | None = None
    source_created_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class PreferenceCandidateRequest(BaseModel):
    requester_user_id: str = Field(min_length=1, max_length=128)
    topic: str = Field(min_length=1, max_length=MAX_PREFERENCE_TEXT_CHARS)
    canonical_key: str | None = Field(default=None, max_length=MAX_PREFERENCE_KEY_CHARS)
    canonicalization_version: str | None = Field(default=None, max_length=16)
    semantic_text: str | None = Field(default=None, max_length=MAX_PREFERENCE_TEXT_CHARS)
    semantic_input_hash: str | None = Field(default=None, max_length=64)
    excluded_user_ids: list[str] = Field(default_factory=list, max_length=200)
    limit: int = Field(default=20, ge=1, le=100)


class PreferenceSemanticCandidateRequest(BaseModel):
    requester_user_id: str = Field(min_length=1, max_length=128)
    topic: str = Field(min_length=1, max_length=MAX_PREFERENCE_TEXT_CHARS)
    canonical_key: str | None = Field(default=None, max_length=MAX_PREFERENCE_KEY_CHARS)
    canonicalization_version: str | None = Field(default=None, max_length=16)
    semantic_text: str | None = Field(default=None, max_length=MAX_PREFERENCE_TEXT_CHARS)
    semantic_input_hash: str | None = Field(default=None, max_length=64)
    query_embedding: list[float] | None = Field(default=None, max_length=768)
    embedding_model: str = Field(default="", max_length=100)
    excluded_user_ids: list[str] = Field(default_factory=list, max_length=200)
    neighbor_limit: int = Field(default=24, ge=1, le=32)
    concept_limit: int = Field(default=8, ge=1, le=12)
    per_concept_limit: int = Field(default=10, ge=1, le=20)
    candidate_limit: int = Field(default=40, ge=1, le=50)
    evidence_limit: int = Field(default=3, ge=1, le=5)
    min_similarity: float = Field(default=0.82, ge=0.75, le=1.0, allow_inf_nan=False)


class RelatedInterestCandidateRequest(PreferenceSemanticCandidateRequest):
    embedding_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    request_budget_seconds: float = Field(default=27.0, gt=0, le=27.0, allow_inf_nan=False)


@app.post("/api/preferences/related-interest-candidates")
def related_interest_candidates(req: RelatedInterestCandidateRequest):
    from matchmaker_agent.related_interest_canary import canary_requester_enabled
    from related_interest_retrieval import retrieve
    deadline = time.monotonic() + req.request_budget_seconds
    if not canary_requester_enabled(req.requester_user_id):
        return {"status": "error", "error_code": "semantic_policy_disabled", "candidates": [],
                "canonical_key": req.canonical_key or ""}
    # The middleware verifies the existing Social signature. A body containing
    # a canary ID cannot impersonate its owner or claim a background exemption.
    from agent_quota.service import SCOPE
    quota_scope = SCOPE.get()
    if not quota_scope or quota_scope[0] != req.requester_user_id or quota_scope[1] != "matching":
        raise HTTPException(status_code=403, detail="invalid_quota_context")
    try:
        identity = _preference_request_identity(req)
        if not identity:
            raise PreferenceTextError("preference_search_invalid")
        uri, auth, database = _neo4j_config()
        with GraphDatabase.driver(uri, auth=auth, connection_timeout=3.0,
                connection_acquisition_timeout=3.0, max_transaction_retry_time=0.0) as driver:
            with driver.session(database=database, default_access_mode="READ") as session:
                return retrieve(session, req, identity, agent.client, agent.model,
                    os.getenv("GOOGLE_EMBEDDING_MODEL", "models/gemini-embedding-2"), deadline=deadline)
    except PreferenceTextError as exc:
        return {"status": "error", "error_code": exc.code, "candidates": [], "retryable": False,
                "canonical_key": req.canonical_key or ""}
    except TimeoutError:
        return {"status": "error", "error_code": "semantic_retrieval_timeout", "candidates": [],
                "canonical_key": req.canonical_key or ""}
    except Exception:
        return {"status": "error", "error_code": "semantic_graph_unavailable", "candidates": [],
                "canonical_key": req.canonical_key or ""}


@app.get("/api/preferences/related-interest-readiness")
def related_interest_readiness():
    from related_interest_retrieval import readiness
    try:
        uri, auth, database = _neo4j_config()
        with GraphDatabase.driver(uri, auth=auth, connection_timeout=3.0,
                connection_acquisition_timeout=3.0, max_transaction_retry_time=0.0) as driver:
            with driver.session(database=database, default_access_mode="READ") as session:
                return {"status": "success", **readiness(session,
                    os.getenv("GOOGLE_EMBEDDING_MODEL", "models/gemini-embedding-2"))}
    except Exception:
        return {"status": "error", "error_code": "semantic_readiness_unconfirmed", "activation_approved": False}

class MemoryActionRequest(BaseModel):
    user_id: str
    key: str = Field(min_length=2, max_length=MAX_PREFERENCE_KEY_CHARS)
    action: str
    value: str | None = None
    source_created_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)

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
        if is_v2_preference_key(key) or not re.fullmatch(r"[a-z][a-z0-9_]{1,50}", key):
            key = _concept_key(label)
        clean.append({"key": key, "label": label})

    URI, AUTH, DATABASE = _neo4j_config()
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            with driver.session(database=DATABASE) as session:
                def write_context(tx):
                    lock_preferences(tx, req.user_id, source_created_at=now)
                    # Do not delete other owners' relations without their fence.
                    # Expired-context readers already enforce canonical expiry.
                    tx.run("""MATCH (u:User {id:$user_id})
                        OPTIONAL MATCH (u)-[old:CURRENTLY_WANTS]->()
                        DELETE old""", user_id=req.user_id).consume()
                    for item in clean:
                        existing = tx.run("""
                            MATCH (c:Concept)
                            WHERE toLower(c.label)=toLower($label)
                              AND coalesce(c.canonicalization_version, '') <> 'v2'
                              AND NOT (c.key =~ 'v2_[0-9a-f]{48}')
                            RETURN c.key AS key LIMIT 1
                        """, label=item["label"]).single()
                        key = str(existing["key"] if existing else item["key"])
                        if is_v2_preference_key(key):
                            key = _concept_key(item["label"])
                        tx.run("""
                            MATCH (u:User {id:$user_id})
                            MERGE (c:Concept {key:$key})
                            ON CREATE SET c.label=$label, c.kind='activity'
                            WITH u,c
                            WHERE coalesce(c.canonicalization_version, '') <> 'v2'
                            MERGE (u)-[r:CURRENTLY_WANTS]->(c)
                            SET r.expires_at=$expires_at
                        """, user_id=req.user_id, key=key, label=item["label"],
                             expires_at=expires_at).consume()
                    bump_preferences(tx, req.user_id)
                session.execute_write(write_context)
        return {
            "status": "success",
            "user_id": req.user_id,
            "concept_count": len(clean),
            "expires_at": expires_at,
            "revision": req.revision,
        }
    except Exception as exc:
        if isinstance(exc, PreferenceFenceError):
            return fence_error_result(exc)
        print(f"[CONTEXT_GRAPH][9001] projection failed error={type(exc).__name__}")
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

@app.post("/api/users/registration-projection")
def registration_projection(req: RegistrationProjection):
    URI, AUTH, DATABASE = _neo4j_config()
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            with driver.session(database=DATABASE) as session:
                session.run("CREATE CONSTRAINT registration_user_id IF NOT EXISTS FOR (u:User) REQUIRE u.id IS UNIQUE").consume()
                return session.execute_write(project_identity, req)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="registration_projection_unavailable") from exc


def _preference_request_identity(req):
    """Raw topics are fresh input; internal packets are verified without conversion."""
    fields = ("canonical_key", "canonicalization_version", "semantic_text", "semantic_input_hash")
    if any(getattr(req, name) is not None for name in fields):
        identity = stored_concept_identity({name: getattr(req, name) for name in fields})
        if not identity or req.topic != identity.semantic_text:
            raise PreferenceTextError("preference_search_identity_mismatch")
        return identity
    return canonicalize_fresh_concept(req.topic)


@app.post("/api/preferences/candidates")
def preference_candidates(req: PreferenceCandidateRequest):
    """Return bounded internal IDs with explicit positive durable evidence."""
    try:
        identity = _preference_request_identity(req)
    except PreferenceTextError as exc:
        return {"status": "error", "error_code": exc.code, "candidate_ids": [], "retryable": False}
    if not identity:
        return {"status": "skipped", "canonical_key": "", "candidate_ids": []}
    excluded = list(dict.fromkeys(
        str(value).strip()[:128]
        for value in req.excluded_user_ids
        if str(value or "").strip()
    ))[:200]
    URI, AUTH, DATABASE = _neo4j_config()
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            with driver.session(database=DATABASE) as session:
                concept = session.run("""
                    MATCH (concept:Concept {key:$key})
                    RETURN concept.key AS key,concept.semantic_text AS semantic_text,
                           concept.canonicalization_version AS canonicalization_version,
                           concept.semantic_input_hash AS semantic_input_hash,
                           concept.fidelity_status AS fidelity_status
                """, key=identity.key).single()
                verified = stored_concept_identity(dict(concept)) if concept else None
                rows = []
                if verified and verified.key == identity.key:
                    rows = session.run("""
                    MATCH (concept:Concept {key:$key})<-[:PREFERS]-(candidate:User)
                    WHERE candidate.id <> $requester_user_id
                      AND concept.canonicalization_version = 'v2'
                      AND concept.semantic_input_hash = $semantic_input_hash
                    WITH candidate
                    LIMIT $limit
                    RETURN candidate.id AS candidate_id
                """, key=identity.key, requester_user_id=req.requester_user_id,
                     semantic_input_hash=verified.semantic_input_hash, limit=req.limit)
                raw_candidate_ids = list(dict.fromkeys(
                    str(row["candidate_id"])
                    for row in rows if str(row.get("candidate_id") or "").strip()
                ))
                # Optional compatibility is owner-edge proof, never a short
                # global label. Both branches share one hard ID budget.
                remaining = max(0, req.limit - len(raw_candidate_ids))
                legacy = canonicalize_concept_v1(identity.semantic_text)
                if remaining and legacy:
                    legacy_rows = session.run("""
                        MATCH (concept:Concept {key:$legacy_key})<-[edge:PREFERS]-(candidate:User)
                        WHERE candidate.id <> $requester_user_id
                          AND coalesce(concept.canonicalization_version,'legacy_unknown') <> 'v2'
                        WITH concept,edge,candidate LIMIT $limit
                        RETURN candidate.id AS candidate_id,concept.key AS key,
                               coalesce(concept.canonicalization_version,'legacy_unknown') AS canonicalization_version,
                               edge.legacy_evidence_scope AS legacy_evidence_scope,
                               edge.legacy_fidelity_status AS legacy_fidelity_status,
                               edge.legacy_semantic_text AS legacy_semantic_text,
                               edge.legacy_semantic_input_hash AS legacy_semantic_input_hash
                    """, legacy_key=legacy.key, requester_user_id=req.requester_user_id, limit=remaining)
                    for row in legacy_rows:
                        proof = verified_legacy_identity(dict(row))
                        candidate_id = str(row.get("candidate_id") or "").strip()
                        if proof and proof.key == identity.key and candidate_id and candidate_id not in raw_candidate_ids:
                            raw_candidate_ids.append(candidate_id)
                candidate_ids = [
                    candidate_id for candidate_id in raw_candidate_ids
                    if candidate_id not in excluded
                ]
        return {
            "status": "success",
            "canonical_key": identity.key,
            "normalized_topic": identity.label,
            "candidate_ids": candidate_ids[:req.limit],
            "candidate_count_before_filter": len(raw_candidate_ids),
            "candidate_count_after_filter": len(candidate_ids),
        }
    except Exception as exc:
        print(f"[PREFERENCE_MATCH] graph_lookup_failed error={type(exc).__name__}")
        return {
            "status": "error", "error_code": "preference_graph_unavailable",
            "canonical_key": identity.key, "candidate_ids": [],
        }


def _semantic_index_metadata(session) -> dict:
    record = session.run(Query("""
        SHOW VECTOR INDEXES YIELD name, state, populationPercent, options, labelsOrTypes, properties
        WHERE name = $index_name
        RETURN name, state, populationPercent, options, labelsOrTypes, properties
    """, timeout=PREFERENCE_SEMANTIC_GRAPH_TIMEOUT_SECONDS),
        index_name=PREFERENCE_SEMANTIC_INDEX_NAME).single()
    if not record:
        return {
            "exists": False, "state": "MISSING", "dimension": 0,
            "population_percent": 0.0,
            "schema_valid": False,
        }
    options = record.get("options") if isinstance(record.get("options"), dict) else {}
    config = options.get("indexConfig") if isinstance(options.get("indexConfig"), dict) else {}
    try:
        dimension = int(config.get("vector.dimensions", 0) or 0)
    except (TypeError, ValueError):
        dimension = 0
    try:
        population = float(record.get("populationPercent", 0.0) or 0.0)
    except (TypeError, ValueError):
        population = 0.0
    return {
        "exists": True,
        "state": str(record.get("state") or "UNKNOWN")[:24].upper(),
        "dimension": dimension,
        "population_percent": max(0.0, min(population, 100.0)),
        "schema_valid": bool(record.get("labelsOrTypes") == ["Concept"]
                             and record.get("properties") == ["embedding"]
                             and str(config.get("vector.similarity_function", "")).lower() == "cosine"),
    }


def _valid_semantic_embedding(value) -> list[float] | None:
    if not isinstance(value, list) or len(value) != PREFERENCE_SEMANTIC_DIMENSIONS:
        return None
    try:
        vector = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in vector):
        return None
    magnitude = math.sqrt(sum(item * item for item in vector))
    return vector if math.isfinite(magnitude) and magnitude > 0.0 else None


@app.get("/api/preferences/semantic-readiness")
def preference_semantic_readiness():
    """Read-only readiness audit; it never creates indexes or writes Concepts."""
    URI, AUTH, DATABASE = _neo4j_config()
    try:
        with GraphDatabase.driver(URI, auth=AUTH, connection_timeout=3.0,
                                  connection_acquisition_timeout=3.0, max_transaction_retry_time=0.0) as driver:
            with driver.session(database=DATABASE, default_access_mode="READ") as session:
                index = _semantic_index_metadata(session)
                coverage = session.run(Query("""
                    MATCH (concept:Concept)<-[:PREFERS]-(:User)
                    WITH DISTINCT concept
                    RETURN count(concept) AS total,
                           sum(CASE WHEN concept.embedding IS NOT NULL
                               AND size(concept.embedding) = $dimensions
                               THEN 1 ELSE 0 END) AS embedded
                """, timeout=PREFERENCE_SEMANTIC_GRAPH_TIMEOUT_SECONDS),
                    dimensions=PREFERENCE_SEMANTIC_DIMENSIONS).single()
        total = int(coverage.get("total", 0) if coverage else 0)
        embedded = int(coverage.get("embedded", 0) if coverage else 0)
        retrieval_ready = bool(
            index["exists"] and index["state"] == "ONLINE"
            and index["dimension"] == PREFERENCE_SEMANTIC_DIMENSIONS
            and index["schema_valid"]
        )
        return {
            "status": "success",
            "index": index,
            "embedding_coverage": {
                "preference_concept_count": max(0, total),
                "embedded_preference_concept_count": max(0, embedded),
                "coverage_ratio": round(embedded / total, 4) if total else 0.0,
            },
            "expected_dimensions": PREFERENCE_SEMANTIC_DIMENSIONS,
            "expected_task": "semantic_similarity",
            # Historical nodes do not carry a model/task fingerprint.  An
            # operator must confirm one embedding space before active rollout.
            "historical_embedding_fingerprint": "unknown",
            "retrieval_ready": retrieval_ready,
            "active_enable_ready": False,
        }
    except Exception as exc:
        print(f"[PREFERENCE_SEMANTIC] readiness_failed error={type(exc).__name__}")
        return {
            "status": "error", "error_code": "semantic_readiness_unavailable",
            "historical_embedding_fingerprint": "unknown",
            "retrieval_ready": False, "active_enable_ready": False,
        }


@app.post("/api/preferences/semantic-candidates")
def preference_semantic_candidates(req: PreferenceSemanticCandidateRequest):
    """Read bounded semantic Concept neighbours and their explicit PREFERS owners."""
    try:
        identity = _preference_request_identity(req)
    except PreferenceTextError as exc:
        return {"status": "error", "error_code": exc.code, "candidates": [], "retryable": False}
    if not identity:
        return {"status": "skipped", "canonical_key": "", "candidates": []}
    excluded = sorted({
        str(value).strip()[:128]
        for value in req.excluded_user_ids
        if str(value or "").strip()
    })[:200]
    URI, AUTH, DATABASE = _neo4j_config()
    try:
        with GraphDatabase.driver(URI, auth=AUTH, connection_timeout=3.0,
                                  connection_acquisition_timeout=3.0, max_transaction_retry_time=0.0) as driver:
            with driver.session(database=DATABASE, default_access_mode="READ") as session:
                index = _semantic_index_metadata(session)
                if not (
                    index["exists"] and index["state"] == "ONLINE"
                    and index["dimension"] == PREFERENCE_SEMANTIC_DIMENSIONS
                    and index["schema_valid"]
                ):
                    return {
                        "status": "error", "error_code": "semantic_index_unavailable",
                        "canonical_key": identity.key, "candidates": [], "index": index,
                    }
                query_embedding = _valid_semantic_embedding(req.query_embedding)
                embedding_source = "request"
                if query_embedding is None and req.query_embedding is None:
                    row = session.run(Query("""
                        MATCH (concept:Concept {key:$key})
                        RETURN concept.embedding AS embedding,
                               concept.embedding_model AS model,
                               concept.embedding_task AS task,
                               concept.key AS key,concept.semantic_text AS semantic_text,
                               concept.canonicalization_version AS canonicalization_version,
                               concept.semantic_input_hash AS semantic_input_hash,
                               concept.embedding_source_hash AS embedding_source_hash,
                               concept.fidelity_status AS fidelity_status
                    """, timeout=PREFERENCE_SEMANTIC_GRAPH_TIMEOUT_SECONDS),
                        key=identity.key).single()
                    source_identity = stored_concept_identity(dict(row)) if row else None
                    if (source_identity and req.embedding_model
                            and row.get("model") == req.embedding_model
                            and row.get("task") == "semantic_similarity"
                            and row.get("embedding_source_hash") == source_identity.semantic_input_hash):
                        query_embedding = _valid_semantic_embedding(row.get("embedding"))
                    embedding_source = "concept"
                if query_embedding is None:
                    if req.query_embedding is not None:
                        return {
                            "status": "error", "error_code": "semantic_query_embedding_invalid",
                            "canonical_key": identity.key, "candidates": [],
                        }
                    return {
                        "status": "query_embedding_required",
                        "canonical_key": identity.key,
                        "normalized_topic": identity.label,
                        "candidates": [],
                    }
                neighbours = list(session.run(Query("""
                    CALL db.index.vector.queryNodes(
                        $index_name, $neighbor_limit, $query_embedding
                    ) YIELD node AS concept, score
                    WHERE score >= $min_similarity
                      AND concept.key <> $canonical_key
                      AND concept.canonicalization_version = 'v2'
                      AND EXISTS { MATCH (concept)<-[:PREFERS]-(:User) }
                    WITH concept, score
                    ORDER BY score DESC, concept.key ASC
                    LIMIT $concept_limit
                    RETURN concept.key AS concept_key, score AS similarity,
                           concept.embedding_model AS model, concept.embedding_task AS task,
                           concept.key AS key,concept.semantic_text AS semantic_text,
                           concept.canonicalization_version AS canonicalization_version,
                           concept.semantic_input_hash AS semantic_input_hash,
                           concept.embedding_source_hash AS embedding_source_hash,
                           concept.fidelity_status AS fidelity_status
                """, timeout=PREFERENCE_SEMANTIC_GRAPH_TIMEOUT_SECONDS),
                    index_name=PREFERENCE_SEMANTIC_INDEX_NAME,
                    neighbor_limit=req.neighbor_limit,
                    query_embedding=query_embedding,
                    min_similarity=req.min_similarity,
                    canonical_key=identity.key,
                    concept_limit=req.concept_limit,
                ))
                # Missing historical provenance cannot be waived with an env
                # flag. Inspect only the bounded ANN result, never the corpus.
                if any(not req.embedding_model or row.get("model") != req.embedding_model
                       or row.get("task") != "semantic_similarity" for row in neighbours):
                    return {
                        "status": "error", "error_code": "semantic_readiness_unconfirmed",
                        "canonical_key": identity.key, "candidates": [],
                    }
                if any(not stored_concept_identity(dict(row))
                       or row.get("embedding_source_hash") != row.get("semantic_input_hash")
                       for row in neighbours):
                    return {"status": "error", "error_code": "semantic_identity_unverified",
                            "canonical_key": identity.key, "candidates": []}
                concepts = [{"concept_key": row["concept_key"],
                             "semantic_input_hash": row["semantic_input_hash"],
                             "similarity": float(row["similarity"])} for row in neighbours]
                rows = session.run(Query("""
                    UNWIND $concepts AS hit
                    MATCH (concept:Concept {key:hit.concept_key})
                    WHERE concept.canonicalization_version='v2'
                      AND concept.semantic_input_hash=hit.semantic_input_hash
                    WITH concept, hit.similarity AS score
                    CALL {
                        WITH concept
                        MATCH (concept)<-[:PREFERS]-(candidate:User)
                        WITH candidate
                        LIMIT $per_concept_limit
                        WITH candidate
                        WHERE candidate.id <> $requester_user_id
                          AND NOT (candidate.id IN $excluded_user_ids)
                        RETURN candidate
                    }
                    WITH candidate, concept, score
                    ORDER BY candidate.id ASC, score DESC, concept.key ASC
                    WITH candidate,
                         collect({concept_key:concept.key, similarity:score})[0..$evidence_limit]
                         AS evidence
                    WITH candidate, evidence, evidence[0].similarity AS best_score
                    ORDER BY best_score DESC, candidate.id ASC
                    LIMIT $candidate_limit
                    RETURN candidate.id AS candidate_id, evidence, best_score
                """, timeout=PREFERENCE_SEMANTIC_GRAPH_TIMEOUT_SECONDS),
                    concepts=concepts,
                    requester_user_id=req.requester_user_id,
                    excluded_user_ids=excluded,
                    per_concept_limit=req.per_concept_limit,
                    evidence_limit=req.evidence_limit,
                    candidate_limit=req.candidate_limit,
                )
                candidates = []
                considered: dict[str, float] = {
                    hit["concept_key"]: round(hit["similarity"], 4) for hit in concepts
                }
                for row in rows:
                    candidate_id = str(row.get("candidate_id") or "").strip()[:128]
                    if not candidate_id:
                        continue
                    evidence = []
                    for item in list(row.get("evidence") or [])[:req.evidence_limit]:
                        key = str(item.get("concept_key") or "").strip()
                        try:
                            similarity = float(item.get("similarity", 0.0))
                        except (TypeError, ValueError):
                            continue
                        if (re.fullmatch(r"[a-z][a-z0-9_]{1,50}", key)
                                and math.isfinite(similarity)
                                and req.min_similarity <= similarity <= 1.0):
                            similarity = round(similarity, 4)
                            evidence.append({
                                "concept_key": key,
                                "similarity": similarity,
                                "kind": "semantic_related",
                            })
                            considered[key] = max(considered.get(key, 0.0), similarity)
                    evidence.sort(key=lambda item: (-item["similarity"], item["concept_key"]))
                    if evidence:
                        candidates.append({
                            "candidate_id": candidate_id,
                            "best_similarity": evidence[0]["similarity"],
                            "evidence": evidence,
                        })
                candidates.sort(key=lambda item: (-item["best_similarity"], item["candidate_id"]))
        concepts = [
            {"concept_key": key, "similarity": score}
            for key, score in sorted(considered.items(), key=lambda item: (-item[1], item[0]))
        ][:req.concept_limit]
        return {
            "status": "success",
            "canonical_key": identity.key,
            "normalized_topic": identity.label,
            "retrieval_source": "graph_semantic",
            "embedding_source": embedding_source,
            "candidates": candidates[:req.candidate_limit],
            "semantic_concepts_considered": concepts,
            "candidate_count": min(len(candidates), req.candidate_limit),
        }
    except Exception as exc:
        print(f"[PREFERENCE_SEMANTIC] graph_lookup_failed error={type(exc).__name__}")
        return {
            "status": "error", "error_code": "semantic_graph_unavailable",
            "canonical_key": identity.key, "candidates": [],
        }


@app.post("/api/v2/memory/apply")
@app.post("/api/memory/apply")
async def apply_memory(req: MemoryApplyRequest):
    """Atomically write validated proposals and their idempotency marker."""
    allowed_stances = {"like", "dislike", "require", "avoid"}
    protected = re.compile(r"(?:黑人|白人|黃種人|種族|族裔|宗教|信仰|穆斯林|基督教|同性戀|性傾向|性別認同|跨性別|殘障|身心障礙|疾病|政治立場|國籍|公民身分)", re.I)
    key_re = re.compile(r"^[a-z][a-z0-9_]{1,50}$")
    now, clean_by_key = time.time(), {}
    memory_limit = durable_memory_limit()
    if len(req.memories) > memory_limit:
        return {
            "memories": [], "status": "error",
            "error_code": "memory_limit_exceeded", "retryable": False,
        }
    try:
        sources = [normalize_preference_text(
            item.get("semantic_text") or item.get("label") or item.get("label_zh_tw") or ""
        ) for item in req.memories]
        if req.surface == "registration_interest":
            for item in req.memories:
                normalize_preference_text(item.get("evidence_span") or "", max_length=MAX_PREFERENCE_EVIDENCE_CHARS)
    except PreferenceTextError as exc:
        return {"memories": [], "status": "error", "error_code": exc.code, "retryable": False}
    if any(not stored_concept_identity(item) for item in req.memories):
        return {"memories": [], "status": "error", "error_code": "preference_identity_unverified", "retryable": False}
    for item, source in zip(req.memories, sources):
        if has_mixed_preference_polarity(source):
            continue
        label = re.sub(
            r"^(?:喜歡|討厭|不喜歡|偏好|近期情境)\s*[:：,，]?\s*", "",
            source,
        )
        stance = str(item.get("stance", ""))
        try:
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            continue
        if has_mixed_preference_polarity(label):
            continue
        try:
            labels = (
                split_explicit_preference_enumeration(label, limit=HARD_DURABLE_MEMORY_LIMIT)
                or split_compound_concept_label(label, limit=HARD_DURABLE_MEMORY_LIMIT)
                or [label]
            )
        except PreferenceTextError as exc:
            return {"memories": [], "status": "error", "error_code": exc.code, "retryable": False}
        for atomic_label in labels:
            identity = canonicalize_concept(atomic_label, item.get("key"))
            if (
                not identity or not key_re.match(identity.key) or protected.search(identity.label)
                or stance not in allowed_stances or not math.isfinite(confidence) or confidence < 0.75
            ):
                continue
            clean_by_key.setdefault(identity.key, {
                **identity.as_dict(), "stance": stance,
                "category": "preference",
                "confidence": confidence, "last_seen_at": now,
            })
    if len(clean_by_key) > memory_limit:
        return {"memories": [], "status": "error", "error_code": "memory_limit_exceeded", "retryable": False}
    clean = list(clean_by_key.values())
    if not clean:
        return {"memories": [], "status": "skipped"}
    URI, AUTH, DATABASE = _neo4j_config()
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            with driver.session(database=DATABASE) as session:
                session.run("CREATE CONSTRAINT memory_observation_message_id IF NOT EXISTS FOR (o:MemoryObservation) REQUIRE o.message_id IS UNIQUE").consume()

                if req.surface == "registration_interest":
                    seeded = session.execute_write(seed_registration, req.user_id, req.message_id, req.memories)
                    return {"memories": seeded, "status": "success" if seeded else "skipped"}

                def write_memory(tx):
                    lock_preferences(tx, req.user_id, source_created_at=req.source_created_at)
                    assert_existing_preference_identities(tx, clean)
                    if req.message_id:
                        observed = tx.run("""
                            MERGE (u:User {id:$user_id})
                            SET u.registration_projection_lock=coalesce(u.registration_projection_lock,0)+1
                            WITH u
                            MERGE (o:MemoryObservation {message_id:$message_id})
                            ON CREATE SET o.owner_user_id=$user_id,o.created_at=$now
                            RETURN o.created_at=$now AS created
                        """, message_id=req.message_id, user_id=req.user_id, now=now).single()
                        if not observed or not observed["created"]:
                            return False
                    write_preference_edges(tx, req.user_id, clean)
                    bump_preferences(tx, req.user_id)
                    return True

                created = session.execute_write(write_memory)
        return {
            "memories": clean,
            "status": "success" if created else "duplicate",
        }
    except Exception as exc:
        if isinstance(exc, PreferenceFenceError):
            return {"memories": [], **fence_error_result(exc)}
        if isinstance(exc, PreferenceTextError):
            return {"memories": [], "status": "error", "error_code": exc.code, "retryable": False}
        if str(exc) == "preference_identity_conflict":
            return {"memories": [], "status": "error", "error_code": "preference_identity_conflict", "retryable": False}
        print(f"[MEMORY][9001 apply] graph_write_failed error={type(exc).__name__}")
        return {"memories": [], "status": "error", "error_code": type(exc).__name__}
@app.get("/api/memory/{user_id}")
async def list_memories(user_id: str, limit: int = 12, durable_only: bool = False, query: str = ""):
    try:
        query = normalize_preference_text(query, max_length=MAX_OWNER_MEMORY_QUERY_CHARS)
    except PreferenceTextError:
        return {"status": "invalid_query", "error_code": "query_text_too_long", "memories": [], "truncated": False}
    try:
        words = re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9_]+", query.lower())
        terms = list(dict.fromkeys(term for word in words for term in
                     ([word[i:i + 2] for i in range(len(word) - 1)] if re.fullmatch(r"[\u4e00-\u9fff]+", word) else [word])))[:24]
        safe_limit = max(1, min(limit, 30))
        URI, AUTH, DATABASE = _neo4j_config()
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            with driver.session(database=DATABASE) as session:
                rows = session.run("""
                    MATCH (u:User {id:$user_id})-[r:PREFERS|AVOIDS|CURRENTLY_WANTS]->(c:Concept)
                    WHERE type(r) <> 'CURRENTLY_WANTS'
                       OR (NOT $durable_only AND coalesce(r.expires_at, 0) > $now)
                    WITH c, r, reduce(score=0, term IN $terms |
                        score + CASE WHEN toLower(coalesce(c.semantic_text, c.label, c.key, '')) CONTAINS term
                                          OR toLower(coalesce(c.key, '')) CONTAINS term
                                     THEN 1 ELSE 0 END) AS relevance
                    WHERE size($terms)=0 OR relevance > 0
                    RETURN c.key AS key,
                           coalesce(c.label, c.key) AS label,
                           c.semantic_text AS semantic_text,
                           c.display_label AS display_label,
                           coalesce(c.canonicalization_version, 'legacy_unknown') AS canonicalization_version,
                           c.semantic_input_hash AS semantic_input_hash,
                           c.fidelity_status AS fidelity_status,
                           r.legacy_evidence_scope AS legacy_evidence_scope,
                           r.legacy_fidelity_status AS legacy_fidelity_status,
                           r.legacy_semantic_text AS legacy_semantic_text,
                           r.legacy_semantic_input_hash AS legacy_semantic_input_hash,
                           CASE type(r)
                               WHEN 'AVOIDS' THEN 'dislike'
                               WHEN 'PREFERS' THEN 'like'
                               WHEN 'CURRENTLY_WANTS' THEN 'want'
                               ELSE 'like'
                           END AS stance,
                           coalesce(c.kind, 'preference') AS category,
                           1.0 AS confidence,
                           coalesce(r.last_seen_at, 0) AS last_seen_at
                    ORDER BY relevance DESC, CASE WHEN stance='dislike' THEN 0 ELSE 1 END,
                             last_seen_at DESC, key ASC
                    LIMIT $limit
                """, user_id=user_id, limit=safe_limit + 1, durable_only=durable_only, now=time.time(), terms=terms)
                items = [dict(row) for row in rows]
                for item in items:
                    identity = stored_concept_identity(item)
                    if identity:
                        item.update(identity.as_dict())
                    else:
                        item["fidelity_status"] = "legacy_unknown"
                return {"status": "success", "memories": items[:safe_limit], "truncated": len(items) > safe_limit}
    except Exception as exc:
        print(f"[MEMORY][9001] graph_read_failed error={type(exc).__name__}")
        return {"status": "error", "error_code": "graph_read_failed", "memories": []}

@app.post("/api/v2/memory/action")
@app.post("/api/memory/action")
async def memory_action(req: MemoryActionRequest):
    if req.action not in {"disable", "restore", "correct"}:
        return {"status": "error", "error_code": "unsupported_action"}
    user_id = re.sub(r"\s+", "", str(req.user_id or ""))[:80]
    key = str(req.key or "").strip().lower()
    if not user_id or not key:
        return {"status": "error", "error_code": "invalid_memory_reference"}
    identity = None
    if req.action == "correct":
        # Correction is an explicit owner action, not an implicit legacy re-key.
        if not is_v2_preference_key(key):
            return {"status": "error", "error_code": "legacy_identity_unverified",
                    "reconfirmation_required": True, "retryable": False}
        try:
            label = normalize_fresh_preference_text(req.value or "")
        except PreferenceTextError as exc:
            return {"status": "error", "error_code": exc.code, "retryable": False}
        protected = re.compile(
            r"(?:黑人|白人|黃種人|種族|族裔|宗教|信仰|穆斯林|基督教|同性戀|性傾向|性別認同|跨性別|殘障|身心障礙|疾病|政治立場|國籍|公民身分)",
            re.I,
        )
        try:
            invalid = (not label or protected.search(label) or has_mixed_preference_polarity(label)
                       or split_explicit_preference_enumeration(label)
                       or split_compound_concept_label(label))
        except PreferenceTextError as exc:
            return {"status": "error", "error_code": exc.code, "retryable": False}
        if invalid:
            return {"status": "error", "error_code": "invalid_correction"}
        identity = canonicalize_concept(label)
        if not identity:
            return {"status": "error", "error_code": "invalid_correction"}
    now = time.time()
    URI, AUTH, DATABASE = _neo4j_config()
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            with driver.session(database=DATABASE) as session:
                if req.action == "disable":
                    def disable(tx):
                        lock_preferences(tx, user_id, source_created_at=req.source_created_at, require_existing=True)
                        row = tx.run("""
                            MATCH (u:User {id:$user_id})-[active:PREFERS|AVOIDS|CURRENTLY_WANTS]->
                                  (concept:Concept {key:$key})
                            WITH u,concept,active,type(active) AS original_relation,
                                 active.expires_at AS original_expires_at
                            MERGE (u)-[disabled:MEMORY_DISABLED]->(concept)
                            SET disabled.original_relation=original_relation,
                                disabled.original_expires_at=original_expires_at,
                                disabled.disabled_at=$now
                            DELETE active
                            RETURN original_relation
                        """, user_id=user_id, key=key, now=now).single()
                        if row:
                            bump_preferences(tx, user_id)
                        return dict(row) if row else None
                    changed = session.execute_write(disable)
                    return {"status": "success" if changed else "not_found"}

                if req.action == "restore":
                    def restore(tx):
                        lock_preferences(tx, user_id, source_created_at=req.source_created_at, require_existing=True)
                        row = tx.run("""
                            MATCH (u:User {id:$user_id})-[disabled:MEMORY_DISABLED]->
                                  (concept:Concept {key:$key})
                            WITH u,concept,disabled,
                                 disabled.original_relation AS original_relation,
                                 disabled.original_expires_at AS original_expires_at
                            DELETE disabled
                            FOREACH (_ IN CASE WHEN original_relation='PREFERS' THEN [1] ELSE [] END |
                                MERGE (u)-[:PREFERS]->(concept))
                            FOREACH (_ IN CASE WHEN original_relation='AVOIDS' THEN [1] ELSE [] END |
                                MERGE (u)-[:AVOIDS]->(concept))
                            FOREACH (_ IN CASE WHEN original_relation='CURRENTLY_WANTS'
                                                   AND coalesce(original_expires_at,0)>$now
                                              THEN [1] ELSE [] END |
                                MERGE (u)-[intent:CURRENTLY_WANTS]->(concept)
                                SET intent.expires_at=original_expires_at)
                            RETURN original_relation,original_expires_at
                        """, user_id=user_id, key=key, now=now).single()
                        if row:
                            bump_preferences(tx, user_id)
                        return dict(row) if row else None
                    restored = session.execute_write(restore)
                    if not restored:
                        return {"status": "not_found"}
                    if (
                        restored.get("original_relation") == "CURRENTLY_WANTS"
                        and float(restored.get("original_expires_at") or 0) <= now
                    ):
                        return {"status": "expired"}
                    return {"status": "success"}

                corrected_key, label = identity.key, identity.label

                def correct(tx):
                    lock_preferences(tx, user_id, source_created_at=req.source_created_at, require_existing=True)
                    original = tx.run("""
                        MATCH (:User {id:$user_id})-[:PREFERS|AVOIDS|CURRENTLY_WANTS|MEMORY_DISABLED]->
                              (old:Concept {key:$key})
                        RETURN old.key AS key,old.semantic_text AS semantic_text,
                               old.canonicalization_version AS canonicalization_version,
                               old.semantic_input_hash AS semantic_input_hash,
                               old.fidelity_status AS fidelity_status
                        LIMIT 1
                    """, user_id=user_id, key=key).single()
                    if not original:
                        return None
                    if not stored_concept_identity(dict(original)):
                        raise ValueError("legacy_identity_unverified")
                    if corrected_key == key:
                        # An alias/case-only correction must not MERGE then
                        # DELETE the very same owner's relationship.
                        return {"relation": "unchanged"}
                    assert_existing_preference_identities(tx, [identity.as_dict()])
                    row = tx.run("""
                        MATCH (u:User {id:$user_id})-[existing:PREFERS|AVOIDS|CURRENTLY_WANTS|MEMORY_DISABLED]->
                              (old:Concept {key:$key})
                        WITH u,old,existing,type(existing) AS relation,
                             existing.expires_at AS expires_at,
                             existing.original_relation AS original_relation,
                             existing.original_expires_at AS original_expires_at
                        MERGE (corrected:Concept {key:$corrected_key})
                        ON CREATE SET corrected.label=$label,
                            corrected.semantic_text=$label,corrected.display_label=$display_label,
                            corrected.canonicalization_version='v2',
                            corrected.semantic_input_hash=$semantic_input_hash,
                            corrected.fidelity_status='complete',
                            corrected.kind=coalesce(old.kind,'preference')
                        FOREACH (_ IN CASE WHEN relation='PREFERS' THEN [1] ELSE [] END |
                            MERGE (u)-[:PREFERS]->(corrected))
                        FOREACH (_ IN CASE WHEN relation='AVOIDS' THEN [1] ELSE [] END |
                            MERGE (u)-[:AVOIDS]->(corrected))
                        FOREACH (_ IN CASE WHEN relation='CURRENTLY_WANTS' THEN [1] ELSE [] END |
                            MERGE (u)-[intent:CURRENTLY_WANTS]->(corrected)
                            SET intent.expires_at=expires_at)
                        FOREACH (_ IN CASE WHEN relation='MEMORY_DISABLED' THEN [1] ELSE [] END |
                            MERGE (u)-[disabled:MEMORY_DISABLED]->(corrected)
                            SET disabled.original_relation=original_relation,
                                disabled.original_expires_at=original_expires_at,
                                disabled.disabled_at=$now)
                        DELETE existing
                        RETURN relation
                    """, user_id=user_id, key=key, corrected_key=corrected_key,
                         label=label, display_label=identity.display_label,
                         semantic_input_hash=identity.semantic_input_hash, now=now).single()
                    if row:
                        bump_preferences(tx, user_id)
                    return dict(row) if row else None
                corrected = session.execute_write(correct)
                return {
                    "status": "success" if corrected else "not_found",
                    "key": corrected_key if corrected else key,
                }
    except Exception as exc:
        print(f"[MEMORY][9001 action] failed error={type(exc).__name__}")
        if isinstance(exc, PreferenceFenceError):
            if exc.code == "preference_owner_missing":
                return {"status": "not_found"}
            return fence_error_result(exc)
        code = "legacy_identity_unverified" if str(exc) == "legacy_identity_unverified" else "memory_action_failed"
        return {"status": "error", "error_code": code}
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
