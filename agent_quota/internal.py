"""Signed accounting context for the local Social -> Matchmaker boundary.

Clients cannot select an owner, feature or background exemption. Unsigned local
background requests remain unmetered; only Social can sign billable contexts.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from .service import SCOPE, task_scope, start_worker, stop_worker


def secret():
    from dotenv import load_dotenv
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / 'social' / '.env', override=False)
    return os.getenv('APPWRITE_API_KEY', '').encode()


def signed_headers():
    scope = SCOPE.get()
    if not scope:
        return {}
    value = base64.urlsafe_b64encode(json.dumps([*scope, int(time.time())]).encode()).decode()
    signing_key = secret()
    if not signing_key:
        raise RuntimeError('quota signing key unavailable')
    signature = hmac.new(signing_key, value.encode(), hashlib.sha256).hexdigest()
    return {'X-Agent-Quota': value + '.' + signature}


class MatchmakerQuotaMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path') not in {
            '/api/match', '/api/preferences/related-interest-candidates',
        }:
            return await self.app(scope, receive, send)
        value = dict(scope.get('headers', [])).get(b'x-agent-quota', b'').decode()
        if not value:
            return await self.app(scope, receive, send)
        try:
            payload, signature = value.rsplit('.', 1)
            signing_key = secret()
            expected = hmac.new(signing_key, payload.encode(), hashlib.sha256).hexdigest()
            if not signing_key or not hmac.compare_digest(expected, signature):
                raise ValueError()
            owner, feature, task, timestamp = json.loads(base64.urlsafe_b64decode(payload))
            if feature != 'matching' or abs(time.time() - timestamp) > 180:
                raise ValueError()
        except Exception:
            from starlette.responses import JSONResponse
            return await JSONResponse({'detail': 'invalid_quota_context'}, 403)(scope, receive, send)
        with task_scope(owner, feature, task):
            await self.app(scope, receive, send)
