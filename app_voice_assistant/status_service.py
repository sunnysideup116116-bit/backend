"""Authenticated, unmetered state reads. No model, sending or risk evaluation."""
import asyncio
from collections import OrderedDict, deque
import hashlib
import time

from fastapi import HTTPException

from .status_contract import public_quota, status_result


class VoiceStatusService:
    def __init__(self, *, quota_reader=None, chat_reader=None):
        self.quota_reader = quota_reader or self._quota_read
        self.chat_reader = chat_reader or self._chat_read
        self._pending = {}
        self._requests = OrderedDict()

    def allow_read(self, owner):
        now = time.monotonic()
        rows = self._requests.setdefault(owner, deque())
        while rows and rows[0] <= now - 60:
            rows.popleft()
        self._requests.move_to_end(owner)
        while len(self._requests) > 4096:
            self._requests.popitem(last=False)
        if len(rows) >= 60:
            raise HTTPException(429, detail={'code': 'status_rate_limited', 'message': '查詢太頻繁，請稍後再試。'})
        rows.append(now)

    @staticmethod
    def _quota_read(owner):
        from agent_quota.service import service
        return service.status(owner, sync_name=False)

    async def quota(self, owner):
        existing = self._pending.get(owner)
        if existing is not None:
            return await asyncio.shield(existing)

        async def read():
            try:
                return public_quota(await asyncio.to_thread(self.quota_reader, owner))
            except Exception:
                return public_quota(None)
            finally:
                self._pending.pop(owner, None)

        task = asyncio.create_task(read())
        self._pending[owner] = task
        return await asyncio.shield(task)

    @staticmethod
    def _chat_read(owner, contact, attempt):
        from services.relationship_engagement_service import find_accepted_match
        from services.chat_service import generate_room_id
        from services.voice_chat_status import read_chat_status
        if not contact or contact == owner or not find_accepted_match(owner, contact):
            raise HTTPException(403, detail={'code': 'contact_unavailable', 'message': '目前無法查詢此聯絡人的對話狀態。'})
        return read_chat_status(owner, generate_room_id(owner, contact), attempt)

    async def chat(self, owner, contact, attempt=''):
        return await asyncio.to_thread(self.chat_reader, owner, contact, attempt)
