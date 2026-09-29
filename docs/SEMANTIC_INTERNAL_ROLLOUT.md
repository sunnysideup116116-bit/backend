# All internal semantic rollout — eligibility contract and historical preparation

This document retains the rollout eligibility contract and historical preparation
sequence. The accepted Phase 2 E2E and frozen R3 conclusions are not re-evaluated.
It does not authorize another pilot, bootstrap, embedding backfill or P1-B.

Current status (2026-09-29): enabled population migration, universal-v2 writer
hardening and **production semantic activation are complete**; DatingApp PR #45
is merged. Running backend `8ccfe09f688b92c105e961994e4d1da87ed480b7` uses
`enabled_accounts`, semantic active, related-interest ON, ANN score threshold
0.90 and kill cleared; bootstrap OFF, new V2 worker ON, historical worker OFF.
Earlier internal confirmation/capacity/activation blockers are superseded.
[Current flags, score semantics and kill/rollback runbook](PREFERENCE_SYSTEM_OVERVIEW.md).

## Ownership and admission

`matchmaker_agent/semantic_rollout_policy.py` owns routing, not identity proof.
`MATCH_RELATED_INTEREST_ROLLOUT_MODE=canary` is the deployment-compatible default;
its existing 2–10-ID contract remains only for staged compatibility. Explicit
`enabled_accounts` uses no allowlist and has no population-size cap. Unknown/off
mode fails closed. `start_all.sh` propagates and validates this server-only field.

All modes retain global semantic/related flags, fingerprint confirmation, kill
file and **qualified exact > 0 => exact only, zero ANN/validator calls**. Existing
exact matching does not depend on v2 completeness. A client cannot choose mode or
claim eligibility. Social uses canonical authenticated owner context; the 9001
API independently requires the existing signed matching owner scope.

In enabled-accounts mode, `semantic_user_eligibility.EnabledUserChecks` reads:

- current Appwrite `$id`/strict boolean enabled status;
- exactly one Mongo profile keyed by that ID, valid metadata, no pending projection;
- exactly one Graph User with the same ID, no disabled/block/pending state.

Missing, duplicate, malformed or unavailable identity fails closed. It does not
create a profile/account, refill quota, accept body-provided proof or cache a
positive result across requests. Mongo/SRV initialization is at 9001 startup,
not inside the request deadline. Each lookup consumes the same remaining budget,
has no retry, and uses at most two matching identity rows. Metadata cache is
operation-local and bounded to requester + 50 candidate owners, **not 51 users
in the installation**. Disabled metadata is never interpreted as a preference.

EMPTY and legacy-only owners with valid identities may request semantic searches.
Their preferences are not inferred or bootstrapped. Existing indeterminate legacy
hard-conflict qualification may still reject a particular pair; this is not a
blanket requester ban. Missing positive v2 evidence means no candidate evidence.

## Candidate proof and final proposal boundary

ANN uses only `concept_embedding_v2_index` / `Concept.embedding_v2`:
verified stored full v2 identity; matching semantic source hash and runtime
fingerprint; a recorded provenance fingerprint; 768 finite non-boolean numeric
values, non-zero L2 unit norm. A high similarity score is not relation approval.
The relation validator runs before bounded PREFERS owner expansion; taxonomy and
acceptance policy remain unchanged. PR #26 excludes Concept-level ERROR while
retaining trusted decisions; partial ERROR plus valid REJECT can be normal
no-match, whereas zero-trusted/systemic failure remains typed unavailable.
Negative/unknown/ERROR evidence never expands into a positive candidate.

Enabled owner metadata and fresh active PREFERS ownership are checked after
expansion. Each accepted packet is reread against current Concept/source/vector;
missing, duplicate, AVOIDS-only or mixed-polarity evidence is dropped. Limits on
neighbors/Concepts/per-Concept fanout/evidence/candidates are unchanged. Their
purpose is per-request work control; no population census is in the request path.
Popular concepts can exhaust fanout before qualification; an empty result does
not prove no eligible owner exists. No global scan or unbounded pagination retry.

Immediately before proposal insertion, `semantic_proposal_eligibility` rechecks
Risk block, current profiles and qualification, bounded pair history, then calls
the signed 9001 `/api/preferences/related-interest-recheck` endpoint. That endpoint
uses a **new** account/profile/Graph check instance and current allowed relation,
source/vector/PREFERS proof and hard-conflict state. Query intent is not a durable
requester preference; a copy claiming the requester owns Q requires fresh positive
Q proof. Failure drops the proposal and releases reserved quota. The final route,
mode and job ownership guard run again before insert. Existing consent, quota,
Matchmaker qualification and canonical match lifecycle remain in force.

The shared final budget is at most 5s (within the original selection deadline);
no new model calls or retries. Service unavailability is not a candidate. Existing
live-pair uniqueness/CAS handles duplicate proposals. These checks narrow TOCTOU
but are **not distributed ACID** across Appwrite, Neo4j, Risk and Mongo: an account
can change immediately after a read. Existing action/confirmation guards continue
to run. No claim of atomic cross-store account-disable enforcement is made.

## Read-only readiness inventory

`scripts/report_semantic_rollout.py` accepts explicit process environment only
(no dotenv). Appwrite accounts are paginated; missing/duplicate IDs, inconsistent
totals or any inventory bound overflow returns unavailable, never partial success.
Default output has aggregate counts only; `--include-user-ids` belongs in a private
operator artifact, never Git, public logs or a model prompt.

| Class | Definition/action |
| --- | --- |
| READY | v2-clean, every active positive preference has a compatible v2 vector; AVOIDS needs no positive vector |
| MIGRATABLE | current owner legacy snapshot admits a deterministic full preview plan; not committed, still requires consent |
| EMPTY | no active preferences; valid requester, no fabricated candidate proof |
| BLOCKED | identity ambiguity, unsafe/incomplete evidence, contract overflow or incompatible/missing embeddings; isolate that owner |

`enabled_semantic_requesters` measures technical eligibility independently of
activation; inspect actual process flags/kill rather than inferring activation
from the inventory count. Current accepted activation is recorded above. The old canary
cohort is no longer the enabled_accounts population model.
`semantic_ready_candidate_owners` requires at least one current positive vector-
backed v2 edge (an AVOIDS-only READY owner is not a positive candidate).
An otherwise valid owner with some legacy preferences may own separate ready v2
evidence; all existing conflict rules still apply. EMPTY is reported separately
from nonempty v2-clean. A BLOCKED bootstrap capacity does not invalidate requester
identity. Inventory is per-owner consistent reads, not one cross-store snapshot.

Optional `--since EPOCH --until EPOCH` aggregates only jobs/proposals **created**
in `[since, until)`. Lifecycle outcomes are read at `lifecycle_observed_at`.
Bounds/truncation/missing latency denominators are explicit. Deleted test proposals
are not reconstructed from surviving jobs; immutable operator test receipts are
separate evidence, not live invitation counts. Zero samples never means success.

## Owner confirmation flow (now implemented; internal backlog superseded)

The following owner-facing flow remains for future owners requiring confirmation;
DatingApp PR #45 supplies the UI. It is not a requirement to reconfirm the already
administratively migrated internal population. That one-time approval allowed
only deterministic existing-canonicalizer normalization with meaning/polarity
preserved, never inferred content or migration of other memory types.

1. On first semantic-feature entry, show an **optional readiness panel** for the
   authenticated owner. Requester access does not wait for preference migration.
2. Server reads a fresh owner-scoped active Graph set plus Mongo facts; display
   full PREFERS, full AVOIDS (including an explicit empty panel), and precise
   legacy retirement preview. Preserve original text/polarity/compound grouping.
   No model rewrite, suffix guessing, split, merge, alias invention or completion.
3. Owner explicitly confirms each complete-set consent flag; never prechecked.
   Disagreement edits/cancels the preview, not an automatic data repair. If the
   normal canonicalizer changes a legacy source, block that item/owner for review;
   do not silently treat changed output as owner confirmation of the original.
4. Use the existing owner-JWT bootstrap preview/commit API, signed plan, ten-minute
   TTL, current revision/snapshot/fence, 1:1 retirement binding and idempotency.
   A fresh snapshot/polarity/locator change requires a new preview and consent.
5. Verify committed Graph operation and synced Mongo projection, no unresolved
   legacy/duplicates, then queue an explicit missing-v2 embedding batch. Pending
   or failed projection is not ready. No semantic rating becomes PREFERS/AVOIDS.

Existing explicit owner confirmation may be reused only when it actually binds
the same owner/full current set/empty AVOIDS/retirement. Enabled status, participation
consent or an operator's inventory alone is not complete-set consent. This PR does
not authorize a bulk conversion or introduce a new confirmation write route.

### Historical capacity decision: 11-item owner (superseded)

The later lifecycle contract admits 1–64 combined items and 16 KiB full-source
UTF-8 within existing snapshot/receipt bounds. Ordinary memory/add-only caps did
not change; no split complete-set transactions. The original proposal below is
retained as design history, not a current 10-item limit.

This PR leaves complete-set capacity **10 combined PREFERS+AVOIDS**. That owner is
individually BLOCKED for bootstrap, not a rollout-wide blocker or requester ban.
Do not use two complete-set commits. General capacity should be designed against
the existing 100-edge snapshot and 100-fact resource bounds, ≤500 Unicode
codepoints per source, `MAX_BYTES=262144` signed JSON, and snapshot `< MAX_BYTES/5`.
Count alone cannot predict the before/after/retirement/compound proof envelope.

A separate capacity change should admit an entire set under **both** a bounded
item limit derived from the 100-edge contract and actual serialized plan/token
byte limits, before mutation. Test 11, boundary/overflow, large Unicode/property
payloads, compound locators, existing-v2 retention, stale snapshots, rollback and
all-or-nothing Mongo projection. Change only complete_set admission; ordinary
memory/extraction, add_only, candidate fanout and prompt budgets stay unchanged.

## Incremental embeddings: explicit, frozen, idempotent

`scripts/prepare_related_interest_embeddings.py --keys-file PRIVATE_KEYS.json
--provenance-file APPROVED_FROZEN.json` is dry-run. `--apply
--ack-versioned-embedding-write` still requires separate operation approval.
Only explicit, verified full v2 Concepts **missing all versioned metadata** are
embedded. A compatible existing vector is reused with zero provider call;
partial/stale/incompatible metadata stops the whole batch, never overwrites.

The approved provenance file pins model, SDK/wire-client versions, exact input
prefix/full semantic source, unchanged get_embeddings/normalize/unit_vector/
prepare_batch source hashes, runtime and provenance fingerprints. Drift fails
before embedding; live model metadata is revalidated. No fallback provider/model
or mutable-revision claim. Keys are process-memory only, ≤6 configured slots;
static failure counts replace partial-key/provider-body logging. No SDK retry.

At most 100 explicit keys, generation chunks ≤20, dimensions768, finite/nonzero
L2 validation. Generate/validate all vectors before one bounded transaction;
acquire keyed Concept locks, compare elementId/full source/hash, reuse a compatible
concurrent writer or abort a conflict, write only embedding_v2 properties, verify
count/provenance and every non-v2 property unchanged. Unknown commit acknowledgment
is reported explicitly and requires inspection/idempotent rerun, not blind retry.
The existing ONLINE dedicated index is required; `--create-index` now rejects as
a separately approved schema operation. No historical embedding/index is rebuilt.

## Rollout monitoring and release gate

The retired five-user/28-vector external schedule stays stopped. The new read-only
reporter covers enabled requesters, ready candidate owners, v2/vector coverage,
index/fingerprint, duplicate identities/owner edges, pending projections/journals,
active fact mapping, exact/semantic counts, ANN hash/scores, relation distribution,
retry/ERROR/typed unavailable, proposals/confirmation/acceptance, conservative
false-shared-copy alerts, reason/opening fallback and latency p50/p95 with samples.
Copy is inspected only in memory and not retained in output. Copy substring alerts
are conservative warnings, not a complete semantic correctness classifier.

Server continuous protection remains **3 consecutive typed unavailable jobs in
15 minutes**, now population-independent under enabled_accounts; no provider
error retries or thresholds are altered. New telemetry does not write memory.
Optional metric failure never authorizes a proposal. Operator report unavailable,
truncation, corruption, rejected-relation acceptance or false-shared claim requires
review/kill according to existing policy, not a fake healthy zero. A replacement
external schedule needs separate explicit approval; this PR creates none.

Historical preparation release sequence (completed by the later approved
shadow/active gates; not instructions to reset current production flags):

1. Use only `start_all.sh`, frozen reviewed commit, all semantic/related flags OFF;
   retain kill and bootstrap OFF. Do not manipulate the dirty primary worktree.
2. Validate all four health200, matching release/mode across processes, correct
   profile DB/account credentials, existing Graph indexes ONLINE, unique metadata,
   source/runtime/provenance and fresh inventory. Flags-OFF exact smoke must not
   invoke embedding/ANN/validator or new eligibility services.
3. Evaluate diagnostics and separately approve enabled_accounts activation; no
   new cohort list is required. Unready identities stay exact-only; unready legacy
   candidates are excluded without blocking other ready candidates.
4. Keep all kill/validator/qualification/history/block/consent/final proof controls.
   No timer reset, rollback of owner data, bootstrap or backfill is implicit.

The original preparation validation used hermetic service suites and owner-verified disposable
Neo4j/Mongo (synthetic vectors/fake relation responses), not production traffic or
a new model experiment. Local plans must use indexed Concept lookup, bounded
PREFERS expansion and no all-node/label scan. Deployment health/flags-OFF smoke
was a **later gate**, not claimed by those unit/integration tests. Its subsequent
production completion is recorded in the overview, separately from this evidence.
