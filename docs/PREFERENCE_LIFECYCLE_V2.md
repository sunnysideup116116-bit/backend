# Preference Lifecycle v2 / Permanent All-User Readiness

Implementation and readiness preparation only. This change does not deploy,
migrate an account, generate production vectors, change matching policy, or
enable semantic traffic. No P1-B, relation/ANN/threshold change, or legacy-vector
fallback is introduced.

## Owner authority and first use

Requester eligibility remains `enabled_accounts`: an enabled Appwrite account,
one valid Mongo profile and one Graph User, with existing disabled/block/pending
guards. EMPTY and legacy-only requesters do not need a migration to search.
Migration of their candidate evidence is a separate owner-authorized operation.

Client integration contract (authenticated backend API implemented; UI not added):

1. On first preference/semantic settings entry, GET
   `/api/profile/preferences/bootstrap/source` with the owner's Appwrite JWT.
   It reads the **full owner-scoped active Graph PREFERS/AVOIDS set**, not the
   12-item profile cache. Both polarity arrays and identity status are shown.
   Each item also returns its owner-scoped opaque `key` for existing lifecycle
   action APIs. Clients echo this server-issued reference; they never generate
   canonical identity from text. Older responses without keys remain read-only.
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
retains MEMORY_DISABLED with original polarity; restore reuses the same identity
and enqueues verified positive evidence if needed. Neither deletes a shared node.

Graph is authoritative; Mongo facts are partial evidence, not a full corpus.
Actions stage a durable Mongo projection intent before Graph. Success reconciles
only the old key's `active` flag from current Graph under the existing Mongo fence;
it never manufactures facts, changes counters, rewrites identity or replays the
action. An unknown HTTP outcome waits 75 seconds. New Social commands expire
30 seconds after source time, checked **after the Graph owner lock** on every
managed attempt; Graph action transactions are bounded to 20 seconds. Deployment
requires coherent host clocks and matching writer versions. Background recovery
polls every 15 seconds, leases 90 seconds and retries at most eight times with
bounded backoff. Exhaustion remains explicit failed/pending audit, not success.
The two databases are not distributed ACID. A concurrent Graph revision change
causes projection retry; bootstrap's snapshot/fact checks remain fail closed.

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

Latest preparation inventory (2026-09-27, per-owner reads, not distributed ACID):
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
P1-B. Owner UI integration is still a client follow-up using the API above.

## Verification at preparation

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
