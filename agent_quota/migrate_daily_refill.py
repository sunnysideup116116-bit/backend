"""One-time 150K/40K upgrade. Re-running never grants the 50K difference twice."""
from datetime import datetime, timezone
from appwrite.query import Query
from .setup import setup
from .service import QuotaService, DEFAULT_TOKENS, DAILY_REFILL_TOKENS


def migrate():
    setup()
    service = QuotaService()
    store = service.store
    for collection, field, default in [
        ('agent_quota_settings', 'initial_tokens', DEFAULT_TOKENS),
        ('agent_quotas', 'max_tokens', DEFAULT_TOKENS),
        ('agent_quotas', 'remaining_tokens', DEFAULT_TOKENS),
    ]:
        store.request('PATCH', f'/databases/{store.database}/collections/{collection}/attributes/integer/{field}',
                      {'required': False, 'default': default, 'min': 0})
    store.update('agent_quota_settings', 'default',
                 {'initial_tokens': DEFAULT_TOKENS, 'daily_refill_tokens': DAILY_REFILL_TOKENS})
    current = datetime.now(timezone.utc)
    cursor = None
    upgraded = 0
    while True:
        queries = [Query.limit(100), Query.order_asc('$id')]
        if cursor:
            queries.append(Query.cursor_after(cursor))
        rows = store.request('GET', store.path('agent_quotas'), params={'queries[]': queries})['documents']
        for row in rows:
            # Settle pending usage before crediting the new policy.
            service.replay(owner=row['$id'], strict=True)
            service.refill(row['$id'], now=current)
            upgraded += 1
        if len(rows) < 100:
            break
        cursor = rows[-1]['$id']
    print(f'Daily quota policy ready: 150K default, 40K/day, Asia/Taipei 00:00; {upgraded} accounts checked')


if __name__ == '__main__':
    migrate()
