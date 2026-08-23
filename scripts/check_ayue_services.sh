#!/usr/bin/env bash
set -euo pipefail

social_url="${AYUE_SOCIAL_HEALTH_URL:-http://127.0.0.1:8000/api/health}"
matchmaker_url="${AYUE_MATCHMAKER_HEALTH_URL:-http://127.0.0.1:9001/health}"
risk_url="${AYUE_RISK_HEALTH_URL:-http://127.0.0.1:8001/health}"
guardrail_url="${AYUE_GUARDRAIL_HEALTH_URL:-http://127.0.0.1:8081/v1/models}"

check_health() {
    local service="$1"
    local url="$2"
    if curl --fail --silent --show-error --max-time 3 "$url" >/dev/null; then
        printf '%s: ok\n' "$service"
    else
        printf '%s: unavailable\n' "$service" >&2
        return 1
    fi
}

status=0
check_health "ayue-social" "$social_url" || status=1
check_health "ayue-matchmaker" "$matchmaker_url" || status=1
check_health "risk-backend" "$risk_url" || status=1
check_health "guardrail-classifier" "$guardrail_url" || status=1
exit "$status"
