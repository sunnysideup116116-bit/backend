#!/bin/bash

# --- Color Definitions for Polish ---
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0;37m' # No Color

echo -e "${CYAN}================================================================${NC}"
echo -e "${CYAN}🚀  Dating App Project Backend Startup (Scheme-A)${NC}"
echo -e "${CYAN}================================================================${NC}"

# 1. Activate Virtual Environment
if [ -d "venv" ]; then
    echo -e "${GREEN}🔍 Found virtual environment, activating...${NC}"
    source venv/bin/activate
else
    echo -e "${YELLOW}⚠️  No virtual environment (venv) found. Running in system Python...${NC}"
fi

export PYTHONUNBUFFERED=1

# Determine Python command to use
if command -v python &>/dev/null; then
    PYTHON_CMD="python"
elif command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
else
    echo -e "${RED}❌ Error: Python is not installed or not in PATH!${NC}"
    exit 1
fi

# 2. Check and clean up Ports: 8000 (Main), 8001 (Risk), 9001 (Matchmaker)
cleanup_port() {
    local port=$1
    local pids=$(lsof -t -i:$port 2>/dev/null)
    if [ ! -z "$pids" ]; then
        echo -e "${YELLOW}⚠️  Port $port is currently occupied by PIDs: $pids${NC}"
        echo -e "${YELLOW}👉 Attempting to terminate stale processes...${NC}"
        kill -9 $pids 2>/dev/null
        sleep 1
        pids=$(lsof -t -i:$port 2>/dev/null)
        if [ ! -z "$pids" ]; then
            echo -e "${RED}❌ Failed to free Port $port. Please terminate it manually!${NC}"
            exit 1
        else
            echo -e "${GREEN}✅ Port $port successfully freed!${NC}"
        fi
    fi
}

echo -e "\n${CYAN}🛡️  Step 1: Checking and cleaning up network ports...${NC}"
cleanup_port 8000
cleanup_port 8001
cleanup_port 9001

# 3. Start Risk Detection Backend (Port 8001)
echo -e "\n${CYAN}🛡️  Step 2: Starting Risk Detection Backend on Port 8001...${NC}"
export RISK_PORT=8001
export RISK_HOST=127.0.0.1

$PYTHON_CMD -u risk_backend/main.py > risk_backend.log 2>&1 &
RISK_PID=$!

echo -e "${GREEN}✅ Risk Detection Backend started in the background (PID: $RISK_PID). Logs: risk_backend.log${NC}"
echo -e "${CYAN}⏳ Waiting for Risk Backend to initialize...${NC}"
sleep 2

# 4. Start Matchmaker Agent (Port 9001)
echo -e "\n${CYAN}💘 Step 3: Starting Matchmaker Agent on Port 9001...${NC}"

$PYTHON_CMD -u matchmaker_agent/agent_api.py > matchmaker_agent.log 2>&1 &
MATCHMAKER_PID=$!

echo -e "${GREEN}✅ Matchmaker Agent started in the background (PID: $MATCHMAKER_PID). Logs: matchmaker_agent.log${NC}"
echo -e "${CYAN}⏳ Waiting for Matchmaker Agent to initialize...${NC}"
sleep 2

# 5. Start Main Unified Server (Port 8000) — foreground
echo -e "\n${CYAN}🚀 Step 4: Starting Main Unified Server on Port 8000...${NC}"
echo -e "${GREEN}----------------------------------------------------------------${NC}"
echo -e "${GREEN}   📱 前端頁面:    http://localhost:8000/${NC}"
echo -e "${GREEN}   🔌 主系統 API:  http://localhost:8000/api/${NC}"
echo -e "${GREEN}   🤖 AI Gen:       http://localhost:8000/ai-gen/${NC}"
echo -e "${GREEN}   💘 媒婆 Agent:   http://localhost:9001/${NC}"
echo -e "${GREEN}   🛡️  風險偵測:    http://localhost:8001/${NC}"
echo -e "${GREEN}   ❤️  健康檢查:    http://localhost:8000/health${NC}"
echo -e "${GREEN}----------------------------------------------------------------${NC}"

# Cleanup trap: kill background services when the main script is stopped
cleanup_all() {
    echo -e "\n\n${YELLOW}🛑 Shutting down all services...${NC}"
    if [ ! -z "$RISK_PID" ]; then
        echo -e "${YELLOW}Stopping Risk Backend (PID: $RISK_PID)...${NC}"
        kill -9 $RISK_PID 2>/dev/null
    fi
    if [ ! -z "$MATCHMAKER_PID" ]; then
        echo -e "${YELLOW}Stopping Matchmaker Agent (PID: $MATCHMAKER_PID)...${NC}"
        kill -9 $MATCHMAKER_PID 2>/dev/null
    fi
    echo -e "${GREEN}👋 Shutdown complete. Have a great day!${NC}"
    exit 0
}

trap cleanup_all SIGINT SIGTERM

$PYTHON_CMD -u main.py