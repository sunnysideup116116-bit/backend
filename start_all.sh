#!/usr/bin/env bash
set -uo pipefail

# --- Color Definitions for Polish ---
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0;37m' # No Color

# 0. Anchor to Script Directory
SERVER_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$SERVER_ROOT" || exit 1

LOG_DIR="${AYUE_LOG_DIR:-$SERVER_ROOT/.runtime-logs}"
mkdir -p "$LOG_DIR"

echo -e "${CYAN}================================================================${NC}"
echo -e "${CYAN}🚀  Dating App Backend Full Stack Startup (Ayue V3 Architecture)${NC}"
echo -e "${CYAN}📁  Server Root: $SERVER_ROOT${NC}"
echo -e "${CYAN}📁  Log Directory: $LOG_DIR${NC}"
echo -e "${CYAN}================================================================${NC}"

# 1. Clean up Stale Network Ports (8000, 8001, 9001)
cleanup_port() {
    local port=$1
    local pids=""
    if command -v lsof &>/dev/null; then
        pids=$(lsof -t -i:"$port" 2>/dev/null)
    elif command -v fuser &>/dev/null; then
        pids=$(fuser "$port"/tcp 2>/dev/null)
    fi

    if [ -n "$pids" ]; then
        echo -e "${YELLOW}⚠️  Port $port is occupied by PID(s): $pids${NC}"
        echo -e "${YELLOW}👉 Attempting graceful termination...${NC}"
        kill -15 $pids 2>/dev/null || true
        sleep 1
        pids=$(lsof -t -i:"$port" 2>/dev/null || true)
        if [ -n "$pids" ]; then
            echo -e "${YELLOW}👉 Force killing remaining process(es) on Port $port...${NC}"
            kill -9 $pids 2>/dev/null || true
            sleep 1
        fi
        pids=$(lsof -t -i:"$port" 2>/dev/null || true)
        if [ -n "$pids" ]; then
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

stop_services() {
    echo -e "\n${YELLOW}🛑 Stopping services...${NC}"
    for pid in "${SERVICE_PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -15 "$pid" 2>/dev/null || true
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
    SERVICE_PIDS=()
    cleanup_port 8000 >/dev/null 2>&1 || true
    cleanup_port 8001 >/dev/null 2>&1 || true
    cleanup_port 9001 >/dev/null 2>&1 || true
}

stop_all() {
    echo -e "\n\n${YELLOW}🛑 Shutting down all services...${NC}"
    stop_services
    echo -e "${GREEN}👋 Shutdown complete. Have a great day!${NC}"
    exit 0
}

trap stop_all INT TERM

# Readiness Probe Helper Function
wait_for_health() {
    local service_name="$1"
    local health_url="$2"
    local log_file="$3"
    local max_retries="${4:-10}"
    local count=0

    echo -e "${CYAN}⏳ Waiting for $service_name ($health_url)...${NC}"
    while [ $count -lt "$max_retries" ]; do
        if curl --fail --silent --show-error --max-time 2 "$health_url" >/dev/null 2>&1; then
            echo -e "${GREEN}✨ $service_name is ready!${NC}"
            return 0
        fi
        sleep 1
        count=$((count + 1))
    done

    echo -e "${RED}❌ $service_name failed to respond at $health_url within $max_retries seconds!${NC}"
    if [ -f "$log_file" ]; then
        echo -e "${YELLOW}📜 Last 15 lines of $log_file:${NC}"
        tail -n 15 "$log_file"
    fi
    return 1
}

start_services() {
    # 2. Start Guardrail Classifier (Port 8081) if not already active
    echo -e "\n${CYAN}🛡️  Checking Guardrail Classifier on Port 8081...${NC}"
    if curl --fail --silent --show-error --max-time 1 "http://127.0.0.1:8081/v1/models" >/dev/null 2>&1; then
        echo -e "${GREEN}✨ Guardrail Classifier is already running on Port 8081.${NC}"
    else
        if [ -x "$SERVER_ROOT/scripts/run_ayue_guardrail.sh" ]; then
            "$SERVER_ROOT/scripts/run_ayue_guardrail.sh" >"$LOG_DIR/guardrail.log" 2>&1 &
            GUARDRAIL_PID=$!
            SERVICE_PIDS+=("$GUARDRAIL_PID")
            echo -e "${GREEN}✅ Guardrail started (PID: $GUARDRAIL_PID). Logs: $LOG_DIR/guardrail.log${NC}"
            wait_for_health "Guardrail Classifier" "http://127.0.0.1:8081/v1/models" "$LOG_DIR/guardrail.log" 8 || true
        fi
    fi

    # 3. Start Risk Detection Backend (Port 8001)
    echo -e "\n${CYAN}🛡️  Starting Risk Detection Backend on Port 8001...${NC}"
    "$SERVER_ROOT/scripts/run_ayue_risk.sh" >"$LOG_DIR/risk.log" 2>&1 &
    RISK_PID=$!
    SERVICE_PIDS+=("$RISK_PID")
    echo -e "${GREEN}✅ Risk Backend started (PID: $RISK_PID). Logs: $LOG_DIR/risk.log${NC}"
    if ! wait_for_health "Risk Backend" "http://127.0.0.1:8001/health" "$LOG_DIR/risk.log" 10; then
        return 1
    fi

    # 4. Start Matchmaker Agent (Port 9001)
    echo -e "\n${CYAN}💘 Starting Matchmaker Agent on Port 9001...${NC}"
    "$SERVER_ROOT/scripts/run_ayue_matchmaker.sh" >"$LOG_DIR/matchmaker.log" 2>&1 &
    MATCHMAKER_PID=$!
    SERVICE_PIDS+=("$MATCHMAKER_PID")
    echo -e "${GREEN}✅ Matchmaker Agent started (PID: $MATCHMAKER_PID). Logs: $LOG_DIR/matchmaker.log${NC}"
    if ! wait_for_health "Matchmaker Agent" "http://127.0.0.1:9001/health" "$LOG_DIR/matchmaker.log" 10; then
        return 1
    fi

    # 5. Start Ayue Social / Main API (Port 8000)
    echo -e "\n${CYAN}🚀 Starting Ayue Social Backend on Port 8000...${NC}"
    "$SERVER_ROOT/scripts/run_ayue_social.sh" >"$LOG_DIR/social.log" 2>&1 &
    SOCIAL_PID=$!
    SERVICE_PIDS+=("$SOCIAL_PID")
    echo -e "${GREEN}✅ Ayue Social Backend started (PID: $SOCIAL_PID). Logs: $LOG_DIR/social.log${NC}"
    if ! wait_for_health "Ayue Social" "http://127.0.0.1:8000/api/health" "$LOG_DIR/social.log" 15; then
        return 1
    fi

    # 6. Service Health Check & Dashboard Summary
    echo -e "\n${GREEN}================================================================${NC}"
    echo -e "${GREEN}🎉 All Dating App Backend Services are RUNNING & HEALTHY!${NC}"
    echo -e "${GREEN}----------------------------------------------------------------${NC}"
    echo -e "${GREEN}   📱 前端頁面:       http://localhost:8000/${NC}"
    echo -e "${GREEN}   🔌 阿月核心 API:    http://localhost:8000/api/${NC}"
    echo -e "${GREEN}   🛡️  風險偵測後端:   http://localhost:8001/health${NC}"
    echo -e "${GREEN}   💘 媒婆 Agent:      http://localhost:9001/health${NC}"
    echo -e "${GREEN}   🛡️  Guardrail:      http://localhost:8081/v1/models${NC}"
    echo -e "${GREEN}   ❤️  主系統健康檢查: http://localhost:8000/api/health${NC}"
    echo -e "${GREEN}================================================================${NC}"
    echo -e "${CYAN}💡 [熱重載] 修改任何 Python 檔存檔後會自動 reload${NC}"
    echo -e "${CYAN}💡 [手動刷新] 輸入 'r' 鍵即可手動重新啟動所有服務${NC}"
    echo -e "${CYAN}💡 [退出] 輸入 'q' 或按 Ctrl+C 結束程式${NC}\n"
    return 0
}

if ! start_services; then
    stop_all
    exit 1
fi

# Interactive Loop for Reloading / Stopping
while true; do
    read -r -p "👉 請輸入指令 [r: 重啟所有服務 / q: 退出]: " cmd || true
    case "${cmd:-}" in
        [rR])
            echo -e "\n${YELLOW}🔄 正在重新啟動所有服務...${NC}"
            stop_services
            sleep 1
            if ! start_services; then
                echo -e "${RED}❌ 重啟服務失敗！請檢查日誌。${NC}"
            fi
            ;;
        [qQ])
            stop_all
            ;;
        *)
            ;;
    esac
done