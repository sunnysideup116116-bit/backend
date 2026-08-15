#!/usr/bin/env bash
set -euo pipefail

server_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
service_root="$server_root/risk_backend"
python_bin="$server_root/venv/bin/python"
port="${RISK_PORT:-8001}"

if [[ ! -x "$python_bin" ]]; then
    printf 'Risk Python environment is unavailable.\n' >&2
    exit 1
fi
if [[ ! -f "$server_root/.env" ]]; then
    printf 'Server environment file is unavailable.\n' >&2
    exit 1
fi
if command -v ss >/dev/null 2>&1 && ss -ltn "sport = :$port" | tail -n +2 | grep -q .; then
    printf 'Port %s is already occupied; no process was stopped.\n' "$port" >&2
    exit 2
fi

cd "$service_root"
exec "$python_bin" -m dotenv -f "$server_root/.env" run -- \
    "$python_bin" main.py
