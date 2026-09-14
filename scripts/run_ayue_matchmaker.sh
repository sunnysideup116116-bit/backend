#!/usr/bin/env bash
set -euo pipefail

server_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
service_root="$server_root/matchmaker_agent"
python_bin="$server_root/.local-venv/matchmaker/bin/python"
port="${AYUE_MATCHMAKER_PORT:-9001}"

if [[ ! -x "$python_bin" ]]; then
    printf 'Ayue matchmaker Python environment is unavailable.\n' >&2
    exit 1
fi
if [[ ! -f "$service_root/.env" ]]; then
    printf 'Ayue matchmaker environment file is unavailable.\n' >&2
    exit 1
fi
if command -v ss >/dev/null 2>&1 && ss -ltn "sport = :$port" | tail -n +2 | grep -q .; then
    printf 'Port %s is already occupied; no process was stopped.\n' "$port" >&2
    exit 2
fi

cd "$service_root"
exec "$python_bin" -m uvicorn agent_api:app --host 127.0.0.1 --port "$port" --reload
