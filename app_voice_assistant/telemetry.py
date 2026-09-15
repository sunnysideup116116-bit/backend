"""Privacy-minimal operational metrics for App Voice routing and tasks."""

from __future__ import annotations

import json
import logging
from typing import Any


_LOGGER = logging.getLogger("app_voice.telemetry")
_ALLOWED_FIELDS = frozenset({
    "routing_mode", "capability_id", "search_ranking", "latency_ms",
    "result_code", "task_stage",
})


def record_voice_metric(event: str, **fields: Any) -> None:
    payload = {"event": str(event or "unknown")[:40]}
    for key, value in fields.items():
        if key not in _ALLOWED_FIELDS or value is None:
            continue
        if key == "latency_ms":
            try:
                payload[key] = max(0, min(int(value), 600_000))
            except (TypeError, ValueError):
                continue
        else:
            payload[key] = str(value)[:240]
    _LOGGER.info("APP_VOICE_METRIC %s", json.dumps(payload, separators=(",", ":")))
