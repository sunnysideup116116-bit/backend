"""
統一 FastAPI 入口
所有服務透過一個 port 對外提供，使用 URL 前綴區分：
  /            → 前端頁面 (main_app)
  /api/...     → 主系統 API (main_app)
  /matchmaker/ → 媒婆 Agent API (matchmaker_agent)
  /ai-gen/     → AI 聊天建議 API (ai_gen)
"""
import sys
import atexit
from pathlib import Path
from dotenv import load_dotenv

# 1. 載入頂層統一 .env（必須在所有 import 之前）
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

# 2. 將子服務目錄加入 sys.path，讓各模組的內部 import 正常運作
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.append(str(PROJECT_ROOT / "main_app"))
sys.path.append(str(PROJECT_ROOT / "matchmaker_agent"))
sys.path.append(str(PROJECT_ROOT / "ai_gen"))

# 3. 建立 FastAPI 應用
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="Unified AI Social Platform",
    description="整合 main_app / matchmaker_agent / ai_gen 三大服務的統一 API",
    version="1.0.0"
)

# CORS 中介軟體（開放外網呼叫）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生產環境請改為指定域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health_check():
    """健康檢查端點"""
    return {
        "status": "ok",
        "services": {
            "main_app": "mounted at /api",
            "matchmaker_agent": "mounted at /matchmaker",
            "ai_gen": "mounted at /ai-gen"
        }
    }

# ============================
# 掛載 main_app 路由
# ============================
from routers import chat, match, system, frontend, guidance

app.include_router(frontend.router)       # GET /
app.include_router(chat.router)           # /api/chat, /api/messages, /api/direct_chat, ...
app.include_router(match.router)          # /api/match, /api/match/accept, /api/match/decline
app.include_router(system.router)         # /api/init, /api/seed, /api/clear, ...
app.include_router(guidance.router)       # /api/guidance/suggestion, ...

# ============================
# 掛載 matchmaker_agent 路由
# ============================
from agent_api import router as matchmaker_router

app.include_router(matchmaker_router, prefix="/matchmaker", tags=["Matchmaker Agent"])
# → /matchmaker/match, /matchmaker/feedback, /matchmaker/global_reflection

# ============================
# 掛載 ai_gen 路由
# ============================
from app import router as ai_gen_router

app.include_router(ai_gen_router, prefix="/ai-gen", tags=["AI Gen"])
# → /ai-gen/track-message, /ai-gen/semantic-plan, /ai-gen/generate-suggestion

# ============================
# 掛載 請求監控日誌 路由與 Middleware
# ============================
from routers import request_logs

app.include_router(request_logs.router)
app.add_middleware(request_logs.RequestLoggingMiddleware)

# ============================
# Shutdown 清理
# ============================
from app import on_exit as ai_gen_on_exit
atexit.register(ai_gen_on_exit)



if __name__ == "__main__":
    import uvicorn
    import os
    host = os.environ.get("SERVER_HOST", "127.0.0.1")
    port = int(os.environ.get("SERVER_PORT", "8000"))
    print(f"\n🚀 啟動統一服務：http://{host}:{port}")
    print(f"   📱 前端頁面:       http://localhost:{port}/")
    print(f"   🔌 主系統 API:     http://localhost:{port}/api/")
    print(f"   💘 媒婆 Agent:     http://localhost:{port}/matchmaker/")
    print(f"   🤖 AI Gen:         http://localhost:{port}/ai-gen/")
    print(f"   📊 監控儀表板:     http://localhost:{port}/dashboard")
    print(f"   ❤️  健康檢查:      http://localhost:{port}/health")
    print()
    reload = os.environ.get("UVICORN_RELOAD", "0") == "1"
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=reload,
        timeout_keep_alive=int(os.environ.get("UVICORN_KEEPALIVE", "75")),
    )
