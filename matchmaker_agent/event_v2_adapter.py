"""Read-time Event relevance. Legacy projections/embeddings have no authority."""
from collections import OrderedDict
import hashlib
import json
import math
import os
import secrets
import threading
import time

from neo4j import Query

from .event_v2_contract import (POLICY, EventUnavailable, owner_signals, query_signal,
    exact_relevance, compatible_kinds, negative_state, snapshot_hash)
from .event_v2_vectors import query_vectors
from .preference_embedding_contract import frozen_contract, RUNTIME, PROVENANCE
from .related_interest_contract import INDEX_NAME, ACCEPTED, RELATIONS, bounded_counts
from .related_interest_retrieval import index_metadata, _DeadlineSession
from .related_interest_validator import PROMPT, validate_concepts
from .semantic_evidence_readiness import verified_vector, pair_preferences_safe
from .semantic_rollout_policy import requester_route_allowed
from .validator_diagnostics import current_trace, emit, remaining_ms

_receipts = OrderedDict()
_receipt_lock = threading.Lock()
_VALIDATOR_RELATION_CACHE_LIMIT = 128


def _validator_relation_key(query, concepts, model):
    """Digest only the exact relation request; never retain its source text."""
    texts = [concept.get('semantic_text') for concept in concepts]
    if not isinstance(query, str) or not isinstance(model, str) or not all(isinstance(text, str) for text in texts):
        return None
    material = json.dumps([
        'event-validator-relation-cache-v1', POLICY, model,
        hashlib.sha256(PROMPT.encode('utf-8')).hexdigest(),
        sorted(RELATIONS), sorted(ACCEPTED), query, texts,
    ], ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(material).hexdigest()


def _trusted_validator_outcome(concepts, accepted, counts):
    """Return only complete, error-free decisions, stored as positions + taxonomy."""
    if (not isinstance(accepted, list) or not isinstance(counts, dict)
            or counts.get('job_unavailable') or counts.get('error') != 0):
        return None
    if any(type(counts.get(key)) is not int or counts[key] < 0
           for key in ('accepted', 'rejected', 'error', *RELATIONS)):
        return None
    accepted_total, rejected_total = counts['accepted'], counts['rejected']
    if accepted_total + rejected_total != len(concepts):
        return None
    relation_total = sum(counts[relation] for relation in RELATIONS)
    if (relation_total != len(concepts)
            or sum(counts[relation] for relation in ACCEPTED) != accepted_total
            or relation_total - accepted_total != rejected_total
            or len(accepted) != accepted_total):
        return None
    indexes = {concept.get('concept_key'): index for index, concept in enumerate(concepts)}
    if len(indexes) != len(concepts):
        return None
    accepted_positions = []
    seen = set()
    for item in accepted:
        if not isinstance(item, dict) or item.get('relation') not in ACCEPTED:
            return None
        index = indexes.get(item.get('concept_key'))
        if (index is None or index in seen
                or item.get('semantic_text') != concepts[index].get('semantic_text')):
            return None
        seen.add(index)
        accepted_positions.append((index, item['relation']))
    if len(seen) != accepted_total:
        return None
    outcome_counts = {key: counts[key] for key in ('accepted', 'rejected', *RELATIONS)}
    return tuple(accepted_positions), outcome_counts


def infrastructure(session):
    """Data readiness is not a wait for every Concept; exact is independent."""
    try:
        frozen_contract()
        metadata = index_metadata(session)
        if not metadata['ready']:
            raise EventUnavailable('event_v2_index_unavailable', systemic=True)
        return {'status': 'ready', 'ready': True, 'exact_ready': True,
            'semantic_ready': True, 'policy': POLICY, 'index': metadata,
            'semantic_ready_scope': 'infrastructure_only',
            'all_vectors_required': False}
    except Exception as exc:
        code = exc.code if isinstance(exc, EventUnavailable) else 'event_v2_contract_unavailable'
        return {'status': 'degraded', 'ready': True, 'exact_ready': True,
            'semantic_ready': False, 'policy': POLICY, 'error_code': code,
            'semantic_ready_scope': 'infrastructure_only',
            'all_vectors_required': False}


def load_events(session, now):
    rows = list(session.run(Query('''MATCH (e:Event) WHERE e.status='active' AND e.expires_at>$now
        OPTIONAL MATCH (e)-[r:HAS_TAG|HAS_VIBE]->(c:Concept)
        WITH e,collect(CASE WHEN r IS NULL THEN null ELSE
            {reference:elementId(r),type:type(r),text:c.label} END) AS signals
        RETURN e{.id,.title,.summary,.venue,.region,.category,.starts_at,.ends_at,.time_precision,
          .session_starts,.session_ends,.session_precisions,.session_count,.source_url,.expires_at} AS event,
          signals ORDER BY e.starts_at,e.id LIMIT 101''', timeout=3), now=now))
    if len(rows) > 100:
        raise EventUnavailable('event_inventory_bound_exceeded')
    result, signals = {}, {}
    for row in rows:
        event = dict(row['event']); eid = event.get('id')
        if not eid or eid in result or len(row['signals']) > 10:
            raise EventUnavailable('event_inventory_ambiguous')
        if any(s.get('type') not in {'HAS_TAG','HAS_VIBE'} for s in row['signals']):
            raise EventUnavailable('event_signal_invalid')
        result[eid] = {'event': event, 'signals': sorted(row['signals'], key=lambda s: s['reference'])}
        signals[eid] = [query_signal(s['text'], 'tag' if s['type']=='HAS_TAG' else 'vibe',
            s['reference']) for s in row['signals']]
    return result, signals


def load_owner(session, owner):
    rows = [dict(r) for r in session.run(Query('''MATCH (u:User {id:$owner})-[r:PREFERS|AVOIDS|CURRENTLY_WANTS]->(c:Concept)
        WHERE coalesce(r.active,true)=true
        RETURN elementId(r) AS reference,type(r) AS polarity,r.active AS active,r.expires_at AS expires_at,
          u.preference_revision AS owner_revision,u.current_context_revision AS context_revision,
          c{.key,.canonical_key,.concept_key,.canonicalization_version,.semantic_text,.semantic_input_hash,.fidelity_status,.label,.kind,
            .embedding_v2,.embedding_v2_source_hash,.embedding_v2_fingerprint,
            .embedding_v2_provenance_fingerprint} AS props
        ORDER BY reference LIMIT 101''', timeout=3), owner=owner)]
    if len(rows) > 100:
        raise EventUnavailable('event_owner_signal_bound_exceeded')
    return rows


class Adapter:
    def __init__(self, session, client, model, *, deadline, clock=time.monotonic,
                 now=time.time, eligible=None, vectors=query_vectors, validate=validate_concepts,
                 allowed=requester_route_allowed):
        self.clock, self.deadline, self.now = clock, deadline, now
        self.session = _DeadlineSession(session, deadline, clock)
        self.client, self.model = client, model
        if eligible is None:
            from services.semantic_user_eligibility import EnabledUserChecks
            from .event_v2_api import profiles_for_events
            self.checks = EnabledUserChecks(self.session, deadline=deadline, clock=clock,
                profiles=profiles_for_events())
            eligible = self.checks.check
        self.eligible, self.vectors, self.validate, self.allowed = eligible, vectors, validate, allowed
        started = time.monotonic()
        try:
            self.events, self.signals = load_events(self.session, now())
        finally:
            emit('event_stage', stage='event_load', elapsed_ms=(time.monotonic()-started)*1000,
                remaining_event_ms=remaining_ms(deadline, clock))
        self.rows, self.results, self.ann = {}, {}, {}
        self.counts = {'ann_calls': 0, 'validator_calls': 0, 'pending_signals': 0}
        self.validator_counts = {}
        # Event-request-local relation results; no Graph refs, source text, or persistence.
        self.validator_relations = OrderedDict()
        self.semantic_owners = set()
        self.owner_diagnostics = {}
        self.decisions = {}

    def telemetry(self):
        trace = current_trace()
        diagnostic = {'diagnostic_id': trace.correlation_id} if trace else {}
        return {**self.counts, **diagnostic, 'semantic_triggered': bool(self.semantic_owners),
            'validator': bounded_counts(self.validator_counts)}

    def relevance(self, owner, *, event_ids=None, exact_only=False):
        started = time.monotonic()
        eligible = self.eligible(owner)
        emit('event_stage', stage='event_eligibility', elapsed_ms=(time.monotonic()-started)*1000,
            remaining_event_ms=remaining_ms(self.deadline, self.clock))
        if not eligible:
            code = 'event_owner_eligibility_unavailable' if getattr(getattr(self, 'checks', None), 'unavailable', False) else 'event_owner_ineligible'
            raise EventUnavailable(code)
        if owner not in self.rows:
            started = time.monotonic()
            try:
                self.rows[owner] = load_owner(self.session, owner)
            finally:
                emit('event_stage', stage='event_owner_signals', elapsed_ms=(time.monotonic()-started)*1000,
                    remaining_event_ms=remaining_ms(self.deadline, self.clock))
        rows = self.rows[owner]
        positive, negative = owner_signals(rows, self.now())
        requested = set(self.signals) if event_ids is None else set(event_ids) & self.signals.keys()
        requested = {eid for eid in requested if self.events[eid]['event'].get('expires_at', 0)>self.now()}
        active_refs = {p.reference for p in positive}
        for key,evidence in list(self.decisions.items()):
            if key[0]==owner and any(e['user_ref'] not in active_refs for e in evidence):
                del self.decisions[key]
        groups = {eid: self.signals[eid] for eid in requested}
        started = time.monotonic()
        exact, negative_unknown = exact_relevance(positive, negative, groups)
        emit('event_stage', stage='event_exact', category='success', elapsed_ms=(time.monotonic()-started)*1000,
            remaining_event_ms=remaining_ms(self.deadline, self.clock))
        self.owner_diagnostics[owner] = {'exact_event_count': len(exact)}
        if exact_only:
            return exact
        for eid, evidence in exact.items():
            self.decisions[owner,eid] = evidence
        for eid,group in groups.items():
            if negative_state(negative,group)=='blocked':
                self.decisions[owner,eid] = []
        groups = {eid: group for eid,group in groups.items() if (owner,eid) not in self.decisions}
        if not groups:
            return {eid:self.decisions[owner,eid] for eid in requested if self.decisions.get((owner,eid))}
        if negative_unknown:
            emit('event_stage', stage='event_relevance', category='negative_unavailable', typed_code='event_negative_unavailable')
            raise EventUnavailable('event_negative_unavailable')
        if not positive or not groups:
            return {}
        if not self.allowed(owner):
            raise EventUnavailable('event_semantic_disabled')
        readiness = infrastructure(self.session)
        if not readiness['semantic_ready']:
            raise EventUnavailable(readiness['error_code'], systemic=True)
        self.semantic_owners.add(owner)
        usable = [p for p in positive if p.namespace == 'event-recent-v1' or p.vector_ready]
        self.counts['pending_signals'] += len(positive)-len(usable)
        if not usable:
            raise EventUnavailable('event_positive_embedding_pending')
        event_signals = [s for eid, group in groups.items()
            if negative_state(negative, group)=='clear' for s in group]
        started = time.monotonic()
        try:
            vectors = self.vectors(event_signals + [p for p in usable if p.namespace=='event-recent-v1'],
                deadline=self.deadline, clock=self.clock)
        finally:
            emit('event_stage', stage='event_vectors', elapsed_ms=(time.monotonic()-started)*1000,
                remaining_event_ms=remaining_ms(self.deadline, self.clock))
        matches, incomplete, local_counts = {}, len(positive) != len(usable), {}
        # Same deployed config keys. Never guess a default activation threshold.
        try:
            threshold = float(os.environ['MATCH_PREFERENCE_SEMANTIC_MIN_SIMILARITY'])
            neighbor_limit = max(1, min(32, int(os.getenv('MATCH_PREFERENCE_SEMANTIC_NEIGHBOR_LIMIT', '24'))))
        except (KeyError, TypeError, ValueError):
            raise EventUnavailable('event_semantic_config_unavailable', systemic=True) from None
        if not math.isfinite(threshold) or not .75 <= threshold <= 1:
            raise EventUnavailable('event_semantic_config_unavailable', systemic=True)
        for signal in usable:
            concepts, positions = [], {}
            for event_id, group in groups.items():
                if negative_state(negative, group) != 'clear':
                    continue
                for target in group:
                    if not compatible_kinds(signal, target):
                        continue
                    vector = vectors.get(target.source_hash)
                    if vector is None:
                        incomplete = True
                        continue
                    score = None
                    if signal.namespace == 'preference-v2':
                        if target.source_hash not in self.ann:
                            self.counts['ann_calls'] += 1
                            started = time.monotonic()
                            hits = self.session.run(Query('''CALL db.index.vector.queryNodes($index_name,$limit,$vector)
                                YIELD node,score
                                WHERE EXISTS {MATCH (:User)-[r:PREFERS]->(node) WHERE coalesce(r.active,true)=true}
                                RETURN node{.key,.canonical_key,.concept_key,.canonicalization_version,.semantic_text,.semantic_input_hash,.fidelity_status,
                                  .embedding_v2,.embedding_v2_source_hash,.embedding_v2_fingerprint,
                                  .embedding_v2_provenance_fingerprint} AS props,score''', timeout=3),
                                index_name=INDEX_NAME, limit=neighbor_limit, vector=vector)
                            verified_hits = {}
                            for hit in hits:
                                props = dict(hit['props'])
                                if (not verified_vector(props, RUNTIME)
                                        or props.get('embedding_v2_provenance_fingerprint')!=PROVENANCE):
                                    continue
                                if props['key'] in verified_hits:
                                    raise EventUnavailable('event_concept_ambiguous')
                                verified_hits[props['key']] = hit['score']
                            self.ann[target.source_hash] = verified_hits
                            emit('event_stage', stage='event_ann', elapsed_ms=(time.monotonic()-started)*1000,
                                remaining_event_ms=remaining_ms(self.deadline, self.clock))
                        score = self.ann[target.source_hash].get(signal.comparison_key)
                    else:
                        recent = vectors.get(signal.source_hash)
                        if recent is None:
                            incomplete = True
                            continue
                        # Same Neo4j cosine score scale; query artifacts only.
                        score = (1+sum(a*b for a,b in zip(recent, vector)))/2
                    if type(score) not in (int, float) or not math.isfinite(score) or not threshold <= score <= 1.0000001:
                        continue
                    # Distinct activity text validated once, then bound back to current signals.
                    key = target.source_hash
                    positions.setdefault(key, []).append((event_id, target))
                    if not any(c['concept_key']==key for c in concepts):
                        concepts.append({'concept_key': key, 'semantic_text': target.text, 'similarity': min(1., score)})
            if len(concepts)>12:
                incomplete = True
                concepts = concepts[:12]
            if not concepts:
                continue
            if not self.allowed(owner):
                raise EventUnavailable('event_semantic_disabled')
            started = time.monotonic()
            cache_key = _validator_relation_key(signal.text, concepts, self.model)
            cached = self.validator_relations.get(cache_key) if cache_key else None
            if self.clock() >= self.deadline:
                emit('event_stage', stage='deadline', category='shared_deadline_exhaustion',
                    typed_code='semantic_validator_unavailable', final_typed_outcome='unavailable',
                    remaining_event_ms=remaining_ms(self.deadline, self.clock))
                raise EventUnavailable('semantic_validator_unavailable', systemic=True)
            if cached is None:
                self.counts['validator_calls'] += 1
                accepted, counts = self.validate(signal.text, concepts, self.client, self.model,
                    deadline=min(self.deadline, self.clock()+18), clock=self.clock,
                    is_enabled=lambda: self.allowed(owner))
                emit('event_stage', stage='event_relevance', elapsed_ms=(time.monotonic()-started)*1000,
                    remaining_event_ms=remaining_ms(self.deadline, self.clock))
            else:
                self.validator_relations.move_to_end(cache_key)
                accepted_positions, outcome_counts = cached
                accepted = [{**concepts[index], 'relation': relation}
                    for index, relation in accepted_positions]
                counts = {**outcome_counts, 'error': 0, 'attempts': 0,
                    'attempt_errors': 0, 'retries': 0}
            for key, value in bounded_counts(counts).items():
                self.validator_counts[key] = self.validator_counts.get(key, 0)+value
                local_counts[key] = local_counts.get(key, 0)+value
            if counts.get('job_unavailable') or self.clock()>=self.deadline:
                emit('event_stage', stage='event_relevance' if counts.get('job_unavailable') else 'deadline',
                    category='other' if counts.get('job_unavailable') else 'shared_deadline_exhaustion',
                    job_unavailable=bool(counts.get('job_unavailable')), typed_code='semantic_validator_unavailable',
                    final_typed_outcome='unavailable', elapsed_ms=(time.monotonic()-started)*1000,
                    remaining_event_ms=remaining_ms(self.deadline, self.clock))
                raise EventUnavailable('semantic_validator_unavailable', systemic=True)
            if not self.allowed(owner):
                raise EventUnavailable('event_semantic_disabled')
            if cached is None and cache_key:
                trusted = _trusted_validator_outcome(concepts, accepted, counts)
                if trusted is not None:
                    self.validator_relations[cache_key] = trusted
                    self.validator_relations.move_to_end(cache_key)
                    while len(self.validator_relations) > _VALIDATOR_RELATION_CACHE_LIMIT:
                        self.validator_relations.popitem(last=False)
            for item in accepted:
                if (item.get('relation') not in ACCEPTED
                        or item.get('concept_key') not in {c['concept_key'] for c in concepts}):
                    raise EventUnavailable('event_validator_evidence_invalid', systemic=True)
                for event_id, target in positions[item['concept_key']]:
                    matches.setdefault(event_id, []).append({'basis': 'semantic', 'relation': item['relation'],
                        'user_ref': signal.reference, 'event_ref': target.reference,
                        'source_kind': 'recent' if signal.namespace=='event-recent-v1' else 'durable',
                        'user_text': signal.text, 'event_text': target.text,
                        'score': round(item['similarity'], 4)})
        if (local_counts.get('error') and not (local_counts.get('accepted') or local_counts.get('rejected'))):
            raise EventUnavailable('semantic_validator_unavailable')
        if not matches and incomplete:
            raise EventUnavailable('event_positive_embedding_pending')
        for eid in groups:
            self.decisions[owner,eid] = matches.get(eid, [])[:3]
        return {eid:self.decisions[owner,eid] for eid in requested if self.decisions.get((owner,eid))}

    def select(self, owner, excluded):
        target = self.relevance(owner, exact_only=True)
        if not self.events or not owner_signals(self.rows[owner],self.now())[0]:
            return []
        # Request-local candidate bound, NOT a fixed enabled population/cohort.
        candidates = list(self.session.run(Query('''MATCH (u:User)
            WHERE u.id<>$owner AND NOT u.id IN $excluded AND EXISTS {
              MATCH (u)-[r:PREFERS|CURRENTLY_WANTS]->() WHERE coalesce(r.active,true)=true
              AND (type(r)<>'CURRENTLY_WANTS' OR r.expires_at>$now)}
            WITH DISTINCT u
            ORDER BY CASE WHEN EXISTS {MATCH (u)-[r:PREFERS]->(c:Concept)
                WHERE coalesce(r.active,true)=true AND c.key IN $exact_keys} THEN 0 ELSE 1 END,
                CASE WHEN u.id>$owner THEN 0 ELSE 1 END,u.id
            RETURN u.id AS id LIMIT 51''', timeout=3),
            owner=owner, excluded=list(excluded), now=self.now(),
            exact_keys=list({s.comparison_key for group in self.signals.values() for s in group})))
        incomplete = len(candidates)>50
        errors = {'event_candidate_pool_incomplete'} if incomplete else set()
        candidates = candidates[:50]
        # Exhaust qualified exact bridges in this bounded pool BEFORE any
        # candidate semantic fallback. Do not require A/B to share a key.
        exact_candidates, remaining = [], []
        for row in candidates:
            candidate = row['id']
            try:
                if not self.eligible(candidate):
                    incomplete |= getattr(getattr(self, 'checks', None), 'unavailable', False)
                    if getattr(getattr(self, 'checks', None), 'unavailable', False):
                        errors.add('event_owner_eligibility_unavailable')
                    continue
                exact = self.relevance(candidate, exact_only=True)
                self.results[candidate] = exact
                (exact_candidates if target.keys() & exact.keys() else remaining).append(row)
            except EventUnavailable as exc:
                incomplete = True
                errors.add(exc.code)
        for candidate_row in exact_candidates + remaining:
            candidate = candidate_row['id']
            other = self.results.get(candidate, {})
            common = target.keys() & other.keys()
            if common:
                event_id = min(common, key=lambda eid: (self.events[eid]['event'].get('starts_at') or 0,eid))
                if pair_preferences_safe(self.session,owner,candidate,query_key=''):
                    return [self.selection(owner,candidate,event_id,target[event_id],other[event_id])]
        for event_id in sorted(self.events, key=lambda eid: (self.events[eid]['event'].get('starts_at') or 0,eid)):
            try:
                event_target = self.relevance(owner,event_ids=[event_id])
            except EventUnavailable as exc:
                if exc.systemic:
                    raise
                incomplete = True
                errors.add(exc.code)
                continue
            if event_id not in event_target:
                continue
            for candidate_row in exact_candidates + remaining:
                candidate = candidate_row['id']
                try:
                    other = self.relevance(candidate,event_ids=[event_id])
                except EventUnavailable as exc:
                    if exc.systemic:
                        raise
                    incomplete = True
                    errors.add(exc.code)
                    continue
                if event_id in other and pair_preferences_safe(self.session,owner,candidate,query_key=''):
                    return [self.selection(owner,candidate,event_id,event_target[event_id],other[event_id])]
        if incomplete:
            if errors == {'semantic_validator_unavailable'}:
                if self.validator_counts.get('accepted') or self.validator_counts.get('rejected'):
                    return []  # Partial ERROR is excluded, not a job-level outage.
                raise EventUnavailable('semantic_validator_unavailable')
            if len(errors)==1:
                raise EventUnavailable(next(iter(errors)))
            raise EventUnavailable('event_candidate_evidence_unavailable')
        return []

    def selection(self, owner, candidate, event_id, target_evidence, candidate_evidence):
        event = self.events[event_id]['event']
        match = {'user_id': owner, 'candidate_id': candidate,
            'event_id': event_id, 'event_name': event['title'],
            'event_description': event.get('summary'), 'event_location': event.get('venue'),
            'event_region': event.get('region'), 'event_category': event.get('category'),
            **{k: event.get(k) for k in ('starts_at','ends_at','time_precision','session_starts',
                'session_ends','session_precisions','session_count','source_url','expires_at')},
            'event_policy': POLICY}
        for prefix,evidence in [('target',target_evidence),('candidate',candidate_evidence)]:
            match[prefix+'_links'] = [e['event_text'] for e in evidence]
            match[prefix+'_user_concepts'] = [e['user_text'] for e in evidence]
            match[prefix+'_source_kinds'] = [e['source_kind'] for e in evidence]
        all_evidence = target_evidence+candidate_evidence
        semantic = [e for e in all_evidence if e['basis']=='semantic']
        vector_refs = {e['user_ref'] for e in semantic if e['source_kind']=='durable'}
        used_refs = {e['user_ref'] for e in all_evidence}
        expiries = [r['expires_at'] for rows in (self.rows[owner],self.rows[candidate]) for r in rows
            if r['reference'] in used_refs and r['polarity']=='CURRENTLY_WANTS']
        proof = {'owner': owner, 'candidate': candidate, 'event_id': event_id,
            'snapshot': snapshot_hash({event_id:self.events[event_id]}, {owner:self.rows[owner], candidate:self.rows[candidate]},vector_refs=vector_refs),
            'vector_refs': vector_refs,
            'semantic': bool(semantic), 'relations': [e['relation'] for e in semantic],
            'min_score': min(e['score'] for e in semantic) if semantic else None,
            'until': min(self.now()+60,event['expires_at'],*expiries)}
        token = secrets.token_urlsafe(32)
        with _receipt_lock:
            _receipts[token] = proof
            while len(_receipts)>512:
                _receipts.popitem(last=False)
        match['event_receipt'] = token
        match['event_telemetry'] = self.telemetry()
        return match


def recheck(session, token, owner, candidate, *, deadline, now=time.time, clock=time.monotonic, eligible=None):
    with _receipt_lock:
        proof = dict(_receipts.get(token) or {})
    if (not proof or proof['owner']!=owner or proof['candidate']!=candidate or proof['until']<=now()):
        return False
    try:
        adapter = Adapter(session, None, None, deadline=deadline, now=now, clock=clock, eligible=eligible)
        if not adapter.eligible(owner) or not adapter.eligible(candidate):
            return False
        rows = {uid: load_owner(adapter.session, uid) for uid in (owner, candidate)}
        for value in rows.values():
            owner_signals(value, now())
        event_id = proof['event_id']
        if event_id not in adapter.events or snapshot_hash({event_id:adapter.events[event_id]}, rows,vector_refs=proof['vector_refs'])!=proof['snapshot']:
            return False
        if proof['semantic']:
            if not infrastructure(adapter.session)['semantic_ready'] or not all(adapter.allowed(u) for u in (owner,candidate)):
                return False
            threshold = float(os.environ['MATCH_PREFERENCE_SEMANTIC_MIN_SIMILARITY'])
            if (not math.isfinite(threshold) or not .75<=threshold<=1 or proof['min_score'] < threshold
                    or not all(r in ACCEPTED for r in proof['relations'])):
                return False
        return (clock()<deadline and proof['until']>now()
            and pair_preferences_safe(adapter.session, owner, candidate, query_key=''))
    except Exception:
        return False
