# Event V2 adapter contract — implementation gate

Status: approved product decisions, local implementation only. Base: `8ccfe09f688b92c105e961994e4d1da87ed480b7`. No deployment, production scan, migration, flags or data changes are authorized by this document.

## Authority and data flow

```text
enabled owner + unique profile / Graph User
  ├─ active verified V2 PREFERS (authoritative stored identity)
  ├─ active CURRENTLY_WANTS (owner-scoped, finite future expiry)
  └─ active AVOIDS (negative only)
                 ↓
      Event-specific read-only adapter ← active Event tags / vibes
                 ↓
      exact user → activity relevance
                 └─ no usable exact → bounded V2 semantic relevance
                 ↓
      User A → Event ← User B (no common preference requirement)
                 ↓
      exclusions / history / quota / owner qualification
                 ↓
      fresh Event + owner evidence recheck → existing draft lifecycle
```

Exact compares deterministic normalized signal identities, not substring/fuzzy
display labels. The gate is per **owner → activity**, not per owner population
or shared preference. Qualified exact bridges are searched before semantic work.
For another activity without exact evidence, semantic may run for that owner.
Another owner's evidence cannot establish the first owner's relevance.

## Signal namespaces

Durable PREFERS/AVOIDS must pass `stored_concept_identity`; stale, inactive,
ambiguous or legacy durable source is unavailable, never upgraded by this adapter.
`CURRENTLY_WANTS` retains its original owner edge and expiry: non-finite,
expired or ambiguous source cannot establish positive evidence.

Event tag/vibe and recent-intent query representations have an `event-signal-v1`
namespace and content hash. Normalization helpers are reused solely for
comparison/encoding. They are **not** stored Preference V2 identities and never
create User preference edges, Concept nodes, bootstrap receipts or embedding jobs.
Activity signals match tags; interests may match tags or vibes, preserving the
existing Event signal distinction. Event data remains public activity metadata.

## Vector / inference contract

Only `concept_embedding_v2_index` (ONLINE, Concept.embedding_v2, 768, COSINE,
identity indexes ready) is used for durable positive ANN. Reuse `index_metadata`,
`frozen_contract`, `verified_vector`, `stored_concept_identity`, `unit_vector`,
the pinned Gemini encoder and existing relation validator. No copied validator,
canonicalizer or vector rules. Runtime and exact pinned encoder provenance must
match; source hashes must match current source.

Event/recent query vectors are bounded process-memory query artifacts with their
own namespace/source hash. Encoder provenance describes encoding only, not durable
identity authority. They use the same frozen encoder/input prefix/768/L2 space;
they are never persisted as `Concept.embedding_v2` or `Concept.embedding`.
Provider calls run in Social's existing pinned SDK environment over a private
path/body/time-signed query-vector API. Matchmaker adds no SDK dependency and
validates the returned namespace/source hash/runtime/encoder provenance/vector.
Provider calls occur outside Graph write transactions. A bounded cache never
grants owner authority; final recheck rereads authoritative state.

Semantic validation direction is **user signal Q → activity signal C**. Existing
accepted/rejected taxonomy and partial-ERROR semantics are reused. ERROR is never
evidence; systemic or zero-trusted-decision failure is typed unavailable. Active
semantic routing, emergency kill and continuous typed-unavailable protection are
reused, not bypassed. Existing production threshold config remains authoritative
(0.90 Neo4j score = raw cosine 0.80); no config or ordinary matching changes.

## Negative policy

Exact active AVOIDS blocks before positive evidence, independent of vector
readiness. This release does not invent a semantic-negative classifier: remaining
unresolved active avoidance returns `event_negative_unavailable` for that owner /
activity, including an otherwise exact positive. It cannot become an empty
constraint set. This is deliberately conservative and may reduce coverage; it is
not a system-wide failure and never blocks scanning other owners. AVOIDS is never
embedded/expanded as positive evidence. Pairwise preference conflict checks remain.

## Readiness, bounds and final authority

Readiness reports separate exact/source availability and semantic infrastructure
availability. Missing individual vectors is not global pending and there is no
900-second all-Concept barrier. Systemic index/contract failure disables semantic
evaluation with a typed result while exact remains independently available.
Bounds/overflow, invalid source or an exhausted shared deadline are explicit
unavailable states, not successful no-match. No silent population truncation.

Old EVENT_RELEVANCE/EVENT_AVOIDANCE projections are not trusted by the new runtime.
Relevance is computed from current sources. A short-lived opaque server receipt
binds owner IDs, event and the exact source/evidence snapshot; it is not client
authority. Immediately before draft insertion, refresh owner eligibility, Event
expiry/source, active polarity/expiry, vector/source/provenance and kill state.
Changed/missing/expired proof fails closed, including new avoidance. History,
block and live-proposal checks remain in the existing Event domain service.

Receipts (512, 60s) and query caches (1024 vectors, 300s) are bounded,
process-local and ephemeral; process
restart or cross-worker receipt miss rejects safely. There is no restart recovery
authority for an uncommitted selection. An already committed draft retains the
existing idempotency/lifecycle behavior. No automatic invitation acceptance.

Wording describes relevance to the activity, not shared/identical preferences.
Telemetry contains bounded counts/status/latency, never raw signals, vectors or
provider output. Proof/source snapshots remain request-local, not telemetry.

## Migration / release impact

- No preference or Event Concept migration/backfill; no vector/index creation.
- Do not enable historical worker or copy vectors to the legacy field.
- Existing derived Event links may remain historical data but cannot authorize a
  new proposal. Legacy refresh entrypoints must no longer invoke legacy encoding.
- Ordinary user-triggered exact/semantic, Preference lifecycle and thresholds are
  unchanged. Event adapters and tests are the only intended runtime changes.
- Event semantic terminal counts use a separate seven-day aggregate audit store,
  without owner/candidate IDs or source/provider text. The existing 3-in-15-minute
  typed-unavailable policy is reused and engages the same emergency kill file.
- Deployment, production smoke and any new Event signal coverage policy require
  a later gate. Local tests alone do not establish production readiness.

## Required local evidence

V2-ready/legacy-null; exact while embedding pending; different preferences with
exact relevance to one Event; V2-only ANN; invalid hash/runtime/provenance; index
failure; AVOIDS exact block/unresolved fail closed; expired recent intent; active
recent intent; Event signals never durable writes; ERROR exclusion; source/expiry/
kill changes at final recheck; no legacy projection fallback; bounded telemetry;
base/head Event + Matchmaker + Social differential.
