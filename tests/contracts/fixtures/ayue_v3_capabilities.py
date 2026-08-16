"""Build a browser/mobile-safe capability projection from Ayue configuration."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any


BASE_CAPABILITIES: dict[str, Any] = {
    "contract_version": 1,
    "backend": {"name": "ayue", "public_agent_version": "v3", "private_agent_version": "v2"},
    "capabilities": {
        "public_stream": {
            "available": True,
            "transport": "ndjson",
            "endpoint": "/api/direct_chat/stream",
        },
        "assessment_controls": {"available": True, "typed_cancel": True},
        "context_confirmation": {"available": True, "background_status_polling": True},
        "calendar": {"available": True, "typed_mutations": True},
        "date_coordination": {"available": True, "typed_cards": True},
        "match_decision": {
            "available": True,
            "mode": "status_cas",
            "revision": "echo_when_present",
            "conflict_policy": "refresh_without_replay",
        },
        "place_cards": {"available": False, "text_fallback_required": True},
        "private_mediator": {"available": True, "agent_version": "v2"},
        "relationship_quiz": {"available": True, "random_topic_route": False},
        "risk_projection": {
            "available": True,
            "status": "pair_chat_pre_persistence",
            "failure_policy": "deliver_degraded",
        },
    },
}


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "on", "yes"}


def build_mobile_capabilities(social_env_path: Path) -> dict[str, Any]:
    values = _parse_env(social_env_path)
    result = deepcopy(BASE_CAPABILITIES)
    place_cards_available = all((
        _enabled(values.get("AYUE_GOOGLE_PLACE_CARDS_ENABLED")),
        _enabled(values.get("AYUE_PUBLIC_PLACE_CARDS_ENABLED")),
        bool(values.get("GOOGLE_PLACES_SERVER_API_KEY", "").strip()),
        bool(values.get("GOOGLE_MAPS_BROWSER_API_KEY", "").strip()),
    ))
    result["capabilities"]["place_cards"]["available"] = place_cards_available
    return result
