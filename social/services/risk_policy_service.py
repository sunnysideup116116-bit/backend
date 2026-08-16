"""Mandatory pair-chat risk policy applied before receiver-visible storage."""

from __future__ import annotations

from collections import OrderedDict
import os
from typing import Callable, NamedTuple

import requests


_DELIVERABLE_LEVELS = {"safe", "observation", "warning", "restricted"}
_KNOWN_LEVELS = _DELIVERABLE_LEVELS | {"blocked"}


class PairMessageRiskDecision(NamedTuple):
    level: str
    delivery: str

    @property
    def may_persist(self) -> bool:
        return self.delivery == "delivered"

    def public_projection(self) -> dict[str, str]:
        priority = "risk" if self.level in {"warning", "restricted", "blocked"} else "coach"
        return {
            "level": self.level,
            "ui_priority": priority,
            "delivery": self.delivery,
        }


class PairMessageRiskGate:
    def __init__(
        self,
        *,
        transport: Callable[[dict], dict] | None = None,
        service_url: str | None = None,
        timeout_seconds: float | None = None,
        cache_size: int = 256,
    ) -> None:
        self._service_url = (
            service_url
            or os.getenv("RISK_SERVICE_URL")
            or "http://127.0.0.1:8001"
        ).rstrip("/")
        self._timeout = float(timeout_seconds or os.getenv("RISK_TIMEOUT_SEC") or "20")
        self._transport = transport or self._http_transport
        self._cache_size = max(1, min(cache_size, 4096))
        self._cache: OrderedDict[str, PairMessageRiskDecision] = OrderedDict()

    def evaluate(
        self,
        *,
        conversation_id: str,
        sender_id: str,
        receiver_id: str,
        content: str,
        idempotency_key: str,
        message_timestamp: str | None = None,
    ) -> PairMessageRiskDecision:
        key = str(idempotency_key or "").strip()
        if not key:
            return PairMessageRiskDecision("unavailable", "delivered")
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        payload = {
            "conversation_id": conversation_id,
            "sender_id": sender_id,
            "receiver_id": receiver_id,
            "current_message": content,
        }
        if message_timestamp:
            payload["message_timestamp"] = message_timestamp
        try:
            raw = self._transport(payload)
            level = str(raw.get("risk_level") or "").strip().lower()
            if level == "blocked":
                decision = PairMessageRiskDecision("blocked", "blocked")
            elif level in _DELIVERABLE_LEVELS:
                decision = PairMessageRiskDecision(level, "delivered")
            else:
                decision = PairMessageRiskDecision("unavailable", "delivered")
        except Exception:
            # Risk is an advisory safety dependency, not a chat availability
            # dependency. Only an explicit `blocked` decision may prevent the
            # canonical pair message from being persisted.
            decision = PairMessageRiskDecision("unavailable", "delivered")
        self._cache[key] = decision
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return decision

    def _http_transport(self, payload: dict) -> dict:
        response = requests.post(
            f"{self._service_url}/api/v1/risk/detect",
            json=payload,
            timeout=self._timeout,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("risk response must be an object")
        return result


pair_message_risk_gate = PairMessageRiskGate()
