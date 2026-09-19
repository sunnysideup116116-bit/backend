"""Explicit integration smoke; creates and removes isolated quota documents only."""
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from agent_quota.service import QuotaService, key


def run():
    owner = 'quota-test-' + uuid.uuid4().hex[:16]
    calls = [f'call-{i}' for i in range(12)]
    q = QuotaService(outbox=tempfile.mktemp(prefix='quota-smoke-', suffix='.sqlite'))
    try:
        assert q.status(owner)['remaining_tokens'] == 150000
        q.store.update('agent_quotas', owner, {'remaining_tokens': 100})
        def charge(call):
            q.record(owner, 'matching', 'smoke', call, 60, 40)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(charge, calls))
        result = q.status(owner)
        assert result['remaining_tokens'] == 0, result
        assert result['used_tokens'] == 1200, result
        q.store.update('agent_quotas', owner, {'remaining_tokens': 10000, 'max_tokens': 200000})
        for call in calls:
            charge(call)
        result = q.status(owner)
        assert result['remaining_tokens'] == 10000 and result['remaining_percent'] == 5, result
        assert q.redeem(owner, 'sunnyfan116') == 'invalid_code'
        assert q.redeem(owner, 'Sunnyfan116') == 'redeemed'
        q.record(owner, 'voice', 'smoke', 'unlimited', 500, 200)
        assert q.status(owner)['remaining_tokens'] == 10000
        q.store.update('agent_quotas', owner, {'infinity': False})
        assert q.redeem(owner, 'Sunnyfan116') == 'redeemed'
        assert q.status(owner)['infinity'] is True
        print('PASS: Appwrite concurrent transactions, deduplication, zero floor, top-up, reusable Sunnyfan116 code')
    finally:
        for collection, ids in [
            ('agent_quota_usage', [key(owner + ':' + c) for c in calls + ['unlimited']]),
            ('agent_quota_redemptions', [key(owner + ':Sunnyfan116')]),
            ('agent_quotas', [owner]),
        ]:
            for ident in ids:
                try:
                    q.store.request('DELETE', q.store.path(collection, ident))
                except Exception as exc:
                    if getattr(exc, 'status', None) != 404:
                        raise
        print('Temporary Appwrite documents removed')


if __name__ == '__main__':
    import urllib3
    urllib3.disable_warnings()
    run()
