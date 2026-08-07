import sys
from fastapi import FastAPI
from routers import calendar, chat, match, system, frontend
from routers.match import ensure_match_indexes
from services.calendar_service import ensure_calendar_indexes
from services.ayue_agent.v3.scheduler import ensure_indexes as ensure_ayue_agent_indexes
from services.ayue_agent.maps_client import ensure_map_cache_indexes
from services.profile_skills import ensure_profile_skill_indexes
from services.ayue_agent.proactive_scheduler import start_proactive_care_scheduler, stop_proactive_care_scheduler
from services.match_search_job_service import start_match_search_worker, stop_match_search_worker

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
    ensure_ayue_agent_indexes()
    ensure_map_cache_indexes()
    ensure_profile_skill_indexes()
    ensure_match_indexes()
    start_match_search_worker()
    start_proactive_care_scheduler()


@app.on_event("shutdown")
def stop_background_services():
    stop_proactive_care_scheduler()
    stop_match_search_worker()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
