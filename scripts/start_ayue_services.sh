#!/usr/bin/env bash
set -euo pipefail

server_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
log_root="${AYUE_LOG_DIR:-$server_root/.runtime-logs}"
mkdir -p "$log_root"

declare -a service_pids=()

start_service() {
    local name="$1"
    local launcher="$2"
    "$launcher" >"$log_root/$name.log" 2>&1 &
    service_pids+=("$!")
    printf '%s started (pid=%s, log=%s)\n' "$name" "$!" "$log_root/$name.log"
}

stop_services() {
    local pid
    for pid in "${service_pids[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    done
}

trap stop_services EXIT INT TERM

start_service "guardrail" "$server_root/scripts/run_ayue_guardrail.sh"
start_service "risk" "$server_root/scripts/run_ayue_risk.sh"
start_service "matchmaker" "$server_root/scripts/run_ayue_matchmaker.sh"
start_service "social" "$server_root/scripts/run_ayue_social.sh"

sleep 3
"$server_root/scripts/check_ayue_services.sh"
wait
