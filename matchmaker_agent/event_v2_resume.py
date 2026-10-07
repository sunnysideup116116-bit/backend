"""Opaque Event progress checkpoint: encode, decode, rebind, validate.

Pure module: no I/O, no persistence, no raw preference text. A checkpoint
stores only non-private identifiers (Graph element ids), actor digests,
relation names/scores and the execution position, so a later worker attempt can
resume the requester's semantic groups instead of restarting from group 1.

Trusted-decision semantics are unchanged: a checkpointed decision is only
rebound to the *current* owner/event objects, the whole checkpoint is discarded
when the event inventory snapshot changed, and an actor's decisions are
discarded when that actor's own signal references/text changed.
"""
from .event_v2_contract import POLICY, digest

CHECKPOINT_VERSION = 1
MAX_EVIDENCE = 96


def actor_digest(positive):
    """Digest an actor's current positive signals; no raw text is exposed."""
    return digest(sorted((str(getattr(s, 'reference', '') or ''),
                          str(getattr(s, 'text', '') or '')) for s in positive or []))


def snapshot(owner_rows, events, signals, *, policy=POLICY):
    """Deterministic identity of the exact inputs a checkpoint was computed on."""
    owner = sorted(
        (str(row.get('reference') or ''), str(row.get('polarity') or ''),
         row.get('expires_at') if isinstance(row.get('expires_at'), (int, float)) else None)
        for row in (owner_rows or [])
    )
    events_id = sorted(
        (str(eid), (entry.get('event') or {}).get('expires_at'))
        for eid, entry in (events or {}).items()
    )
    signal_refs = sorted(
        (str(eid), tuple(sorted(str(getattr(s, 'reference', '') or '') for s in group)))
        for eid, group in (signals or {}).items()
    )
    return digest({'policy': policy, 'owner': owner, 'events': events_id, 'signals': signal_refs})


def evidence_refs(evidence):
    """Reduce bound evidence dicts to opaque references + outcome only.

    `user_text`/`event_text` and Graph refs never enter the checkpoint; only
    the relation label, score and the opaque element references do.
    """
    refs = []
    seen = set()
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        user_ref = item.get('user_ref')
        event_ref = item.get('event_ref')
        relation = item.get('relation')
        if not all(isinstance(value, str) and value for value in (user_ref, event_ref, relation)):
            continue
        key = (user_ref, event_ref, relation)
        if key in seen:
            continue
        seen.add(key)
        score = item.get('score')
        refs.append({'user_ref': user_ref, 'event_ref': event_ref, 'relation': relation,
                     'score': round(float(score), 4) if isinstance(score, (int, float)) else None,
                     'source_kind': 'recent' if item.get('source_kind') == 'recent' else 'durable'})
    return refs[:MAX_EVIDENCE]


def encode(*, owner, snapshot_hash, decisions, actor_digests, progress=None,
        partial=None, model=None, fallback_model=None, policy=POLICY):
    """Build the opaque checkpoint returned to the caller.

    `decisions` maps (actor, event_id) -> fully trusted evidence. `partial`
    maps (actor, event_id) -> evidence accumulated for signals already scanned
    but whose remaining signals were not reached. `progress` maps a canonical
    key -> next signal index.
    """
    completed = {}
    for (actor, event_id), items in (decisions or {}).items():
        actor, event_id = str(actor), str(event_id)
        refs = evidence_refs(items)
        if not actor or not event_id or not refs:
            continue
        completed[f'{actor}|{event_id}'] = refs
    partial_out = {}
    for (actor, event_id), items in (partial or {}).items():
        actor, event_id = str(actor), str(event_id)
        refs = evidence_refs(items)
        if not actor or not event_id or not refs:
            continue
        partial_out[f'{actor}|{event_id}'] = refs
    return {
        'v': CHECKPOINT_VERSION,
        'policy': policy,
        'owner': str(owner),
        'snapshot': str(snapshot_hash),
        'model': model if isinstance(model, str) else None,
        'fallback_model': fallback_model if isinstance(fallback_model, str) else None,
        'completed': completed,
        'partial': partial_out,
        'actor_digests': {str(actor): str(value)
            for actor, value in (actor_digests or {}).items()
            if isinstance(value, str)},
        'progress': {str(key): {'next_index': max(0, int(value.get('next_index', 0))),
                                'signal_count': value.get('signal_count')
                                if type(value.get('signal_count')) is int else None}
            for key, value in (progress or {}).items() if isinstance(value, dict)},
    }


def decode(value):
    """Return a structurally valid checkpoint or None. Never raises."""
    if not isinstance(value, dict):
        return None
    if value.get('v') != CHECKPOINT_VERSION or value.get('policy') != POLICY:
        return None
    owner, snapshot_hash, completed = value.get('owner'), value.get('snapshot'), value.get('completed')
    if not isinstance(owner, str) or not owner:
        return None
    if not isinstance(snapshot_hash, str) or not snapshot_hash:
        return None
    if not isinstance(completed, dict):
        return None
    clean = {}
    for key, items in completed.items():
        if not isinstance(key, str) or key.count('|') != 1:
            return None
        if not isinstance(items, list):
            return None
        for item in items:
            if (not isinstance(item, dict) or not isinstance(item.get('user_ref'), str)
                    or not isinstance(item.get('event_ref'), str)
                    or not isinstance(item.get('relation'), str)):
                return None
        clean[key] = items[:MAX_EVIDENCE]
    partial_in = value.get('partial')
    if partial_in is not None and not isinstance(partial_in, dict):
        return None
    partial = {}
    for key, items in (partial_in or {}).items():
        if not isinstance(key, str) or key.count('|') != 1 or not isinstance(items, list):
            return None
        for item in items:
            if (not isinstance(item, dict) or not isinstance(item.get('user_ref'), str)
                    or not isinstance(item.get('event_ref'), str)
                    or not isinstance(item.get('relation'), str)):
                return None
        partial[key] = items[:MAX_EVIDENCE]
    digests = value.get('actor_digests')
    if digests is not None and not isinstance(digests, dict):
        return None
    progress_in = value.get('progress') or {}
    if not isinstance(progress_in, dict):
        return None
    progress = {}
    for key, slot in progress_in.items():
        if not isinstance(key, str) or not isinstance(slot, dict):
            return None
        next_index = slot.get('next_index')
        signal_count = slot.get('signal_count')
        if type(next_index) is not int or next_index < 0:
            return None
        if signal_count is not None and type(signal_count) is not int:
            return None
        progress[key] = {'next_index': next_index, 'signal_count': signal_count}
    model, fallback = value.get('model'), value.get('fallback_model')
    return {'owner': owner, 'snapshot': snapshot_hash, 'completed': clean, 'partial': partial,
            'actor_digests': {str(k): str(v) for k, v in (digests or {}).items()
                if isinstance(v, str)},
            'progress': progress,
            'model': model if isinstance(model, str) else None,
            'fallback_model': fallback if isinstance(fallback, str) else None}


def scope_key(scope):
    """Canonical key for a relevance() scope (None = all events)."""
    return None if scope is None else '|'.join(sorted(str(event_id) for event_id in scope))


def progress_key(actor, scope):
    key = scope_key(scope)
    return f'{actor}|*' if key is None else f'{actor}|{key}'


def progress_for(checkpoint, actor, scope):
    if not checkpoint:
        return None
    entry = (checkpoint.get('progress') or {}).get(progress_key(actor, scope))
    return entry if isinstance(entry, dict) else None


def rebind(evidence, *, signals_by_ref, targets_by_ref):
    """Rebind stored refs to the current run's objects.

    Returns None when any ref no longer exists (owner/event changed) so the
    caller discards the checkpoint instead of trusting stale evidence.
    """
    rebound, seen = [], set()
    for item in evidence or []:
        signal = signals_by_ref.get(item.get('user_ref'))
        target = targets_by_ref.get(item.get('event_ref'))
        if signal is None or target is None:
            return None
        key = (item.get('user_ref'), item.get('event_ref'), item.get('relation'))
        if key in seen:
            continue
        seen.add(key)
        rebound.append({'basis': 'semantic', 'relation': item['relation'],
            'user_ref': item['user_ref'], 'event_ref': item['event_ref'],
            'source_kind': item.get('source_kind') or 'durable',
            'user_text': signal.text, 'event_text': target.text,
            'score': item.get('score') if item.get('score') is not None else 0.0})
    return rebound
