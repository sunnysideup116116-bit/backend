"""Daily refill contract against real Appwrite; only temporary documents."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import tempfile
import uuid
from agent_quota.service import QuotaService, key


def run():
    now = datetime.now(timezone.utc)
    owner = 'refill-test-' + uuid.uuid4().hex[:16]
    q = QuotaService(outbox=tempfile.mktemp(prefix='refill-smoke-', suffix='.sqlite'))
    try:
        row = q.account(owner, now=now - timedelta(days=2))
        assert row['max_tokens'] == 150000
        q.store.update('agent_quotas', owner, {'remaining_tokens': 10000})
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda _: q.status(owner, now=now), range(8)))
        assert all(row['remaining_tokens'] == 90000 for row in rows), rows
        assert all(row['used_tokens'] == 0 for row in rows)
        assert rows[0]['refill_time'] == '00:00'
        assert q.status(owner, now=now)['remaining_tokens'] == 90000
        full = q.status(owner, now=now + timedelta(days=10))
        assert full['remaining_tokens'] == 150000
        q.record(owner, 'matching', 'smoke', 'spent', 100000, 0)
        assert q.status(owner, now=now + timedelta(days=10))['remaining_tokens'] == 50000
        assert q.status(owner, now=now + timedelta(days=11))['remaining_tokens'] == 90000
        print('PASS: real Appwrite 150K default, 2-day 80K refill, concurrent login dedup, cap, no banking')
    finally:
        for collection, ident in [('agent_quota_usage', key(owner + ':spent')), ('agent_quotas', owner)]:
            try:
                q.store.request('DELETE', q.store.path(collection, ident))
            except Exception as exc:
                if getattr(exc, 'status', None) != 404:
                    raise
        print('Temporary Appwrite refill documents removed')


if __name__ == '__main__':
    import urllib3
    urllib3.disable_warnings()
    run()
