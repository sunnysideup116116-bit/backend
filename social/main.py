import os
import sys
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from services.codex_chat_provider import shutdown as shutdown_codex_provider
from routers import calendar, chat, match, places, system, frontend, google_calendar
from routers.match import ensure_match_indexes
from routers.chat_messages import ensure_chat_read_indexes
from services.calendar_service import ensure_calendar_indexes
from services.google_calendar_service import ensure_google_calendar_indexes
from services.ayue_agent.shared.runtime_state import ensure_indexes as ensure_ayue_agent_indexes
from services.ayue_agent.maps_client import ensure_map_cache_indexes
from services.profile_skills import ensure_profile_skill_indexes
from services.profile_task_service import start_profile_retry_worker, stop_profile_retry_worker
from services.ayue_agent.proactive_scheduler import start_proactive_care_scheduler, stop_proactive_care_scheduler
from services.ayue_agent.shared.calendar_state import ensure_indexes as ensure_calendar_reference_indexes
from services.ayue_agent.shared.date_coordination_state import ensure_indexes as ensure_date_coordination_reference_indexes
from services.ayue_agent.shared.place_history import ensure_indexes as ensure_place_reference_indexes
from services.proactive_followup_service import ensure_indexes as ensure_proactive_followup_indexes
from services.post_date_followup_service import (
    ensure_indexes as ensure_post_date_followup_indexes,
)
from services.relationship_memory_service import (
    ensure_indexes as ensure_relationship_memory_indexes,
    start_relationship_memory_worker,
    stop_relationship_memory_worker,
)
from services.match_search_job_service import start_match_search_worker, stop_match_search_worker
from services.conversation_compaction_service import ensure_conversation_compaction_indexes
from services.memory_outbox_service import (
    ensure_memory_outbox_indexes, start_memory_outbox_worker,
    stop_memory_outbox_worker,
)
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
from services.event_delivery_service import start_event_delivery_worker, stop_event_delivery_worker
from services.match_quota_service import ensure_match_quota_indexes
from services.event_lifecycle_service import (
    start_event_lifecycle_worker, stop_event_lifecycle_worker,
)
from event_worker import (
    start_event_discovery_worker, stop_event_discovery_worker,
)
from registration_voice import router as registration_voice_router
from app_voice_assistant import router as app_voice_assistant_router
from routers.relationship_memories import router as relationship_memories_router
from routers.conversation_summaries import router as conversation_summaries_router
from services.conversation_summary_operations import start_summary_worker, stop_summary_worker

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

app = FastAPI(title="Profiling System API", description="AI Matchmaker API Backend")

# CORS: allow browser clients (e.g. the Flutter web build hosted on GitHub
# Pages) to call this API. Mobile apps are unaffected by CORS.
# With allow_credentials=True the browser requires explicit origins (no
# wildcard), so GitHub Pages origins are matched via regex; additional
# origins can be supplied via the CORS_ORIGINS env var (comma-separated).
_cors_origins = [
    origin.strip()
    for origin in os.environ.get("CORS_ORIGINS", "").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=r"https://.*\.github\.io",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(frontend.router)
app.include_router(chat.router)
app.include_router(match.router)
app.include_router(places.router)
app.include_router(system.router)
app.include_router(calendar.router)
app.include_router(google_calendar.router)
app.include_router(registration_voice_router)
app.include_router(app_voice_assistant_router)
app.include_router(relationship_memories_router, prefix="/api", tags=["Relationship memories"])
app.include_router(conversation_summaries_router, prefix="/api", tags=["Conversation summaries"])

@app.on_event("startup")
def setup_calendar_indexes():
    ensure_chat_read_indexes()
    ensure_calendar_indexes()
    ensure_google_calendar_indexes()
    ensure_calendar_reference_indexes()
    ensure_date_coordination_reference_indexes()
    ensure_place_reference_indexes()
    ensure_proactive_followup_indexes()
    ensure_post_date_followup_indexes()
    ensure_relationship_memory_indexes()
    ensure_ayue_agent_indexes()
    ensure_map_cache_indexes()
    ensure_conversation_compaction_indexes()
    ensure_memory_outbox_indexes()
    ensure_profile_skill_indexes()
    ensure_context_graph_indexes()
    ensure_match_indexes()
    ensure_event_opportunity_indexes()
    ensure_match_quota_indexes()
    ensure_event_discovery_job_indexes()
    ensure_event_discovery_cache_indexes()
    ensure_interactive_priority_indexes()
    start_match_search_worker()
    start_memory_outbox_worker()
    start_profile_retry_worker()
    start_proactive_care_scheduler()
    start_relationship_memory_worker()
    start_context_graph_worker()
    start_concept_embedding_worker()
    start_event_lifecycle_worker()
    start_event_discovery_worker()
    start_event_delivery_worker()
    start_summary_worker()


@app.on_event("shutdown")
def stop_background_services():
    stop_summary_worker()
    shutdown_codex_provider()
    stop_event_delivery_worker()
    stop_proactive_care_scheduler()
    stop_relationship_memory_worker()
    stop_memory_outbox_worker()
    stop_profile_retry_worker()
    stop_match_search_worker()
    stop_context_graph_worker()
    stop_concept_embedding_worker()
    stop_event_lifecycle_worker()
    stop_event_discovery_worker()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
