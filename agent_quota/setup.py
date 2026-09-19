"""Idempotent additive schema setup: python -m agent_quota.setup.

Never overwrites existing balances, defaults, infinity flags or disabled codes.
"""
import time
from .service import AppwriteStore, StoreError, key, DEFAULT_TOKENS, DAILY_REFILL_TOKENS

SCHEMA = {
    'agent_quota_settings': {'initial_tokens': ('integer', DEFAULT_TOKENS),
                             'daily_refill_tokens': ('integer', DAILY_REFILL_TOKENS)},
    'agent_quotas': {'user_id': ('string', ''), 'user_name': ('string', ''),
                     'max_tokens': ('integer', DEFAULT_TOKENS),
                     'remaining_tokens': ('integer', DEFAULT_TOKENS), 'infinity': ('boolean', False),
                     'used_tokens': ('integer', 0), 'matching_tokens': ('integer', 0),
                     'private_tokens': ('integer', 0), 'voice_tokens': ('integer', 0),
                     'revision': ('string', ''), 'last_refill_at': ('datetime', None),
                     'refill_policy_version': ('integer', 0)},
    'agent_quota_usage': {'user_id': ('string', ''), 'feature': ('string', ''),
                         'task_id': ('string', ''), 'input_tokens': ('integer', 0),
                         'output_tokens': ('integer', 0), 'deducted_tokens': ('integer', 0)},
    'agent_quota_codes': {'code': ('string', ''), 'enabled': ('boolean', True)},
    'agent_quota_redemptions': {'user_id': ('string', ''), 'code_id': ('string', '')},
}


def setup():
    store = AppwriteStore()
    root = f'/databases/{store.database}/collections'
    for collection, fields in SCHEMA.items():
        try:
            store.request('POST', root, {'collectionId': collection, 'name': collection,
                          'permissions': [], 'documentSecurity': False, 'enabled': True})
        except StoreError as exc:
            if exc.status != 409:
                raise
        for field, (kind, default) in fields.items():
            data = {'key': field, 'required': False, 'default': default, 'array': False}
            if kind == 'string':
                data['size'] = 128
            if kind == 'integer':
                data['min'] = 0
            try:
                store.request('POST', f'{root}/{collection}/attributes/{kind}', data)
            except StoreError as exc:
                if exc.status != 409:
                    raise
        for _ in range(60):
            attrs = store.request('GET', f'{root}/{collection}/attributes')['attributes']
            if len(attrs) >= len(fields) and all(a['status'] == 'available' for a in attrs):
                break
            if any(a['status'] in ('failed', 'stuck') for a in attrs):
                raise RuntimeError('Appwrite quota attribute failed: ' + collection)
            time.sleep(1)
        else:
            raise RuntimeError('Appwrite quota attributes not ready: ' + collection)
        print(collection + ': ready')
    for collection, ident, data in [
        ('agent_quota_settings', 'default', {'initial_tokens': DEFAULT_TOKENS,
                                             'daily_refill_tokens': DAILY_REFILL_TOKENS}),
        ('agent_quota_codes', key('Sunnyfan116'), {'code': 'Sunnyfan116', 'enabled': True}),
    ]:
        try:
            store.create(collection, ident, data)
        except StoreError as exc:
            if exc.status != 409:
                raise


if __name__ == '__main__':
    setup()
