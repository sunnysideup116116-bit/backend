#!/usr/bin/env bash
set -euo pipefail

server_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
python_bin="$server_root/venv/bin/python"
env_file="$server_root/.env"

if [[ ! -x "$python_bin" || ! -f "$env_file" ]]; then
    printf 'Guardrail environment is unavailable.\n' >&2
    exit 1
fi

dotenv_get() {
    "$python_bin" -m dotenv -f "$env_file" get "$1" 2>/dev/null || true
}

guardrail_bin="${GUARDRAIL_SERVER_BIN:-$(dotenv_get GUARDRAIL_SERVER_BIN)}"
model_path="${GUARDRAIL_MODEL_PATH:-$(dotenv_get GUARDRAIL_MODEL_PATH)}"
model_alias="${GUARDRAIL_MODEL_ALIAS:-$(dotenv_get GUARDRAIL_MODEL_ALIAS)}"
port="${GUARDRAIL_PORT:-$(dotenv_get GUARDRAIL_PORT)}"
ctx_size="${GUARDRAIL_CTX_SIZE:-$(dotenv_get GUARDRAIL_CTX_SIZE)}"

guardrail_bin="${guardrail_bin:-/home/sunny/Applications/llama-b10333/llama-server}"
model_alias="${model_alias:-llama-guard-3-1b}"
port="${port:-8081}"
ctx_size="${ctx_size:-2048}"

if [[ ! -x "$guardrail_bin" ]]; then
    printf 'llama-server is unavailable: %s\n' "$guardrail_bin" >&2
    exit 1
fi
if [[ -z "$model_path" || ! -f "$model_path" ]]; then
    printf 'Guardrail model is unavailable: %s\n' "$model_path" >&2
    exit 1
fi
if command -v ss >/dev/null 2>&1 && ss -ltn "sport = :$port" | tail -n +2 | grep -q .; then
    printf 'Port %s is already occupied; no process was stopped.\n' "$port" >&2
    exit 2
fi

exec "$guardrail_bin" \
    --model "$model_path" \
    --alias "$model_alias" \
    --ctx-size "$ctx_size" \
    --host 127.0.0.1 \
    --port "$port"
