"""Event-only signal/evidence contract. No I/O or durable identity writes."""
from dataclasses import dataclass
import hashlib
import json
import math

from .concept_identity import canonicalize_fresh_concept, stored_concept_identity
from .preference_embedding_contract import RUNTIME, PROVENANCE
from .semantic_evidence_readiness import verified_vector

POLICY = 'event_relevance_v2'


class EventUnavailable(RuntimeError):
    """Typed Event failure. `systemic` propagates past candidate fallbacks.

    `resumable` marks an execution-boundary/infrastructure stop that preserved
    durable progress: the caller must checkpoint and retry later instead of
    treating the user's whole weekly work as permanently lost. Semantic
    negatives (`event_negative_unavailable`) and manual-kill stops are never
    resumable.
    """

    def __init__(self, code, *, systemic=False, resumable=False):
        self.code, self.systemic, self.resumable = code, systemic, bool(resumable)
        super().__init__(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class Signal:
    namespace: str
    text: str
    comparison_key: str
    source_hash: str
    kind: str
    reference: str
    polarity: str = ''
    vector_ready: bool = False


def query_signal(text, kind, reference, *, namespace='event-signal-v1'):
    """Reuse normalization for comparison only; NEVER persist its V2 key."""
    try:
        identity = canonicalize_fresh_concept(text)
    except ValueError:
        identity = None
    if not identity or kind not in {'tag', 'vibe', 'activity', 'interest'}:
        raise EventUnavailable('event_signal_invalid')
    return Signal(namespace, identity.semantic_text, identity.key,
        digest([namespace, kind, identity.semantic_text]), kind, reference)


def owner_signals(rows, now):
    positives, negatives, seen = [], [], set()
    for row in rows:
        if row.get('active') is False:
            continue
        polarity, props = row.get('polarity'), row.get('props') or {}
        if polarity == 'CURRENTLY_WANTS':
            expiry = row.get('expires_at')
            if not finite_number(expiry):
                raise EventUnavailable('event_recent_source_invalid')
            if expiry <= now:
                continue
            signal = query_signal(props.get('label'), props.get('kind', 'activity'),
                row['reference'], namespace='event-recent-v1')
        elif polarity in {'PREFERS', 'AVOIDS'}:
            identity = stored_concept_identity(props)
            if not identity:
                raise EventUnavailable('event_preference_source_unverified')
            signal = Signal('preference-v2', identity.semantic_text, identity.key,
                identity.semantic_input_hash, props.get('kind') or 'preference',
                row['reference'], polarity,
                bool(verified_vector(props, RUNTIME)
                     and props.get('embedding_v2_provenance_fingerprint') == PROVENANCE))
        else:
            raise EventUnavailable('event_polarity_invalid')
        # Duplicate/mixed active associations are not more supporting evidence.
        key = (signal.namespace, signal.comparison_key)
        if key in seen:
            raise EventUnavailable('event_signal_ambiguous')
        seen.add(key)
        (negatives if polarity == 'AVOIDS' else positives).append(signal)
    return positives, negatives


def compatible_kinds(user, event):
    # Current durable writer's 'preference' is treated as an interest only in
    # this Event adapter; activity/recent sources still compare with tags only.
    return user.kind in {'activity', 'interest', 'preference'} and (user.kind != 'activity' or event.kind == 'tag')


def negative_state(negatives, signals):
    # No semantic-negative classifier has been approved. Unknown is NOT safe.
    if any(n.comparison_key == s.comparison_key for n in negatives for s in signals):
        return 'blocked'
    return 'unavailable' if negatives else 'clear'


def exact_relevance(positives, negatives, events):
    result, unavailable = {}, False
    for event_id, signals in events.items():
        negative = negative_state(negatives, signals)
        unavailable |= negative == 'unavailable'
        if negative != 'clear':
            continue
        evidence = [(p, s) for p in positives for s in signals
                    if compatible_kinds(p, s) and p.comparison_key == s.comparison_key]
        if evidence:
            result[event_id] = [{'basis': 'exact', 'user_ref': p.reference,
                'event_ref': s.reference, 'source_kind': 'recent' if p.namespace == 'event-recent-v1' else 'durable',
                'user_text': p.text, 'event_text': s.text} for p, s in evidence[:3]]
    return result, unavailable


def snapshot_hash(event, owner_rows, *, vector_refs=()):
    """Exact authority is independent of derived vectors; semantic refs bind them."""
    snapshots = {}
    for owner,rows in owner_rows.items():
        snapshots[owner] = []
        for row in rows:
            props = dict(row['props'])
            for name in list(props):
                if name.startswith('embedding_v2'):
                    value = props.pop(name)
                    if row['reference'] in vector_refs:
                        # Handles invalid derived NaN values without letting an
                        # unused vector break canonical exact-source proof.
                        props[name+'_proof_hash'] = hashlib.sha256(repr(value).encode()).hexdigest()
            snapshots[owner].append({**row, 'props': props})
    return digest({'event': event, 'owners': snapshots})
