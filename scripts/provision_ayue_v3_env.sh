#!/usr/bin/env bash
set -euo pipefail

server_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
canonical_root="$server_root"
supplied_social_env="${AYUE_SOCIAL_ENV_SOURCE:-/home/sunny/下載/ayue_for_demo-main/social.env}"
existing_server_env="${AYUE_SERVER_ENV_SOURCE:-$server_root/.env}"
social_example="$canonical_root/social/.env.example"
matchmaker_example="$canonical_root/matchmaker_agent/.env.example"
social_target="$canonical_root/social/.env"
matchmaker_target="$canonical_root/matchmaker_agent/.env"

for required_file in "$supplied_social_env" "$existing_server_env" "$social_example" "$matchmaker_example"; do
    if [[ ! -f "$required_file" ]]; then
        printf 'Missing environment source: %s\n' "$required_file" >&2
        exit 1
    fi
done

social_keys='MONGO_URI MONGO_DB_NAME DEMO_DESTRUCTIVE_TOOLS_ENABLED RISK_SERVICE_URL RISK_TIMEOUT_SEC OLLAMA_HOST OLLAMA_API_KEY OLLAMA_CHAT_MODEL OLLAMA_FAST_CHAT_MODEL AYUE_OLLAMA_TIMEOUT_SECONDS AYUE_LOCAL_DEBUG_TRACE AYUE_RUNTIME_MODEL_SETTINGS_TOKEN AYUE_ALLOWED_RUNTIME_MODELS GOOGLE_AI_STUDIO_API_KEY GOOGLE_EMBEDDING_MODEL AYUE_DEFAULT_TIMEZONE AYUE_CALENDAR_STATE_MONGO AYUE_PROFILE_SKILLS_MODE AYUE_PROFILE_SKILLS_USER_ALLOWLIST TAVILY_API_KEY TAVILY_PROJECT GIPHY_API_KEY GIPHY_GIF_ENABLED AYUE_MAPS_ENABLED AYUE_MAPS_MONGO_CACHE OSM_NOMINATIM_URL OSM_OVERPASS_URL OSM_OVERPASS_FALLBACK_URL OSM_USER_AGENT AYUE_GOOGLE_PLACE_CARDS_ENABLED AYUE_PUBLIC_PLACE_CARDS_ENABLED GOOGLE_PLACES_SERVER_API_KEY GOOGLE_MAPS_BROWSER_API_KEY AYUE_GOOGLE_PLACE_PHOTOS_ENABLED AYUE_GOOGLE_DISTANCE_MATRIX_ENABLED MATCH_AGENT_CANDIDATE_LIMIT MATCH_VECTOR_QUALIFICATION_MIN AYUE_V3_SIMPLE_CHAT_FAST_PATH AYUE_V3_WEB_PLACE_BOOTSTRAP_FAST_PATH AYUE_SUBAGENT_MAX_READS AYUE_SUBAGENT_MAX_PARALLEL'
matchmaker_keys='LLM_API_KEY LLM_BASE_URL LLM_MODEL_ID NEO4J_URI NEO4J_USERNAME NEO4J_PASSWORD NEO4J_DATABASE MATCH_GLOBAL_RULE_LIMIT MATCH_GLOBAL_RULE_CHAR_LIMIT MATCH_GLOBAL_RULE_SIMILARITY_THRESHOLD'

write_selected_env() {
    local target="$1"
    local keys="$2"
    shift 2
    local temporary
    temporary="$(mktemp "$canonical_root/.env-build.XXXXXX")"
    if ! awk -v keys="$keys" '
        BEGIN {
            count = split(keys, ordered, " ")
        }
        /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=/ {
            line = $0
            key = line
            sub(/=.*/, "", key)
            gsub(/[[:space:]]/, "", key)
            sub(/^[^=]*=/, "", line)
            selected[key] = line
        }
        END {
            for (position = 1; position <= count; position++) {
                key = ordered[position]
                printf "%s=%s\n", key, selected[key]
            }
        }
    ' "$@" > "$temporary"; then
        rm -f "$temporary"
        return 1
    fi
    install -m 600 "$temporary" "$target"
    rm -f "$temporary"
}

# Later files have priority. Examples contribute only public defaults.
write_selected_env "$social_target" "$social_keys" \
    "$social_example" "$existing_server_env" "$supplied_social_env"
write_selected_env "$matchmaker_target" "$matchmaker_keys" \
    "$matchmaker_example" "$supplied_social_env" "$existing_server_env"

printf 'Provisioned ignored Ayue V3 environment files with mode 600.\n'
