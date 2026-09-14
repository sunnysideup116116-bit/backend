#!/usr/bin/env bash
set -euo pipefail

server_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
canonical_root="$server_root"
supplied_social_env="${AYUE_SOCIAL_ENV_SOURCE:-}"
supplied_matchmaker_env="${AYUE_MATCHMAKER_ENV_SOURCE:-}"
existing_server_env="${AYUE_SERVER_ENV_SOURCE:-$server_root/.env}"
social_example="$canonical_root/social/.env.example"
matchmaker_example="$canonical_root/matchmaker_agent/.env.example"
social_target="$canonical_root/social/.env"
matchmaker_target="$canonical_root/matchmaker_agent/.env"

for required_file in "$social_example" "$matchmaker_example"; do
    if [[ ! -f "$required_file" ]]; then
        printf 'Missing environment source: %s\n' "$required_file" >&2
        exit 1
    fi
done

# An explicitly supplied source must exist. The shared legacy file is optional
# when provisioning directly from separate Social and Matchmaker attachments.
for supplied_file in "$supplied_social_env" "$supplied_matchmaker_env" "${AYUE_SERVER_ENV_SOURCE:-}"; do
    if [[ -n "$supplied_file" && ! -f "$supplied_file" ]]; then
        printf 'Missing environment source: %s\n' "$supplied_file" >&2
        exit 1
    fi
done

shared_sources=()
social_sources=()
matchmaker_sources=()
if [[ -f "$existing_server_env" ]]; then
    shared_sources+=("$existing_server_env")
fi
if [[ -n "$supplied_social_env" ]]; then
    social_sources+=("$supplied_social_env")
fi
if [[ -n "$supplied_matchmaker_env" ]]; then
    matchmaker_sources+=("$supplied_matchmaker_env")
fi

write_selected_env() {
    local target="$1"
    local example="$2"
    shift 2
    local inputs=("$example" "$@")
    # Re-running provisioning fills missing settings without replacing current
    # credentials, model choices, rollout flags, or additional local settings.
    if [[ -f "$target" ]]; then
        inputs+=("$target")
    fi
    local temporary
    temporary="$(mktemp "$canonical_root/.env-build.XXXXXX")"
    if ! awk -v example="$example" -v target="$target" '
        {
            line = $0
            sub(/\r$/, "", line)
            sub(/^[[:space:]]*export[[:space:]]+/, "", line)
            if (line !~ /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=/) {
                next
            }
            key = line
            sub(/=.*/, "", key)
            gsub(/[[:space:]]/, "", key)
            sub(/^[^=]*=/, "", line)
            # Examples are the canonical allowlist. Keep extra keys only when
            # already present in this target, never by importing arbitrary keys.
            if ((FILENAME == example || FILENAME == target) && !(key in seen)) {
                ordered[++count] = key
                seen[key] = 1
            }
            selected[key] = line
        }
        END {
            for (position = 1; position <= count; position++) {
                key = ordered[position]
                printf "%s=%s\n", key, selected[key]
            }
        }
    ' "${inputs[@]}" > "$temporary"; then
        rm -f "$temporary"
        return 1
    fi
    install -m 600 "$temporary" "$target"
    rm -f "$temporary"
}

# Later sources have priority; each existing target is always read last.
# Reading the Social source for Matchmaker also recovers graph thresholds that
# older attachments placed in Social. They are not imported into Social itself.
write_selected_env "$social_target" "$social_example" \
    "${shared_sources[@]}" "${social_sources[@]}"
write_selected_env "$matchmaker_target" "$matchmaker_example" \
    "${social_sources[@]}" "${shared_sources[@]}" "${matchmaker_sources[@]}"

printf 'Provisioned ignored Ayue V3 environment files with mode 600; existing values preserved.\n'
