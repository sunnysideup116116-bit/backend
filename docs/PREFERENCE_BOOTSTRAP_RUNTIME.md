# Owner-confirmed preference bootstrap runtime

Base bootstrap runtime has completed the approved production integrity/smoke gates.
The narrow legacy-compound compatibility below is a separate release change;
deployment, real-owner bootstrap and pilot activation remain distinct steps.
`PREFERENCE_BOOTSTRAP_ENABLED=off` by default. All semantic/related-interest flags
remain OFF. No embedding, model, matching, invitation, bulk user discovery or
production migration is part of this operation. Identity v2 is reused unchanged.

## Owner API and confirmation

Prefix `/api/profile/preferences/bootstrap`, Appwrite bearer JWT authentication.
Owner comes only from the verified identity, never a body user_id or model tool.

| API | Behavior |
| --- | --- |
| POST `/preview` | Read-only `mode`, required `prefers` AND `avoids` lists (explicit `[]` permitted for one polarity) |
| POST `/commit` | Signed `preview_token` + strict boolean consent |
| GET `/operations/{id}` | Read-only owner-scoped status; does not reconcile |
| POST `/operations/{id}/reconcile` | Recover projection/ambiguous outcome; never replay preference writes |
| POST `/rollback` | Operation ID, original after revision, explicit `confirmed: true` |

`add_only`: confirm_items only; never retires legacy relations.
`complete_set`: confirm_items, complete_set, reviewed_prefers, reviewed_avoids,
retire_legacy must **each** be true. An omitted AVOIDS panel is not consent.
No default checked controls or LLM/admin substitute for owner consent.

`complete_set` admits 1–10 items per owner (atomic by default; narrowly verified
legacy-compound exception below), counted as PREFERS + AVOIDS
**combined**, in one preview/commit; 11 rejects the whole request. It is not split
into partial commits. `add_only` retains 1–min(5, durable_memory_limit()). The
shared bootstrap count validator runs at both HTTP boundaries and Graph admission;
only complete-set capacity is independent of the ordinary memory limit. Normal
extraction/write/registration/feedback caps and all other quotas are unchanged.
Both modes use whole-request validation, Unicode code-point ≤500 before and after normal conversion. No prefix
truncation, inferred polarity, duplicate alias rows, or mixed-polarity compound.
Fresh text uses the existing fixed OpenCC normalization then deterministic v2
identity. Stored v2 identity is independently verified, never converted again.

### Owner-confirmed legacy compound preservation

Only `complete_set` can propose preserving one compound as one v2 identity. There
is no new client override field. The server reads the owner's current snapshot and
requires exactly one active PREFERS/AVOIDS legacy association with the same **literal
text and polarity**. Other-owner, absent, inactive, ambiguous duplicate, reversed,
or claimed-v2 sources fail closed. No suffix reconstruction, split, renamed phrase,
sub-preference extraction, semantic alias or global Concept rewrite is performed.

The item carries an owner-private `legacy_compound_source`: owner, physical
relationship locator, legacy key, relation, exact association hash and owner snapshot
hash. This proof is inside the signed preview and plan hash. Preview is read-only
and provisional; mutation still requires every existing complete-set consent flag,
including reviewed PREFERS, reviewed AVOIDS (explicit empty allowed) and retirement.
There is no consent bypass at Graph commit or Social coordination.

The legacy source is fed whole to the **unchanged** v2 canonicalizer. Its returned
semantic_text must equal the source code point for code point. If prefix removal,
Unicode/case/alias normalization would alter the stored text, this compatibility
path rejects (`legacy_compound_text_not_lossless`) rather than changing the identity
contract. Normal mixed-polarity/protected-content/text-length/count checks remain.
An internal enumeration counts as one item, never an increased ordinary extraction
cap. Ordinary Social memory-write preflight and bootstrap add-only still reject
compound preservation; the existing 9001 atomic decomposition behavior is unchanged.

Preview and locked commit revalidate the binding against a fresh server snapshot.
The source must map one-to-one to the plan's retired association; the signed receipt's
retirement list must agree with that plan. Changes to locator, text, polarity,
properties or owner revision invalidate the receipt before mutation. Audit retains
`legacy_compound_sources` alongside existing before/after/retirement records, not on
the globally shared Concept. Mongo projection, idempotency, fencing and precise
rollback use the existing transaction/reconciliation path unchanged.

The result is one full Identity-v2 key. Querying a sub-fragment is **not** an exact
match for that composite; no atomic child identities or relations are added. Normal
versioned embedding and related-interest validation may later use the full source
under separately approved rollout. This exception only proves a fresh legacy source;
it does not grant a general future compound-edit/write exemption after retirement.

Preview returns normalized full semantic_text/display_label/key/version/hash,
Concept create/reuse lists, edge create list, exact legacy retirement locators,
untouched data, owner revision/snapshot hash, UUID/plan hash/10-minute expiry.
Signed receipt uses existing authoritative APPWRITE_API_KEY resolver with a
separate HMAC domain; Social→9001 uses another path/body/time-bound domain.
Neither secret nor bearer token is in a journal/log. Owner preference before-images
are private owner data in receipt/audit, never public telemetry/model input.

Inventory is bounded at 100 owner edges and 100 Mongo facts, with overflow rejection;
it is not a paginated partial inventory. The 10-item input limit does not truncate
retirement inventory: every approved legacy owner association appears in the same
preview, even if there are more legacy associations than submitted items.
Unsupported non-JSON Graph properties
(including temporal objects) fail closed rather than lossy conversion. Existing
v2 items must all remain in complete_set with their original polarity. Explicit
edit/disable then repreview handles removing v2 items; this operation retires only
legacy associations. Invalid claimed-v2 is corruption, not retireable legacy.

## Transactions, fence and revision

Graph User `preference_revision` is the shared node-write fence. Every online
preference writer locks before reading/changing preference state, then bumps the
revision in the same transaction. Bootstrap compares revision AND exact snapshot
and Concept preconditions again inside this lock. Stale means no preference mutation.

Writer inventory:

- ordinary/manual/extracted memory apply + observation dedupe;
- explicit decline feedback (through the same apply path);
- insert-only registration seed;
- disable, restore, correction;
- owner recent-context projection (conservative snapshot invalidation).

Context projection no longer deletes OTHER owners' expired edges as a side effect:
readers already exclude expired context. Existing shared Concept labels/kinds are
not rewritten by context projection. Event retrieval/ranking is not changed.
Nickname/profile metadata projection does not change preference state. Existing
Concept embedding workers may update derived embedding/kind; identity fields are
still protected by Identity-v2. Derived kind is not an owner snapshot field.

In one Graph transaction: create/reuse verified v2 nodes; create confirmed edges;
archive preview-selected legacy edges as `PREFERENCE_SUPERSEDED` with original
polarity/properties and operation locator; remove only those active owner edges;
bump revision; save immutable before/after audit and persistent projection fence.
No legacy key repair, alias creation or ownership movement. The archive remains
for audit and is excluded by existing PREFERS/AVOIDS readers.

An explicit full-set operation advances an owner source epoch/cutoff. Pending
extraction/feedback/outbox jobs retain original saved-source timestamps; unknown
or pre-epoch jobs cannot resurrect retired preferences. Add-only does not advance
this epoch. Registration after a full-set epoch is never re-seeded. Mongo feedback
projection uses the same epoch cutoff, closing delayed Graph→Mongo delivery races.
This uses the deployment's shared host clock; clock skew is a rollout precondition.

Graph transaction deadline is 8 seconds total. A confirmed Neo4j
DeadlockDetected rollback may retry **once** using remaining time. No transport or
ambiguous commit retry; journal resolution handles those. No automatic driver
transaction retry is layered around bootstrap mutation. New Graph API DB work is
off the async event loop. Ordinary writer retry policies are otherwise unchanged.

## Graph and Mongo are NOT distributed ACID

Mongo uses local multi-document transactions (replica set/sharded support required).
The owner profile is its local fence for facts/projection:

1. Read-only Graph recheck; Mongo snapshot recheck under local fence.
2. Persist intent and invalidate visible memory cache/set pending atomically.
3. Graph atomic mutation, audit and persistent `preference_projection_pending`.
4. Idempotent Mongo projection: retire only exact captured facts, rebuild bounded
   cache, record projected revision/before+after facts, clear Mongo pending.
5. Graph acknowledgement releases ordinary-writer fence; mark operation complete.

No cache/fact reader surfaces retired data during pending. A Graph commit followed
by Mongo failure reports `reconciliation_required` + known Graph status/revision.
Graph is not repeated. A lost ack reuses projected revision without duplicate cache
increment. Background recovery (15s poll, at most10 operations, 30s age minimum)
and owner reconciliation are idempotent; no raw exception/payload is logged.

If commit outcome is unknown, resolution takes the owner fence. A missing journal
creates an aborted tombstone: a late original request cannot mutate afterward.
Duplicate operation submits return the same outcome; contention can return a
retryable unavailable/reconciliation response, not a second mutation.

Rollback checks original after revision **and** after-image. Later modification
blocks before cache invalidation. It removes exact created edges, restores original
legacy relation/properties (new physical relationship IDs are audited), and bumps
revision. Created Concepts are deleted only if still unchanged and unreferenced;
shared/enriched nodes are retained. Mongo restores only captured changed facts.
Rollback failure after preparation reconciles current Graph state, never clobbers
a later writer. Replaying an old receipt cannot recommit a rolled-back operation.

## Enablement / rollback proposal (not performed)

[Profile Writer Hardening](PROFILE_WRITER_HARDENING.md) supplies the shared
creation/recovery boundary for online profile initializers. It does **not** clean
existing duplicate profiles or create the required user_id unique index. Quiesced
repair/index/readiness steps below remain separate approvals and prerequisites.

1. Keep semantic flags OFF and bootstrap OFF. Review/merge, then separately authorize
   deployment of **all** Graph writers + Social timestamp/projection writers via
   `start_all.sh`. No mixed-version writer overlap; drain old background jobs.
2. Confirm Mongo supports transactions, owner profiles are unique, host clocks are
   coherent, worker recovery healthy. Standalone Mongo is not compatible with the
   new fact-write fence; do not deploy until this is checked.
3. Read-only schema preflight requires uniqueness on User.id, Concept.key and
   PreferenceBootstrapOperation.id. Runtime does not create these constraints.
   If missing, request separate explicit schema-write approval (never automatic).
4. Disable destructive demo/admin reset endpoints and stop out-of-band seed/repair
   scripts for the rollout window. Such maintenance is not a concurrent online
   writer and cannot be run alongside active bootstrap.
5. Only after approval enable bootstrap for the small consenting internal cohort.
   Owners preview/reconfirm separately; no operator bulk apply. Audit and reconcile
   every operation before the separately approved embedding_v2 phase.
6. Disable bootstrap to stop new operations; existing reconciliation/rollback stays
   available. After a completed bootstrap, do not roll back to old writers that
   ignore source epochs/revisions/archive/pending fences. Code rollback requires a
   fence-aware release and no unresolved operations; do not delete audit records.

Known boundaries: no UI added; authenticated API only. Neither historical embedding
fingerprint nor vector readiness is changed. Production compatibility/enablement
is NOT implied by local PASS. No production bootstrap, new index, backfill,
embedding_v2, semantic enablement, P1-B, or DatingApp change in this PR.

## Validation

See `tests/readiness/preference_bootstrap/README.md` for real disposable DB rehearsal,
offline suites and cleanup. The older BOOTSTRAP_CONTRACT/model is preserved as
design evidence (`full_set` there corresponds to runtime `complete_set`); it is not
the deployed protocol implementation.
