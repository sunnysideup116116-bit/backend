#!/bin/bash

# --- Color Definitions for Polish ---
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0;37m' # No Color

echo -e "${CYAN}================================================================${NC}"
echo -e "${CYAN}🚀  Unified Platform with Safety Governance Microservice Startup ${NC}"
echo -e "${CYAN}================================================================${NC}"

# 1. Activate Virtual Environment
if [ -d "venv" ]; then
    echo -e "${GREEN}🔍 Found virtual environment, activating...${NC}"
    source venv/bin/activate
else
    echo -e "${YELLOW}⚠️  No virtual environment (venv) found. Running in system Python...${NC}"
fi

export PYTHONUNBUFFERED=1

# 2. Check and clean up Port 8000 (Unified Server) and Port 8001 (Risk Backend)
cleanup_port() {
    local port=$1
    local pids=$(lsof -t -i:$port 2>/dev/null)
    if [ ! -z "$pids" ]; then
        echo -e "${YELLOW}⚠️  Port $port is currently occupied by PIDs: $pids${NC}"
        echo -e "${YELLOW}👉 Attempting to terminate stale processes...${NC}"
        kill -9 $pids 2>/dev/null
        sleep 1
        # Re-check
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

# 3. Start Risk Detection Backend (Port 8001)
echo -e "\n${CYAN}🛡️  Step 2: Starting Risk Detection Backend on Port 8001...${NC}"
export RISK_PORT=8001
export RISK_HOST=127.0.0.1

# Export fallback values for Appwrite connection to prevent startup crash
# export APPWRITE_ENDPOINT="http://localhost/v1"
# export APPWRITE_PROJECT_ID="mock_project"
# export APPWRITE_API_KEY="mock_key"
# export APPWRITE_DB_ID="mock_db"


python -u risk_backend/main.py > risk_backend.log 2>&1 &
RISK_PID=$!

echo -e "${GREEN}✅ Risk Detection Backend started in the background (PID: $RISK_PID). Logs are saved to risk_backend.log.${NC}"

# Wait a brief moment to ensure the backend starts
echo -e "${CYAN}⏳ Waiting for Risk Backend to initialize...${NC}"
sleep 2

# 4. Start Main Unified Server (Port 8000)
echo -e "${CYAN}🚀 Step 3: Starting Main Unified Server on Port 8000...${NC}"
echo -e "${GREEN}----------------------------------------------------------------${NC}"

# Define a cleanup trap to kill the background risk backend when the script is stopped
cleanup_all() {
    echo -e "\n\n${YELLOW}🛑 Shutting down all services...${NC}"
    if [ ! -z "$RISK_PID" ]; then
        echo -e "${YELLOW}Stopping Risk Backend (PID: $RISK_PID)...${NC}"
        kill -9 $RISK_PID 2>/dev/null
    fi
    echo -e "${GREEN}👋 Shutdown complete. Have a great day!${NC}"
    exit 0
}

trap cleanup_all SIGINT SIGTERM

python -u main.py
