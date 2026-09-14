"""Fail-closed block relationship lookup for chat and matching."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Callable

import requests


class RiskBlockServiceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class UserBlockSets:
    blocked_user_ids: frozenset[str]
    excluded_user_ids: frozenset[str]


class RiskBlockService:
    def __init__(
        self,
        *,
        transport: Callable[[str], dict] | None = None,
        service_url: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._service_url = (
            service_url
            or os.getenv("RISK_SERVICE_URL")
            or "http://127.0.0.1:8001"
        ).rstrip("/")
        self._timeout = float(timeout_seconds or os.getenv("RISK_BLOCK_TIMEOUT_SEC") or "5")
        self._transport = transport or self._http_transport

    @staticmethod
    def _ids(value) -> frozenset[str]:
        if not isinstance(value, list):
            raise RiskBlockServiceUnavailable("invalid block response")
        return frozenset(
            str(item).strip()[:128]
            for item in value[:500]
            if str(item or "").strip()
        )

    def get_sets(self, user_id: str) -> UserBlockSets:
        safe_user_id = str(user_id or "").strip()[:128]
        if not safe_user_id:
            raise RiskBlockServiceUnavailable("missing user id")
        try:
            payload = self._transport(safe_user_id)
            if not isinstance(payload, dict):
                raise RiskBlockServiceUnavailable("invalid block response")
            outgoing = self._ids(payload.get("blocked_user_ids"))
            excluded = self._ids(payload.get("excluded_user_ids"))
        except RiskBlockServiceUnavailable:
            raise
        except Exception as error:
            raise RiskBlockServiceUnavailable(type(error).__name__) from error
        return UserBlockSets(
            blocked_user_ids=outgoing,
            excluded_user_ids=excluded,
        )

    def excluded_user_ids(self, user_id: str) -> set[str]:
        return set(self.get_sets(user_id).excluded_user_ids)

    def is_pair_blocked(self, first_user_id: str, second_user_id: str) -> bool:
        return str(second_user_id) in self.get_sets(first_user_id).excluded_user_ids

    def _http_transport(self, user_id: str) -> dict:
        response = requests.get(
            f"{self._service_url}/api/v1/risk/blocks",
            params={"user_id": user_id},
            timeout=self._timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("risk block response must be an object")
        return payload


risk_block_service = RiskBlockService()
