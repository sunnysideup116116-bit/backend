import sys
from fastapi import FastAPI
from routers import calendar, chat, match, system, frontend
from routers.match import ensure_match_indexes
from services.calendar_service import ensure_calendar_indexes
from services.ayue_agent.v3.scheduler import ensure_indexes as ensure_ayue_agent_indexes
from services.ayue_agent.maps_client import ensure_map_cache_indexes
from services.profile_skills import ensure_profile_skill_indexes
from services.ayue_agent.proactive_scheduler import start_proactive_care_scheduler, stop_proactive_care_scheduler
from services.ayue_agent.v3.calendar_drafts import ensure_indexes as ensure_calendar_draft_indexes
from services.ayue_agent.v3.calendar_references import ensure_indexes as ensure_calendar_reference_indexes
from services.ayue_agent.v3.relationship_references import ensure_indexes as ensure_relationship_reference_indexes
from services.match_search_job_service import start_match_search_worker, stop_match_search_worker
from services.conversation_compaction_service import ensure_conversation_compaction_indexes
from services.context_graph_service import (
    ensure_context_graph_indexes, start_context_graph_worker, stop_context_graph_worker,
)
from services.event_discovery_job_service import ensure_event_discovery_job_indexes
from services.event_discovery_service import ensure_event_discovery_cache_indexes
from services.interactive_priority_service import ensure_interactive_priority_indexes
from services.concept_embedding_service import (
    start_concept_embedding_worker, stop_concept_embedding_worker,
)
from services.event_opportunity_service import ensure_event_opportunity_indexes
from services.event_lifecycle_service import (
    start_event_lifecycle_worker, stop_event_lifecycle_worker,
)
from event_worker import (
    start_event_discovery_worker, stop_event_discovery_worker,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

app = FastAPI(title="Profiling System API", description="AI Matchmaker API Backend")

app.include_router(frontend.router)
app.include_router(chat.router)
app.include_router(match.router)
app.include_router(system.router)
app.include_router(calendar.router)

@app.on_event("startup")
def setup_calendar_indexes():
    ensure_calendar_indexes()
    ensure_calendar_draft_indexes()
    ensure_calendar_reference_indexes()
    ensure_relationship_reference_indexes()
    ensure_ayue_agent_indexes()
    ensure_map_cache_indexes()
    ensure_conversation_compaction_indexes()
    ensure_profile_skill_indexes()
    ensure_context_graph_indexes()
    ensure_match_indexes()
    ensure_event_opportunity_indexes()
    ensure_event_discovery_job_indexes()
    ensure_event_discovery_cache_indexes()
    ensure_interactive_priority_indexes()
    start_match_search_worker()
    start_proactive_care_scheduler()
    start_context_graph_worker()
    start_concept_embedding_worker()
    start_event_lifecycle_worker()
    start_event_discovery_worker()


@app.on_event("shutdown")
def stop_background_services():
    stop_proactive_care_scheduler()
    stop_match_search_worker()
    stop_context_graph_worker()
    stop_concept_embedding_worker()
    stop_event_lifecycle_worker()
    stop_event_discovery_worker()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
