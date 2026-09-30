"""Process-local admission for public endpoints that can incur provider costs.

These guards limit work before the route calls a provider. They do not prove an
owner or replace account quotas. Client IP comes from the ASGI server's trusted
proxy handling, never directly from a request header or claimed user_id.
"""

from collections import deque
from contextlib import asynccontextmanager
import threading
import time

from fastapi import HTTPException, Request


class RequestBudget:
    def __init__(self, *, per_ip: int, total: int, concurrent: int,
                 window_seconds: float = 60.0, clock=time.monotonic):
        self.per_ip = per_ip
        self.total = total
        self.concurrent = concurrent
        self.window_seconds = window_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._requests = deque()
        self._ips = {}
        self._active = 0

    @asynccontextmanager
    async def admit(self, client_ip: str):
        with self._lock:
            now = self.clock()
            boundary = now - self.window_seconds
            while self._requests and self._requests[0] <= boundary:
                self._requests.popleft()
            for key in list(self._ips):
                timestamps = self._ips[key]
                while timestamps and timestamps[0] <= boundary:
                    timestamps.popleft()
                if not timestamps:
                    del self._ips[key]
            timestamps = self._ips.get(client_ip)
            if (self._active >= self.concurrent or len(self._requests) >= self.total
                    or (timestamps is not None and len(timestamps) >= self.per_ip)):
                raise HTTPException(429, detail={"code": "provider_request_limit"},
                                    headers={"Retry-After": str(int(self.window_seconds))})
            self._requests.append(now)
            self._ips.setdefault(client_ip, deque()).append(now)
            self._active += 1
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1


PLACES_BUDGET = RequestBudget(per_ip=60, total=600, concurrent=8)
ASSESSMENT_BUDGET = RequestBudget(per_ip=10, total=100, concurrent=4)
MAX_PROVIDER_BODY_BYTES = 64 * 1024
MAX_ASSESSMENT_MESSAGE_CHARS = 8000


def _client_ip(request: Request) -> str:
    if request.client is None:
        raise HTTPException(503, detail={"code": "client_address_unavailable"})
    return request.client.host


async def require_places_budget(request: Request):
    if len(await request.body()) > MAX_PROVIDER_BODY_BYTES:
        raise HTTPException(413, detail={"code": "provider_request_too_large"})
    async with PLACES_BUDGET.admit(_client_ip(request)):
        yield


async def require_assessment_budget(request: Request):
    if len(await request.body()) > MAX_PROVIDER_BODY_BYTES:
        raise HTTPException(413, detail={"code": "provider_request_too_large"})
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(422, detail={"code": "invalid_request_json"}) from None
    if isinstance(payload, dict) and any(
        isinstance(payload.get(field), str)
        and len(payload[field]) > MAX_ASSESSMENT_MESSAGE_CHARS
        for field in ("message", "initial_interest")
    ):
        raise HTTPException(422, detail={"code": "assessment_input_too_long"})
    async with ASSESSMENT_BUDGET.admit(_client_ip(request)):
        yield
