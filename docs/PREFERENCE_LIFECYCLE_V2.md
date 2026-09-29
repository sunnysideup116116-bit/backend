# Preference Lifecycle v2 / Permanent All-User Readiness

Lifecycle contract baseline (2026-09-28): backend PR #29 `709ffc9da2c6649ae55f27a182a9703d92694245`; DatingApp PR #45 merge `66ba6e8ecd3f2adbcaf95859073eac906e78dc8c`. Current running release and flags are in the overview below; this lifecycle contract is unchanged by the later semantic activation.
The separately approved internal administrative migration and universal writer
hardening are complete. The separately approved semantic activation completed on
2026-09-29; neither approval includes P1-B or APK packaging.
See [current architecture, evolution and scoped population snapshot](PREFERENCE_SYSTEM_OVERVIEW.md).

## Owner authority and first use

Requester eligibility remains `enabled_accounts`: an enabled Appwrite account,
one valid Mongo profile and one Graph User, with existing disabled/block/pending
guards. EMPTY and legacy-only requesters do not need a migration to search.
Candidate-evidence migration has separate authority. The current enabled internal
test population used an explicitly approved, one-time administrative operation;
future external/unconfirmed owners still require the confirmation flow below.

Client integration contract (backend implemented; management/confirmation UI merged in DatingApp PR #45):

1. On first preference/semantic settings entry, GET
   `/api/profile/preferences/bootstrap/source` with the owner's Appwrite JWT.
   It reads the **full owner-scoped active Graph PREFERS/AVOIDS set**, not the
   12-item profile cache. Both polarity arrays and identity status are shown.
   Each item returns an opaque `key` for correct/disable. This is a MAC, not an
   encoded canonical key or Neo4j ID. It binds owner, owner revision and the exact
   active association ID, polarity, properties and immutable Concept snapshot.
   Clients echo it unchanged; never generate, decode, truncate or repair it.
   Older responses without keys remain read-only for edit/delete.
   It performs no mutation, even when bootstrap execution is OFF.
2. Render every full string, separately for positive and negative polarity.
   Never split, paraphrase, infer additional items, guess AVOIDS empty, or
   pre-check confirmation controls. The owner may explicitly edit the set.
3. POST `/preview` with `mode=complete_set`, both arrays and `source_token`.
   This read-only discovery receipt binds owner, current Graph snapshot and
   Mongo fact snapshot for 10 minutes. A changed source requires a new preview;
   it is not a commit capability. Existing explicit manual preview callers remain
   compatible without this optional discovery token.
4. Show normalized full source plus exact create/reuse/retire effects. Commit
   only with the signed preview and all five explicit consent booleans from the
   existing contract. Unconfirmed sets remain untouched. Existing v2 entries
   cannot be silently omitted/reversed: use the explicit edit/disable API first.
5. Show reconciliation-required outcomes; never replay an unknown Graph write.
   After Graph commit, Mongo projection and Graph ack, the transactional queue
   can embed new active verified PREFERS. An embedding failure does not undo
   owner preferences or block exact matching.

Malformed/duplicate owner/profile/Graph identity, ambiguous polarity, stale
source, incomplete snapshot and unsafe legacy evidence fail closed per owner.
Historical pilot consent does not confirm a newly discovered complete set.
That rule is not a remaining blocker for the completed administrative internal
migration, and its explicit approval must not be generalized into silent migration.
READY owners no longer need the BLOCKED_CONFIRMATION prompt; EMPTY owners skip
migration and their first durable write goes directly to Identity-v2.

## New writes and immutable identity

Normal extraction, explicit manual add, registration and explicit feedback use
the existing full-text Identity-v2 canonicalizer. The shared Graph transaction
enqueues newly active PREFERS; registration's insert-only writer does the same.
No alias, legacy preference key, guessed preference or AVOIDS-from-test-rejection
writer is added. Event tags and expiring CURRENTLY_WANTS are separate existing
domains and are not durable preferences or new semantic evidence.

Manual `/api/profile/memories/add` and `/action` now require an enabled owner's
Appwrite bearer JWT matching the body user_id. **Client compatibility requirement:**
older clients sending only user_id must send their existing owner JWT; no
anonymous fallback. Ordinary writes require a unique usable profile and use the
shared Graph owner fence; duplicate Concepts also fail closed.

Correction creates/reuses the new verified v2 identity and archives only the
old owner's association as PREFERENCE_SUPERSEDED. It does not alter old/shared
Concept identity or vectors. Conflicting target polarity is rejected. Disable
retains MEMORY_DISABLED with original polarity. Verified v2 restore reuses that
identity; a legacy durable restore canonicalizes the full stored source using the
existing Identity-v2 normalizer, creates/reuses a verified v2 target, and preserves
owner and PREFERS/AVOIDS polarity. It never splits text or revives the legacy edge.
The old MEMORY_DISABLED edge remains inactive with a restored-target marker, so
replaying that legacy restore fails closed. Missing/ambiguous/lossy source,
conflicting target identity/polarity or duplicate associations roll back the whole
transaction. PREFERS queues missing compatible vectors in the same transaction;
AVOIDS does not enqueue positive evidence. No provider call occurs inside it.
Neither path deletes a shared node or changes historical vectors. Non-preference
CURRENTLY_WANTS restore retains its existing expiration semantics.

Correct/disable resolve the reference under the existing Graph owner write fence
and in the same transaction as mutation. Only one currently active PREFERS/AVOIDS
association can match. Disabled/retired, wrong owner, wrong polarity, changed
revision/properties/association, tampered and raw canonical keys fail closed with
public HTTP409 `stale_source`. The client must fetch full source again. No stale
action is idempotent success and no stale rejection stages Mongo projection or
invalidates caches. Even same-identity correction consumes the reference by
advancing the owner revision (without rewriting the shared Concept). Restore
retains its existing internal-key contract and increments revision; it never
revives an old active action reference. Legacy correct still requires explicit
complete-set reconfirmation; a reference is not permission to convert legacy.

Graph is authoritative; Mongo facts are partial evidence, not a full corpus.
Correct/disable/restore stage Mongo projection only AFTER Graph acknowledges success.
A successful Graph transaction atomically records the old key in the bounded
User `preference_action_projection_keys` recovery list (at most100 distinct keys;
full backlog fails closed). No new association IDs, nodes, indexes or migration
are required. Existing projection recovery can discover those committed markers
via service-signed private scan and acknowledge under a revision fence after
Mongo synchronization. Thus a lost HTTP acknowledgement or failed Mongo intent
write cannot lose recovery, and rejected actions leave no recovery marker. This
reuses the existing15-second projection worker, not an embedding worker.
Restore uses this same committed marker, not a pre-staged Mongo intent. Success reconciles only
the old key's `active` flag from current Graph under the existing Mongo fence;
it never manufactures facts, changes counters, rewrites identity or replays the
action. Existing waiting intents still wait75 seconds. New Social commands expire
30 seconds after source time, checked **after the Graph owner lock** on every
managed attempt; Graph action transactions are bounded to 20 seconds. Deployment
requires coherent host clocks and matching writer versions. Background recovery
polls every 15 seconds, leases 90 seconds and retries at most eight times with
bounded backoff. Exhaustion remains explicit failed/pending audit, not success.
The scan does not reset exhausted jobs or replay a mutation. Public action
responses contain status/projection_status, never internal projection keys.
The two databases are not distributed ACID. A concurrent Graph revision change
causes projection retry; bootstrap's snapshot/fact checks remain fail closed.

## Owner-scoped manual projection maintenance

`scripts/rebuild_neo4j_projection.py` is not migration authority. Its default
dry-run validates bounded Mongo reference shape and reports that Graph authority
has not yet been verified. `--apply` requires one explicit canonical `--owner`.
Under the existing owner fence it verifies every durable reference against an
already-active authoritative Graph association with verified Identity-v2 identity
and the same owner/polarity. Legacy payloads, missing/retired associations,
unverified identities and ambiguous sources fail closed; labels cannot authorize
identity creation. An empty Mongo fact cache does not authorize a legacy Graph
source either.

Durable Graph associations are preserved rather than deleted/recreated from
partial Mongo caches. Only that owner's expiring CURRENTLY_WANTS projection is
rebuilt; its key space cannot overwrite a v2 Concept. The CLI does not reset Graph
Users, erase shared Concepts/history/observations, infer polarity, or perform bulk
legacy conversion. Existing positive v2 references may enqueue missing vectors;
external embedding work remains outside the Graph transaction.

Recovery is not an exception to the Identity-v2 writer boundary. Bootstrap
rollback rejects any retirement journal that would restore a legacy preference
with HTTP409 `rollback_legacy_restore_forbidden`. Both the read-only check (before
Mongo prepare) and the authoritative Graph transaction enforce this restriction.
Pure v2 rollback keeps its existing after-image/revision/owner/archive guards;
any restored v2 Concept is revalidated under a shared-node lock. Forward
projection reconciliation remains available; operators must not force a legacy
rollback to repair a Mongo projection failure.

The historical `scripts/migrate_neo4j_preferences.py` remains a read-only
inventory tool. `--apply` unconditionally exits with
`legacy_preference_apply_forbidden` before reading secrets or opening databases.
Its historical key helper is not runtime migration authority.

## Resource-based complete-set contract

One complete-set transaction, not chunks: 1–64 items **combined** and at most
16,384 UTF-8 bytes across full source strings, before and after normalization.
Each source still has the existing 500-codepoint limit. Snapshot bounds remain
100 owner associations / 100 facts and serialized receipt/journal byte bounds.
All constraints apply; 64 is a resource ceiling, not a promise to admit an
oversized before-image. Overflows reject the entire request without mutation.
This handles the 11-item account without a special ID or exception.

Ordinary message default6/hard8 and add-only min(5, ordinary cap) are unchanged.
The 12-item profile UI cache stays bounded; owner discovery always reads Graph.

## Incremental embedding lifecycle

`PREFERENCE_EMBEDDING_V2_ENABLED=off` by default, exported by canonical
`start_all.sh` to Social and Matchmaker. After a separately approved deployment,
enabling this global worker switch handles future eligible writes automatically;
there is no per-user cohort edit. Queue metadata is committed **with** the
PREFERS edge, even if the worker is OFF. It uses `preference_embedding_*`, never
the reserved `embedding_v2*` namespace before a complete verified vector exists.

Social polls every30s with one local in-flight job. The signed private
`:9001/api/v2/preferences/embedding-jobs/{claim,finish,fail}` endpoints require
the worker switch, frozen metadata and ONLINE `concept_embedding_v2_index`.
They do not create indexes, bootstrap users, mutate preference authority or
accept provider/account configuration from callers. API transactions have a
10s bound and no automatic ambiguous-write replay.

Concept lease =300s; claim examines at most5 due Concepts. Before provider work,
recheck verified identity, source hash, active PREFERS and bootstrap projection
fence. Existing compatible vectors are reused; any incompatible/partial v2
metadata is quarantined, never overwritten. Failed provider attempts back off
30·2^attempt seconds (max3600), max8 attempts. A failed/blocked job needs operator
diagnosis; normal writes cannot silently reset its attempt limit. Lost finish
responses recover idempotently from compatible vector state/lease expiry.
Neo4j lock contention may return transient503 with rollback; a subsequent poll
retries, without issuing duplicate leases or replaying uncertain commits.

The worker uses the unchanged approved SafeGeminiEmbedding, prepare_batch and
get_embeddings pipeline: full persisted semantic_text, frozen prefix/model,
768 finite/nonzero L2-normalized components, source hash, runtime manifest and
provenance manifest. Metadata is checked before claim and provider creation;
SDK/source/model drift fails closed. Keys remain in process memory, never logs.
Finish checks lease/source/current PREFERS again and uses the existing
missing-only guarded writer. Removing the last positive owner cancels a late
completion. Bootstrap rollback only removes newly created orphan Concepts when
their only extra properties are queue metadata; shared/enriched/vector nodes
remain retained and a stale worker cannot recreate a removed node.

Runtime fingerprint:
`89cc12c59af783aa2953d856c15ba34625e2db129cf5d8bf5712e53af017b640`

Provenance fingerprint:
`53a1d22fd63bcde9be0fa632df532929a05dcb5aa60ded6879b47ad0c2c1c8af`

Before vector readiness, exact remains independent and semantic candidates fail
closed. AVOIDS-only/legacy-only/EMPTY owners supply no positive semantic evidence.
Current enabled-owner qualification, source/fingerprint proof, history/block/
consent, exact-first, validator ERROR handling, proposal-time recheck, kill and
continuous-unavailable protection are preserved. No old embedding/index fallback.

## Read-only readiness inventory

`scripts/report_semantic_rollout.py --lifecycle` produces bounded aggregates,
index/integrity results and the following disjoint categories. `--include-user-ids`
is for a private operator report only, never committed or public telemetry.

| Category | Meaning |
| --- | --- |
| READY | v2-clean; all positive evidence has compatible vectors (AVOIDS needs none) |
| MIGRATABLE | verified v2 set with derived embedding work pending; not an implicit legacy consent |
| EMPTY | no active PREFERS/AVOIDS; may still be an eligible requester |
| BLOCKED_IDENTITY | missing/ambiguous/disabled identity, corruption or unverified state |
| BLOCKED_CONFIRMATION | legacy full-set confirmation missing or unsafe to infer |
| BLOCKED_CAPACITY | item/text/snapshot resource envelope exceeded |

`technical_preview_ready` separately reports a deterministic complete-set preview.
It does not authorize commit. Requester eligibility and semantic-ready candidate
ownership are independent fields; one blocked migration never stops other owners.
No fixed population/vector count or old five-user monitor is restored.
Inventory also reports count-only incremental job states; failed/blocked jobs
must be investigated, not mistaken for compatible embedding coverage.

### Current enabled-population snapshot (2026-09-28)

39 enabled/eligible; READY29, EMPTY10, BLOCKED_IDENTITY0. Enabled active legacy
edges0; active PREFERS compatible vectors64/64. Two non-enabled historical legacy
edges are explicit out-of-scope, untouched: **not whole-Graph legacy0**. Full
flags/release provenance and audit scope are in the [current overview](PREFERENCE_SYSTEM_OVERVIEW.md#production-snapshot--2026-09-29).

### Historical / superseded preparation inventory (2026-09-27)

The following pre-migration snapshot is retained as evidence, not a current blocker:
39 enabled accounts;32 eligible requesters;READY5, MIGRATABLE0, EMPTY3,
BLOCKED_IDENTITY7, BLOCKED_CONFIRMATION24, BLOCKED_CAPACITY0. Five semantic-ready
owners,28 verified v2 Concepts/28 compatible vectors; index ONLINE. No duplicate
profiles/Graph Users/Concept keys, conflicting active owner associations, pending
bootstrap projection, unmapped active facts or truncation observed. Private IDs
are retained locally, not in this document. No production writes were performed.

## Release boundary

Separate approval is required for canonical deployment and worker/bootstrap flag
activation. Preflight unique identities, required existing constraints, Mongo
transaction support, frozen SDK/source metadata, ONLINE index and host clocks.
Drain old writers; no mixed-version action commands. Run flags-OFF exact checks
and approved rollout checks before activation. Do not enable the old frozen
five-user schedule, bulk-confirm users, rebuild historical embeddings or invoke
P1-B. Owner UI integration is now merged in DatingApp PR #45 using the API above;
merge/readiness is not release APK or activation approval. The completed one-time
internal migration is historical; this document authorizes no additional bulk job.

## Historical verification at preparation (superseded deployment status)

- Matchmaker full:307 passed (+17 subtests).
- Contracts:733 passed (+232 subtests), including canonical start_all.sh
  execution with isolated fake services and no real production ports.
- Focused Social:117 passed. Full Social head34 failed/1875 passed; exact clean
  base1c12b54 has36 failed/1873 passed in the same offline environment.
  New failing test names0; common failure messages identical. The two differing
  stream outcomes are not claimed fixed by this change. Existing Pi/auth/stream
  fixture failures are not an all-green baseline.
- Disposable Neo4j2026.08.1 + Mongo8.2.5:50 scenarios passed, including64-item
  atomic complete-set, capacity rejection, confirmation, queue/lease concurrency,
  provider retry surrogate, idempotent frozen vector write, owner-only retirement,
  shared Concept preservation and Mongo action projection. Synthetic-only:
  no actual Gemini call and no production readiness implied.
- One rehearsal stopped on the existing concurrent duplicate-bootstrap bounded
  deadlock retry; unchanged policy completed on rerun. New queue contention can
  roll back/return transient503; the test requires exactly one lease, not two
  guaranteed successful concurrent claims. No retry budget was increased.
- Canonical script shell syntax/diff whitespace checks pass. No production start
  was performed. Protected relation/ANN/threshold/canonicalizer/Gemini source
  functions remain unchanged; frozen source-hash test passes.
- GitNexus impact and detect-changes attempted against this exact checkout
  (`semantic-internal-pilot-phase2-server`), but it is not indexed/registered:
  graph-analysis risk UNKNOWN, not a clean or low-risk verdict. Direct call-site
  review and regression/integration evidence are the available checks.
