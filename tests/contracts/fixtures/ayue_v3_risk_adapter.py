"""Additive pre-persistence risk contract for the canonical Ayue V3 backend.

This module deliberately does not import Ayue routers or Scheduler internals. An
HTTP composition layer can call it before the canonical message persistence
path. Wiring that insertion point requires GitNexus impact analysis first.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from typing import Any, Callable, NamedTuple


_DELIVERABLE_LEVELS = frozenset({"safe", "observation", "warning", "restricted"})
_KNOWN_LEVELS = _DELIVERABLE_LEVELS | {"blocked"}


class RiskCheckRequest(NamedTuple):
    conversation_id: str
    sender_id: str
    receiver_id: str
    content: str
    idempotency_key: str
    message_timestamp: str | None = None


class RiskDecision(NamedTuple):
    level: str
    persist_policy: str

    def public_projection(self) -> dict[str, str]:
        if self.persist_policy == "block":
            return {"level": self.level, "ui_priority": "risk", "delivery": "blocked"}
        priority = "risk" if self.level in {"warning", "restricted"} else "coach"
        return {"level": self.level, "ui_priority": priority, "delivery": "delivered"}


class MessagePersistencePermit:
    """A single-use permit prevents accidental double persistence per decision."""

    def __init__(self, decision: RiskDecision) -> None:
        self._decision = decision
        self._consumed = False

    def consume(self) -> bool:
        if self._consumed or self._decision.persist_policy != "allow":
            return False
        self._consumed = True
        return True


def receiver_history_filter(room_id: str) -> dict[str, Any]:
    return {"room_id": room_id, "is_blocked": {"$ne": True}}


class RiskAssessmentGate:
    def __init__(
        self,
        *,
        transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        service_url: str | None = None,
        timeout_seconds: float | None = None,
        cache_size: int = 256,
    ) -> None:
        self._service_url = (service_url or os.getenv("RISK_SERVICE_URL") or "http://127.0.0.1:8001").rstrip("/")
        self._timeout = float(timeout_seconds or os.getenv("RISK_TIMEOUT_SEC") or "20")
        self._transport = transport or self._http_transport
        self._cache_size = max(1, min(cache_size, 4096))
        self._cache: OrderedDict[str, RiskDecision] = OrderedDict()

    def evaluate(self, request: RiskCheckRequest) -> RiskDecision:
        if not request.idempotency_key:
            return RiskDecision("unavailable", "allow")
        cached = self._cache.get(request.idempotency_key)
        if cached is not None:
            self._cache.move_to_end(request.idempotency_key)
            return cached
        payload: dict[str, Any] = {
            "conversation_id": request.conversation_id,
            "sender_id": request.sender_id,
            "receiver_id": request.receiver_id,
            "current_message": request.content,
        }
        if request.message_timestamp:
            payload["message_timestamp"] = request.message_timestamp
        try:
            raw = self._transport(payload)
            level = str(raw.get("risk_level") or "").strip().lower()
            if level not in _KNOWN_LEVELS:
                decision = RiskDecision("unavailable", "allow")
            elif level == "blocked":
                decision = RiskDecision(level, "block")
            else:
                decision = RiskDecision(level, "allow")
        except Exception:
            decision = RiskDecision("unavailable", "allow")
        self._cache[request.idempotency_key] = decision
        self._cache.move_to_end(request.idempotency_key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return decision

    def _http_transport(self, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx

        response = httpx.post(
            f"{self._service_url}/api/v1/risk/detect",
            json=payload,
            timeout=self._timeout,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("risk response must be an object")
        return result
