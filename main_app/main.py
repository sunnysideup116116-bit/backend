import sys
import atexit
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from routers import admin, chat, frontend, match, system

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

app = FastAPI(title="Profiling System API", description="AI Matchmaker API Backend")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "main_app": "mounted",
        "matchmaker_agent": "external:http://127.0.0.1:9001",
        "risk_backend": "external:http://127.0.0.1:8001",
    }


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    print(f"Unhandled backend error on {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"detail": str(exc)})

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(frontend.router)
app.include_router(admin.router)
app.include_router(chat.router)
app.include_router(match.router)
app.include_router(system.router)

try:
    from ai_gen.app import router as ai_gen_router, on_exit as ai_gen_on_exit

    app.include_router(ai_gen_router, prefix="/ai-gen", tags=["AI Gen"])
    atexit.register(ai_gen_on_exit)
except Exception as e:
    print(f"AI Gen router not mounted: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


