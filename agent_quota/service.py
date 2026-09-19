"""Appwrite is authoritative; SQLite is a durable delivery outbox, not a balance.

Each provider call sends cumulative usage snapshots. Appwrite transactions apply
only the delta, so duplicate, out-of-order and replayed deliveries are harmless.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, time as daytime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import quote

import requests

LOG = logging.getLogger(__name__)
FEATURES = ('matching', 'private', 'voice')
REFILL_ZONE = ZoneInfo('Asia/Taipei')
DEFAULT_TOKENS = 150000
DAILY_REFILL_TOKENS = 40000
REFILL_POLICY_VERSION = 2
REUSABLE_INFINITY_CODE = 'Sunnyfan116'
EXHAUSTED = 'Agent 額度不足，請至設定 → Agent額度查看。'
UNAVAILABLE = '暫時無法確認額度，請稍後再試。'
SCOPE = ContextVar('agent_quota_scope', default=None)


class QuotaError(RuntimeError):
    def __init__(self, code='agent_quota_unavailable'):
        self.code = code
        self.message = EXHAUSTED if code == 'agent_quota_exhausted' else UNAVAILABLE
        super().__init__(self.message)


class StoreError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f'Appwrite quota request failed ({status})')


def key(value):
    return hashlib.sha256(value.encode()).hexdigest()[:36]


class AppwriteStore:
    def __init__(self):
        from dotenv import load_dotenv
        root = Path(__file__).resolve().parents[1]
        load_dotenv(root / '.env', override=False)
        load_dotenv(root / 'social' / '.env', override=False)
        from services.appwrite_identity_service import AppwriteIdentitySettings
        settings = AppwriteIdentitySettings.from_env()
        self.endpoint = settings.endpoint
        self.verify = settings.verify_tls
        self.headers = {'X-Appwrite-Project': settings.project_id,
                        'X-Appwrite-Key': os.getenv('APPWRITE_API_KEY', '')}
        self.database = os.getenv('AGENT_QUOTA_DATABASE_ID', 'dating_db')
        if not self.headers['X-Appwrite-Key']:
            raise QuotaError()

    def request(self, method, path, data=None, params=None):
        try:
            r = requests.request(method, self.endpoint + path, headers=self.headers,
                                 json=data, params=params, verify=self.verify,
                                 timeout=(2, 8), allow_redirects=False)
        except requests.RequestException as exc:
            raise StoreError(503) from exc
        if not r.ok:
            raise StoreError(r.status_code)
        return r.json() if r.content else {}

    def path(self, collection, document=None):
        path = f'/databases/{quote(self.database, safe="")}/collections/{collection}/documents'
        return path + '/' + quote(document, safe='') if document else path

    def get(self, collection, document, tx=None):
        try:
            return self.request('GET', self.path(collection, document),
                                params={'transactionId': tx} if tx else None)
        except StoreError as exc:
            if exc.status == 404:
                return None
            raise

    def user_name(self, owner):
        """Return the app-facing profile name, falling back to Appwrite Users."""
        profile = self.get('user_profiles', owner)
        if profile:
            name = str(profile.get('name') or '').strip()
            if name:
                return name[:128]
        try:
            account = self.request('GET', f'/users/{quote(owner, safe="")}')
        except StoreError as exc:
            if exc.status == 404:
                return ''
            raise
        return str(account.get('name') or '').strip()[:128]

    def create(self, collection, document, data, tx=None):
        payload = {'documentId': document, 'data': data, 'permissions': []}
        if tx:
            payload['transactionId'] = tx
        return self.request('POST', self.path(collection), payload)

    def update(self, collection, document, data, tx=None):
        payload = {'data': data}
        if tx:
            payload['transactionId'] = tx
        return self.request('PATCH', self.path(collection, document), payload)

    @contextmanager
    def transaction(self):
        tx = self.request('POST', '/databases/transactions', {'ttl': 60})['$id']
        try:
            yield tx
            self.request('PATCH', '/databases/transactions/' + tx, {'commit': True})
        except BaseException:
            try:
                self.request('PATCH', '/databases/transactions/' + tx, {'rollback': True})
            except Exception:
                pass
            raise


class QuotaService:
    def __init__(self, store=None, outbox=None):
        self._store = store
        self.outbox = str(outbox or os.getenv('AGENT_QUOTA_OUTBOX_PATH') or
                          Path(__file__).resolve().parents[1] / '.runtime' / 'agent_quota.sqlite3')

    @property
    def store(self):
        if self._store is None:
            self._store = AppwriteStore()
        return self._store

    def account(self, owner, *, now=None, sync_name=False):
        row = self.store.get('agent_quotas', owner)
        if row is not None:
            return row
        settings = self.store.get('agent_quota_settings', 'default')
        if settings is None:
            raise QuotaError()
        initial = max(0, int(settings['initial_tokens']))
        user_name = self.store.user_name(owner) if sync_name else ''
        data = {'user_id': owner, 'max_tokens': initial, 'remaining_tokens': initial,
                'infinity': False, 'used_tokens': 0, 'revision': '',
                'user_name': user_name,
                'last_refill_at': (now or datetime.now(timezone.utc)).isoformat(),
                'refill_policy_version': REFILL_POLICY_VERSION,
                **{f'{f}_tokens': 0 for f in FEATURES}}
        try:
            return self.store.create('agent_quotas', owner, data)
        except StoreError as exc:
            if exc.status != 409:
                raise
            return self.store.get('agent_quotas', owner)

    def sync_user_name(self, owner, *, now=None):
        """Copy the current app profile name into the quota document."""
        desired = self.store.user_name(owner)
        with self.owner_lock(owner):
            row = self.account(owner, now=now, sync_name=False)
            if str(row.get('user_name') or '') == desired:
                return desired
            for attempt in range(8):
                try:
                    with self.store.transaction() as tx:
                        current = self.store.get('agent_quotas', owner, tx)
                        if str(current.get('user_name') or '') != desired:
                            self.store.update('agent_quotas', owner, {
                                'user_name': desired,
                                'revision': uuid.uuid4().hex,
                            }, tx)
                    return desired
                except StoreError as exc:
                    if exc.status != 409 or attempt == 7:
                        raise
                    time.sleep(0.01 * (attempt + 1))

    def status(self, owner, *, now=None):
        try:
            try:
                current = now or datetime.now(timezone.utc)
                self.sync_user_name(owner, now=current)
            except Exception:
                # A profile-name read must not block an otherwise healthy quota.
                LOG.warning('Agent quota user name sync pending', exc_info=False)
            # Settle older observed usage before admitting a new task.
            self.replay(owner=owner, strict=True)
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                raise ValueError('refill time must include a timezone')
            settings = self.store.get('agent_quota_settings', 'default')
            if settings is None:
                raise QuotaError()
            daily = max(0, int(settings.get('daily_refill_tokens', DAILY_REFILL_TOKENS)))
            row = self.refill(owner, now=current, daily_tokens=daily)
            maximum = max(0, int(row['max_tokens']))
            remaining = max(0, int(row['remaining_tokens']))
            return {k: row.get(k) for k in ('user_name', 'max_tokens', 'remaining_tokens', 'infinity', 'used_tokens',
                    *(f'{f}_tokens' for f in FEATURES))} | {
                'remaining_percent': min(100, remaining * 100 / maximum) if maximum else 0,
                'last_refill_at': row['last_refill_at'],
                'daily_refill_tokens': daily,
                'refill_timezone': 'Asia/Taipei',
                'refill_time': '00:00',
                'next_refill_at': datetime.combine(
                    current.astimezone(REFILL_ZONE).date() + timedelta(days=1),
                    daytime.min, tzinfo=REFILL_ZONE,
                ).isoformat(),
            }
        except QuotaError:
            raise
        except Exception as exc:
            raise QuotaError() from exc

    def refill(self, owner, *, now, daily_tokens=DAILY_REFILL_TOKENS):
        """Lazy midnight accrual, atomic with its date marker; never bank full days.

        The timestamp is server-owned. Login, page refresh and admission checks
        share this path, so a session left open overnight also sees the refill.
        """
        if now.tzinfo is None:
            raise ValueError('refill time must include a timezone')
        with self.owner_lock(owner):
            row = self.account(owner, now=now, sync_name=False)
            if not self._refill_update(row, now, daily_tokens):
                return row
            for attempt in range(8):
                try:
                    with self.store.transaction() as tx:
                        self.store.update('agent_quotas', owner, {'revision': uuid.uuid4().hex}, tx)
                        row = self.store.get('agent_quotas', owner, tx)
                        changes = self._refill_update(row, now, daily_tokens)
                        if changes:
                            self.store.update('agent_quotas', owner, changes, tx)
                        result = {**row, **changes}
                    return result
                except StoreError as exc:
                    if exc.status != 409 or attempt == 7:
                        raise
                    time.sleep(0.01 * (attempt + 1))

    @staticmethod
    def _refill_update(row, now, daily_tokens):
        changes = {}
        maximum = max(0, int(row['max_tokens']))
        remaining = max(0, int(row['remaining_tokens']))
        if int(row.get('refill_policy_version') or 0) < REFILL_POLICY_VERSION:
            # One-time upgrade of the former free tier. Preserve spending and
            # any manually configured non-default ceiling; never reset infinity.
            if maximum == 100000:
                maximum = DEFAULT_TOKENS
                remaining = min(maximum, remaining + 50000)
                changes.update(max_tokens=maximum, remaining_tokens=remaining)
            changes.update(refill_policy_version=REFILL_POLICY_VERSION)
        previous = row.get('last_refill_at')
        if not previous:
            # There was no daily grant before this policy. Start its clock now.
            return {**changes, 'last_refill_at': now.isoformat()}
        previous_time = datetime.fromisoformat(previous.replace('Z', '+00:00'))
        if previous_time.tzinfo is None:
            raise ValueError('stored refill time must include a timezone')
        elapsed = (now.astimezone(REFILL_ZONE).date()
                   - previous_time.astimezone(REFILL_ZONE).date()).days
        if elapsed <= 0:
            return changes
        if not row['infinity']:
            changes['remaining_tokens'] = min(maximum, remaining + elapsed * daily_tokens)
        # Advance even when full or unlimited: unused refill days are not banked.
        changes['last_refill_at'] = now.isoformat()
        return changes

    def check(self, owner):
        row = self.status(owner)
        if not row['infinity'] and row['remaining_tokens'] <= 0:
            raise QuotaError('agent_quota_exhausted')
        return row

    @contextmanager
    def _db(self):
        Path(self.outbox).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.outbox, timeout=15)
        db.execute('CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL)')
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, owner, feature, task, call, inputs, outputs):
        if feature not in FEATURES:
            raise ValueError('invalid quota feature')
        event = {'user_id': owner, 'feature': feature, 'task_id': task,
                 'input_tokens': max(0, int(inputs or 0)), 'output_tokens': max(0, int(outputs or 0))}
        if not event['input_tokens'] and not event['output_tokens']:
            return
        ident = key(owner + ':' + call)
        # Commit locally before any remote work. Never silently drop an event.
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT payload FROM pending WHERE id=?', (ident,)).fetchone()
            if prior:
                old = json.loads(prior[0])
                for field in ('input_tokens', 'output_tokens'):
                    event[field] = max(event[field], old[field])
            db.execute('INSERT OR REPLACE INTO pending VALUES (?, ?, ?)',
                       (ident, owner, json.dumps(event)))
        try:
            self.replay(owner=owner)
        except Exception:
            LOG.warning('AI usage retained for retry', exc_info=False)

    @contextmanager
    def owner_lock(self, owner):
        # All local Server processes share this directory. Appwrite transactions
        # still atomically commit the ledger and balance; flock prevents mixed
        # staged reads in Appwrite 1.9 under competing local transactions.
        directory = Path(self.outbox).parent / 'agent-quota-locks'
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / key(owner)).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def settle(self, ident, event):
        with self.owner_lock(event['user_id']):
            return self._settle_locked(ident, event)

    def _settle_locked(self, ident, event):
        self.account(event['user_id'], sync_name=False)
        for attempt in range(8):
            try:
                with self.store.transaction() as tx:
                    # Stage first, then read the transaction snapshot. This closes
                    # the read-before-stage race, including Console edits.
                    self.store.update('agent_quotas', event['user_id'], {'revision': uuid.uuid4().hex}, tx)
                    account = self.store.get('agent_quotas', event['user_id'], tx)
                    prior = self.store.get('agent_quota_usage', ident, tx) or {}
                    inputs = max(event['input_tokens'], prior.get('input_tokens', 0))
                    outputs = max(event['output_tokens'], prior.get('output_tokens', 0))
                    delta = inputs + outputs - prior.get('input_tokens', 0) - prior.get('output_tokens', 0)
                    deducted = 0 if account['infinity'] else min(account['remaining_tokens'], delta)
                    self.store.update('agent_quotas', event['user_id'], {
                        'remaining_tokens': account['remaining_tokens'] - deducted,
                        'used_tokens': account['used_tokens'] + delta,
                        event['feature'] + '_tokens': account[event['feature'] + '_tokens'] + delta,
                    }, tx)
                    value = {**event, 'input_tokens': inputs, 'output_tokens': outputs,
                             'deducted_tokens': prior.get('deducted_tokens', 0) + deducted}
                    if prior:
                        self.store.update('agent_quota_usage', ident, value, tx)
                    else:
                        self.store.create('agent_quota_usage', ident, value, tx)
                return
            except StoreError as exc:
                if exc.status != 409 or attempt == 7:
                    raise
                time.sleep(0.01 * (attempt + 1))

    def replay(self, owner=None, strict=False):
        with self._db() as db:
            rows = db.execute('SELECT id,payload FROM pending' + (' WHERE owner=?' if owner else ''),
                              (owner,) if owner else ()).fetchall()
        for ident, payload in rows:
            try:
                self.settle(ident, json.loads(payload))
                with self._db() as db:
                    db.execute('DELETE FROM pending WHERE id=? AND payload=?', (ident, payload))
            except Exception:
                if strict:
                    raise
                LOG.warning('AI quota settlement pending: %s', ident)

    def redeem(self, owner, code):
        with self.owner_lock(owner):
            return self._redeem_locked(owner, code)

    def _redeem_locked(self, owner, code):
        try:
            self.account(owner, sync_name=False)
            ident = key(owner + ':' + code)
            for attempt in range(8):
                try:
                    with self.store.transaction() as tx:
                        self.store.update('agent_quotas', owner, {'revision': uuid.uuid4().hex}, tx)
                        redeemed = self.store.get('agent_quota_redemptions', ident, tx)
                        if redeemed and code != REUSABLE_INFINITY_CODE:
                            return 'already_redeemed'
                        offer = self.store.get('agent_quota_codes', key(code), tx)
                        if not offer or not offer['enabled'] or offer['code'] != code:
                            return 'invalid_code'
                        self.store.update('agent_quotas', owner, {'infinity': True}, tx)
                        if not redeemed:
                            self.store.create('agent_quota_redemptions', ident,
                                              {'user_id': owner, 'code_id': key(code)}, tx)
                    return 'redeemed'
                except StoreError as exc:
                    if exc.status != 409 or attempt == 7:
                        raise
            raise QuotaError()
        except QuotaError:
            raise
        except Exception as exc:
            raise QuotaError() from exc


service = QuotaService()


@contextmanager
def task_scope(owner, feature, task=None):
    token = SCOPE.set((owner, feature, task or uuid.uuid4().hex))
    try:
        yield
    finally:
        SCOPE.reset(token)


def record_usage(call, inputs, outputs):
    scope = SCOPE.get()
    if scope:
        service.record(*scope, call, inputs, outputs)


def record_gemini(response, call=None):
    usage = getattr(response, 'usage_metadata', None)
    if usage:
        record_usage(call or uuid.uuid4().hex,
                     getattr(usage, 'prompt_token_count', 0),
                     (getattr(usage, 'response_token_count', None) or
                      getattr(usage, 'candidates_token_count', 0) or 0)
                     + (getattr(usage, 'thoughts_token_count', 0) or 0))


_stop = threading.Event()
_worker = None


def start_worker():
    global _worker
    if _worker and _worker.is_alive():
        return
    _stop.clear()
    def work():
        while not _stop.wait(5):
            try:
                service.replay()
            except Exception:
                LOG.warning('AI quota retry worker unavailable')
    _worker = threading.Thread(target=work, name='agent-quota-settlement', daemon=True)
    _worker.start()


def stop_worker():
    _stop.set()
    if _worker:
        _worker.join(timeout=10)


def tracked_ollama(response):
    """Capture provider usage before parsing/callback failures can discard it."""
    call = uuid.uuid4().hex
    def observe(value):
        record_usage(call, value.get('prompt_eval_count', 0), value.get('eval_count', 0))
    if hasattr(response, 'get'):
        observe(response)
        return response
    def stream():
        try:
            for chunk in response:
                observe(chunk)
                yield chunk
        finally:
            close = getattr(response, 'close', None)
            if close:
                close()
    return stream()


@contextmanager
def unmetered():
    token = SCOPE.set(None)
    try:
        yield
    finally:
        SCOPE.reset(token)
