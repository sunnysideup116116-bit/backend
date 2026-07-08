"""
Project unified entry for the scheme-A backend layout.

This process serves social_demotest on port 8000 and mounts NEW_AI_GEN when
available. The risk backend and matchmaker agent remain independent services:
- risk_backend/main.py -> 8001
- matchmaker_agent/agent_api.py -> 9001
"""
import atexit
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


PROJECT_ROOT = Path(__file__).resolve().parent
SOCIAL_ROOT = PROJECT_ROOT / "social_demotest"
AI_GEN_ROOT = PROJECT_ROOT / "NEW_AI_GEN"

for path in (str(SOCIAL_ROOT), str(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


app = FastAPI(
    title="Dating App Project API",
    description=(
        "Scheme-A Project entry. social_demotest is mounted in-process; "
        "matchmaker_agent and risk_backend run as separate local services."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "social_demotest": "mounted",
        "matchmaker_agent": "external:http://127.0.0.1:9001",
        "risk_backend": "external:http://127.0.0.1:8001",
    }


from routers import chat, frontend, match, system  # noqa: E402

app.include_router(frontend.router)
app.include_router(chat.router)
app.include_router(match.router)
app.include_router(system.router)

try:
    if str(AI_GEN_ROOT) not in sys.path:
        sys.path.insert(0, str(AI_GEN_ROOT))
    from app import on_exit as ai_gen_on_exit  # type: ignore  # noqa: E402
    from app import router as ai_gen_router  # type: ignore  # noqa: E402

    app.include_router(ai_gen_router, prefix="/ai-gen", tags=["AI Gen"])
    atexit.register(ai_gen_on_exit)
except Exception as exc:
    print(f"NEW_AI_GEN router not mounted: {exc}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
