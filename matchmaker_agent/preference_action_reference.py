"""Opaque, stateless references to ONE observed active owner association.

Only the MAC leaves the server: no encoded canonical key or Graph element ID.
Validation must run after the owner write fence and inside the mutation tx.
"""
import base64
import hmac
import re
import time

from .preference_bootstrap_contract import _mac, encoded
from .preference_write_fence import PreferenceFenceError, lock_preferences

PREFIX = 'par1.'
MAX_PENDING = 100


def active(row):
    return (row.get('relation') in {'PREFERS', 'AVOIDS'}
            and (row.get('properties', {}).get('active') is None or row['properties']['active'] is True)
            and isinstance(row.get('id'), str) and bool(row['id']))


def reference(owner, revision, row):
    if (not isinstance(revision, int) or isinstance(revision, bool) or revision < 0
            or not active(row)):
        raise PreferenceFenceError('stale_source')
    # Includes relation element identity, properties and immutable Concept
    # identity fields from the existing bounded authoritative snapshot.
    mac = bytes.fromhex(_mac('preference-owner-action-v1', encoded([owner, revision, row])))
    return PREFIX + base64.urlsafe_b64encode(mac).decode('ascii').rstrip('=')


def valid_format(value):
    return isinstance(value, str) and re.fullmatch(r'par1\.[A-Za-z0-9_-]{43}', value) is not None


def resolve(tx, owner, token, state):
    from .preference_bootstrap import snapshot
    if not valid_format(token):
        raise PreferenceFenceError('stale_source')
    current, pending = snapshot(tx, owner)
    if pending or current['revision'] != state['revision']:
        raise PreferenceFenceError('stale_source')
    candidates = [r for r in current['rows'] if active(r)]
    matches = [r for r in candidates if hmac.compare_digest(token, reference(owner, current['revision'], r))]
    if len(matches) != 1:
        raise PreferenceFenceError('stale_source')
    row = matches[0]
    # Do not select an arbitrary duplicate or conflicting owner association.
    if sum(r['concept']['key'] == row['concept']['key'] for r in current['rows']) != 1:
        raise PreferenceFenceError('stale_source')
    pending_keys = tx.run('''MATCH (u:User {id:$owner})
        RETURN coalesce(u.preference_action_projection_keys,[]) AS keys''', owner=owner).single(strict=True)['keys']
    if row['concept']['key'] not in pending_keys and len(pending_keys) >= MAX_PENDING:
        raise PreferenceFenceError('preference_projection_pending')
    return row


def mark_projection(tx, owner, key):
    # Atomic Graph-side recovery marker, written ONLY with a successful action.
    # Avoids both pre-validation Mongo intent writes and the lost-HTTP-ack gap.
    tx.run('''MATCH (u:User {id:$owner})
        SET u.preference_action_projection_keys = CASE
          WHEN $key IN coalesce(u.preference_action_projection_keys,[])
          THEN u.preference_action_projection_keys
          ELSE coalesce(u.preference_action_projection_keys,[]) + $key END''', owner=owner, key=key).consume()


def pending_projections(tx, limit):
    return [dict(r) for r in tx.run('''MATCH (u:User)
        WHERE size(coalesce(u.preference_action_projection_keys,[])) > 0
        UNWIND u.preference_action_projection_keys AS key
        RETURN u.id AS owner,key ORDER BY owner,key LIMIT $limit''', limit=limit)]


def acknowledge_projection(tx, owner, key, revision):
    state = lock_preferences(tx, owner, source_created_at=time.time(), require_existing=True)
    if state['revision'] != revision:
        raise PreferenceFenceError('stale_source')
    tx.run('''MATCH (u:User {id:$owner})
        SET u.preference_action_projection_keys =
          [key IN coalesce(u.preference_action_projection_keys,[]) WHERE key <> $key]
    ''', owner=owner, key=key).consume()
    return {'status': 'synced'}
