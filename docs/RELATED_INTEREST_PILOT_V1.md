# Semantic Related-Interest Matching v1 — internal app-wide pilot

Initial implementation was based on main `64bb7e7`; the implementation-gate
results below are historical. **Flags default OFF.** The accepted two-account
canary is followed by Phase 2 preparation for 5–10 explicitly confirmed,
v2-clean internal owners, not all-user activation. Production expansion is
blocked until each added owner confirms the full preference set and retirement
preview and compatible vectors are verified; see Phase 2 below. Existing
identity/auth/safety/confirmation boundaries remain mandatory for every account.

This is a new product policy for conversation/match discovery, not strict
preference satisfaction. [Research closeout](PREFERENCE_SEMANTIC_ROLLOUT_DECISION.md)
and all frozen R3.3/R3.4 FAIL evidence remain unchanged. This implementation is not
a new model/backend sweep, holdout experiment or P1-B generic hybrid retrieval.

## Flow and owners

```text
Explicit preference → deterministic identity v2 → indexed exact PREFERS retrieval
→ existing block/history/cohort/profile filtering + hard-conflict qualification
→ qualified exact > 0: unchanged exact pipeline (no semantic refill)
→ qualified exact == 0, all server flags enabled, requester in server canary:
  Gemini query embedding / verified embedding_v2 reuse
  → concept_embedding_v2_index bounded ANN
  → full-source/fingerprint checks
  → DeepSeek relation-only validation
  → accepted Concepts only → bounded canary PREFERS owner expansion + dedupe
  → existing filters / qualification / max-20 pool / Matchmaker
  → existing quota / proposal dedupe / confirmation / invitation / mutual consent
```

- Social `preference_semantic_service.py`: ephemeral Gemini query, strict typed
  internal response, total deadline. It never writes owner memory or query vectors.
- 9001 `related_interest_retrieval.py`: dedicated index, provenance validation,
  validator before user expansion, read-only Graph. No fallback to old vectors.
- `related_interest_validator.py`: existing Matchmaker DeepSeek credentials/endpoint/model,
  temperature 0, default reasoning, 4096-token output, batch 2. No DeepSeek embedding.
- `related_interest_contract.py`: one v1 relation policy and evidence validation;
  independent of the archived strict acceptance mapping.
- Social `related_interest_reason_service.py`: role-bound factual Ayue copy,
  rendered from validated evidence rather than allowing model prose to invent facts.
- Existing match action/decision services keep lifecycle ownership. No new
  automatic invitation, consent bypass or parallel proposal writer.
- The validator endpoint reuses signed Social→9001 matching owner/task accounting.
  Every returned completion (including truncation/retry) records provider usage;
  accounting failure aborts validation rather than triggering another paid call.
  It uses a durable local outbox (0.5s SQLite busy wait per operation) and the
  existing settlement worker, not synchronous remote ledger replay in validation.
  Existing callers keep immediate delivery/default timeouts. Matching quota rules
  and unsigned-background policy are unchanged.

## Relation policy

| Relation | v1 disposition |
| --- | --- |
| equivalent | ACCEPT AS RELATED_INTEREST |
| candidate_more_specific | ACCEPT AS RELATED_INTEREST |
| sibling_related | ACCEPT AS RELATED_INTEREST |
| role_mismatch | ACCEPT AS RELATED_INTEREST, not same-activity compatibility |
| candidate_more_broad / constraint_conflict / lexical_ambiguity / unrelated / unknown | REJECT |
| ERROR | REJECT; at most one identical retry, within the shared deadline |

Strict schema requires every transient batch ID exactly once, known relation enum,
no extra fields, no duplicate JSON keys, and finish_reason=stop. Invalid/truncated/
provider error receives at most one retry; valid NO/unknown/rejected relations do
not retry. Partial valid batches remain usable; failed Concept batches never expand
owners. If no accepted Concepts and any validator ERROR occurred, return typed
`semantic_validator_unavailable`, not a false claim of no candidates.

This guarantees policy enforcement given classifications, not perfect model
semantics. Known validator reliability/ambiguity risks remain pilot risks; no
production precision guarantee is inferred from unit tests.

## Evidence / truthful reasons

Internal packets preserve `basis_type=related_interest`, `query_preference`,
`candidate_preference`, `relation`, `semantic_score`, `validator_status`, policy,
canonical Concept key and compatible similarity. They are validated at Graph→Social,
qualification and Matchmaker boundaries and remain separate from shared preference.
`semantic_score` is the Neo4j ANN score, not raw cosine or a probability of shared
interest. The existing provisional .82 threshold is unchanged.

- Exact query evidence proves the candidate has that preference, not that the
  requester does. Only both users' verified canonical PREFERS prove shared preference.
- A saved requester PREFERS on the query key allows “你喜歡 A，對方喜歡 B”.
  Otherwise say “你這次想找對 A 有興趣的人；對方保存的興趣是 B”.
- Role mismatch explicitly describes different participation and does not promise
  the pair can do the same activity together. No inferred skills, schedules or consent.
- No “你們都喜歡 A” for semantic-related evidence. Exact reasoning remains unchanged.
- Read-time card/notification/history consumers re-render the validated role-bound
  packet, ignoring conflicting saved/model viewer prose. Secondary reason items
  remain explicitly related, not exact/shared.
- The post-consent shared chat opening uses the same fact-bound packet and real
  names, not a new unrestricted model rewrite. It preserves requester query versus
  saved preference and explicitly distinguishes participation roles. Other proposal
  types retain their existing personalized model opening.
- Full phrases are disclosed only when safe and fitting the existing 220-character
  reason envelope; long/private phrases use a neutral introduction, never a clipped
  qualifier. Full <=500-codepoint source is still retained internally for validation.
- Query intent is never sent to the durable PREFERS/AVOIDS writer.

## Bounds, flags and failures

Defaults remain:

```ini
MATCH_PREFERENCE_SEMANTIC_MODE=off
MATCH_PREFERENCE_SEMANTIC_EMBEDDING_SPACE_CONFIRMED=off
MATCH_RELATED_INTEREST_ENABLED=off
```

The flags must be enabled in both Social and 9001, including active mode;
Social additionally requires the embedding-space confirmation. Missing/invalid
canary configuration, non-canary requesters, or a kill switch keep live searches
exact-only; exact results do not require these flags. A legacy active flag cannot
route live searches to the unvalidated old ANN endpoint. Flag checks occur again
before owner expansion and proposal commit. Already-created proposals retain the
existing consent/lifecycle, rather than being silently cancelled by a flag change.

For an immediate operator stop without restarting processes, create
`Server/.related-interest-disabled` (gitignored). Both services check this shared
file at work boundaries and before committing a new semantic proposal; the
validator also checks before every attempt. A currently running provider call may
finish within its bounded timeout, but cannot authorize a new proposal after stop.
`MATCH_RELATED_INTEREST_KILL_SWITCH_FILE` can bind an operator-owned shared path
when services use distinct filesystem roots. Removing the file does not turn on
any OFF environment flag. There is no client-controlled flag-changing endpoint.

ANN default/hard max: 24/32 neighbors; 8/12 Concepts; 10/20 owners per Concept;
40/50 candidate IDs; 3/5 evidence records; hydrated pool <=20. Canary membership
is applied before the per-Concept LIMIT, which remains before exclusions/grouping;
only two indexed User.id lookups are eligible. No full graph scan or refill. Deterministic
dedupe/order: score descending, Concept key and user ID ties. Exact wins first.
Only zero qualified exacts permits fallback, regardless of an old threshold override.

The new path has an explicit 18-second shared validator budget, <=6 seconds per
attempt, <=2 attempts per batch; no SDK retries. The async provider transport is
cancelled on wall timeout, not left running in a detached sync thread. Social allows 27 seconds per
Graph/validator request and 38 seconds total including optional query embedding.
Each HTTP request carries its remaining budget (with a response margin). Server
Graph operations and validator retries spend that same budget; Graph preparation
does not grant a fresh 18 seconds when less caller time remains. Related Matchmaker
batches likewise carry the remaining shared selection budget to the existing
async endpoint timeout. Exact/OFF requests omit the new optional budget field.
These are hard caps, not latency targets. OFF/exact budgets are unchanged. Removing
two generic model-authored friend-intro calls from the new related branch offsets
some added work but does not establish a production p95 claim. General users are
not automatically background-shadowed; the existing separate observation policy remains.

## Versioned embedding readiness and controlled preparation

Historical `Concept.embedding` fingerprint remains unknown. New runtime only uses
`embedding_v2`, `embedding_v2_fingerprint`, `embedding_v2_source_hash` and the
dedicated 768-d cosine `concept_embedding_v2_index`.
The existing ONLINE RANGE indexes on `Concept.key` and `User.id` are required;
the read path fails closed without either, rather than allowing canary owner
expansion to become a label scan. No missing schema is repaired automatically.

The fingerprint covers provider, configured request model identifier, task,
dimension, exact embedding-2 sentence-similarity prefix, full v2 semantic source
contract and L2 normalization. Provider immutable revision remains **unreported**;
do not claim that same dimension/current config proves historical compatibility.
Only newly generated, explicitly recorded request-contract provenance is accepted.
Every ANN hit must pass v2 identity/hash/vector checks before classification.

`scripts/prepare_related_interest_embeddings.py` is dry-run by default, requires
an explicit JSON key list (<=100, embedding batches <=20), and skips unverified
legacy/truncated source. `--apply --ack-versioned-embedding-write` is a separately
approved operation; `--create-index` is an explicit additional write. It only adds
versioned vector metadata under source-hash CAS, never rekeys/merges Concepts,
rewrites PREFERS/AVOIDS, reconstructs missing suffixes, or enables the feature.
Operator connection/key variables are supplied through environment, not Git.
Generated vectors are not printed or committed; provider failures are sanitized.
The existing Event/Concept embedding worker is unchanged. New or corrected
Concepts without a prepared compatible v2 vector remain exact-searchable but are
not semantic-retrievable until an explicitly approved preparation batch runs.
This first pilot does not silently introduce an automatic backfill worker.

`scripts/audit_related_interest_readiness.py` only reads metadata/counts via the
new internal readiness endpoint. Coverage/index/provenance findings are not an
activation authorization. The endpoint never creates an index or backfills data.

## Pilot telemetry

Bounded, idempotent `$set` telemetry is attached to the existing owner-bound
search job. It records exact-vs-fallback observations and bounded accepted/rejected/error,
attempt, retry, attempt-error and relation counts. Validated ANN hashes/scores
(at most 12, including rejected/error hits) are retained internally; no labels,
raw query/owner message, candidate IDs, provider response or Graph payload are
added. Writes have a 0.5s budget. Optional telemetry failure
does not roll back matching; reports may undercount during storage outages.

Semantic proposal metadata carries only policy/basis/relation tags. The existing
state history supplies sent invitation and accept/decline/expiry outcomes; a
declined draft is not counted as a declined sent invitation. The bounded read-only
`scripts/report_related_interest_pilot.py` reports counts, rates, latency, rendering
outcomes and a truncation marker. It requires an explicit cohort and supports an
activation timestamp. Read-only joins use existing IDs but never emit them.
It is not a public analytics endpoint. No new UI reason-feedback control is added
in this backend phase; future pilot ratings must be separate from explicit
rejection reasons that authorize AVOIDS writes.

## Verification / rollout plan

- Relation taxonomy/policy, strict output schema, one retry/deadline, no-Graph-writes,
  fingerprint mismatch, validation-before-expansion and dedupe regressions.
- OFF/P0 parity, exact aliases, qualified exact filtered to zero, block/history/
  hard conflict, no hybrid refill, expired context and mid-flight kill switch tests.
- Both viewer directions, requester intent vs durable fact, no false shared claim,
  tampered prose, private/long phrase handling, and count-only telemetry tests.
- Disposable local Neo4j 2026.08.1: actual ANN/PROFILE/indexed expansion/limits;
  deterministic vectors and stub classifier only, not real semantic quality scoring.

Deployment order after separate approval: review/merge backend → keep all flags
OFF while upgrading 9001 + Social together through `start_all.sh` → read-only
deployed readiness audit → review bounded preparation dry-run → separately approve
versioned vector/index writes → audit coverage/integrity/provenance → explicitly
approve internal environment activation. Set OFF to stop new semantic selection;
do not delete the versioned index or touch old identities during rollback.
DatingApp #42 remains a separate identity-v2 voice contract PR; no merge/deploy here.

## Implementation gate result — 2026-09-24

| Verification | Result |
| --- | --- |
| Matchmaker full offline suite | 220 passed; 15 subtests |
| Contracts full offline suite | 425 passed; 232 subtests |
| Social full offline suite | 1788 passed; 21 failed; 63 subtests |
| Clean main same-environment Social | 1768 passed; the same 21 failures |
| Branch-only Social failure set | Empty (exact testcase-set comparison) |
| Quota existing + new validator tests | 32 passed |
| Focused pilot / OFF parity / quota boundary | 67 passed |
| Compilation / diff whitespace / startup shell syntax | PASS |
| Secret-pattern scan / local env or artifacts in diff | No findings |
| Disposable Neo4j functional gate | PASS; stub validator + synthetic vectors only |

The 21 Social baseline failures are 19 existing authentication/Pi fixture failures
and two existing subprocess import failures (`dotenv` absent from the readiness
interpreter's standalone path). Neither is suppressed or fixed in this feature.
Suite commands use `scripts/run_offline_tests.py social`, `matchmaker`, `contracts`
with the service-compatible local dependency paths; quota uses
`pytest agent_quota/tests/test_quota.py tests/test_related_interest_quota.py`.

New tests cover all nine relation decisions, ERROR recovery/closed failure, strict
schema, wrong model/provenance, missing key index, validator-before-expansion,
PREFERS-only/dedupe, qualified-exact-zero trigger, kill before another call/write,
OFF and ON exact-result parity, factual reasons in both directions and shared chat,
full semantic source preparation, signed quota context and durable accounting.

Local baseline image: `neo4j:2026.08.1`, not inferred production version.
`concept_embedding_v2_index` ONLINE / 768 / cosine; Concept.key index ready.
Local aggregate: 3 PREFERS-connected Concepts, 3 complete v2, 3 compatible
synthetic vectors. PROFILE shows vector ProcedureCall then NodeUniqueIndexSeek;
no AllNodesScan/NodeByLabelScan, early per-Concept Limit, candidate cap/dedupe.
Representative warm fallback: 147.963 ms with a stub classifier. This is **not**
real DeepSeek latency, semantic precision, or a production p95 measurement.
The post-review rerun after restarting the same container also passed (503.810 ms
first invocation in that run, including Graph setup/cache effects).
The disposable container is stopped; only isolated synthetic volumes remain.

GitNexus final diff analysis includes new files using a temporary Git index:
The pre-architecture-review analysis reported 30 files / 140 changed symbols /
29 affected flows, **CRITICAL** shared-boundary
risk (reason projection and quota accounting). Existing quota defaults and exact
payload/proposal behavior have dedicated parity tests. Global index generation
still reports process/call-resolution caps; graph absence is not proof of no
interaction and this is not advertised as an exhaustive graph all-clear.
Final staged analysis: 31 files / 182 changed symbols / 28 affected indexed flows,
still CRITICAL. Dynamic HTTP endpoint/DTO callers are verified from actual
transport and serialization tests rather than treating UNKNOWN graph callers as safe.

Deployment environment Graph access was not performed. Historical fingerprint,
deployed version, real coverage/integrity remain unknown. All three runtime flags
remain OFF; no vectors/index were created outside the disposable Graph. No live
provider call, deployment, migration apply or P1-B work was performed.
Commit/push/PR preparation was subsequently authorized after architecture review;
that authorization does not allow merge, deployment or feature activation.

DatingApp #42: open/unmerged, GitHub mergeable, unchanged 3-file head `8ac67afa`.
Its existing Actions run had zero test steps because of the account billing/
spending-limit condition; it is **not** a CI PASS. Prior local 61-test/static/
catalog/Unicode evidence remains historical evidence, not a new run here.
Merging #42 triggers deployment/build workflows, so it is not operationally ready
under this no-deploy authorization. Verified deployed identity-v2 backend must
precede the frontend release. No frontend files were changed in this pilot.

## Final architecture review

Two deadline blockers were found and corrected before the implementation commit:

1. Social's elapsed budget was not carried to 9001. A late embedding handshake or
   later Matchmaker batch could start a fresh server budget after the caller had
   little time left. Optional bounded `request_budget_seconds` now carries the
   remaining time; Graph preparation, validator attempts and model selection do
   not reset it. Tiny/exhausted budgets start no new related HTTP/provider work.
2. A synchronous HTTP timeout bounded network operations, not the total attempt.
   Validator transport now uses `AsyncOpenAI` with an enclosing `asyncio.timeout`,
   cancellation and client closure. SDK retries stay zero; one application retry
   shares the original deadline. Tests exercise the actual async SDK transport
   with a local fake HTTP handler, not a real provider experiment.

Related Matchmaker completion accounting now uses the same durable deferred
outbox as the validator, so synchronous remote settlement cannot hold the async
model deadline open. Every returned completion, including a truncated attempt,
gets a distinct usage event; settlement replay is idempotent and never reruns an
LLM. Accounting storage failure stops processing, not another paid retry. Exact
Matchmaker callers retain their previous accounting path. Cancelled requests
without provider usage cannot supply a measured token count; that existing
provider-accounting limitation is not represented as zero provider consumption.

### Evidence propagation proof

| Boundary | Retained authority / behavior |
| --- | --- |
| ANN → validator | Verified complete v2 semantic text; only Q/C + transient positions to model |
| Validator → owner expansion | Accepted relation only; rejected/ERROR Concepts never expand |
| Social hydration/qualification | Revalidates packet key/Q/C/relation; verifies candidate owner stance; hard conflicts still exclude |
| Compact profile → 9001 DTO | Explicitly attaches internal packet after compacting profile; generic candidate dict preserves it |
| Graph enrichment → Matchmaker serializer | Copies candidate, adds graph context, validates allowlisted packet without clipping Q/C |
| Selection → proposal/CAS | Server evidence, not model-written explanation; CAS only updates lifecycle fields |
| Card / notification / accepted opening | Role-bound fact renderer; related packet cannot become shared preference or requester ownership |
| Public serializer | Existing allowlist emits controlled copy, not packet/Concept key/score/private graph payload |

`shared_persistent_preferences` is derived only from verified owner-key
intersection. Semantic evidence has no code path inserting into that set or into
the durable writer. Query provenance remains distinct from a verified requester
PREFERS. CAS/mutual-consent tests retain the entire packet through draft→pending
→accepted and verify that acceptance invokes no preference writer.

### Request amplification accounting

- Query embedding: one `get_embeddings` batch of one canonical full text. On
  cache miss there are at most two Graph HTTP calls; the first does no validator
  work. No outer HTTP retry/whole-pipeline restart is introduced.
- Existing Gemini key-pool 429 failover remains: each configured key at most once
  in that batch, all within the same <=10-second embedding budget; SDK retries
  are disabled for this bounded call. This is not a claim that key failover is
  limited to one retry; the **one-retry rule applies to validator/Matchmaker**.
- Validator: <=12 Concepts, batch2, at most6 batches ×2 attempts, shared <=18s and
  caller remaining budget. Valid rejected relations never retry. No wider ANN
  refill, alternate validator model, or fallback backend is attempted.
- Matchmaker: existing max-20 pool/small batches; only explicit no-candidate may
  advance to the next existing batch. One truncation retry inside the original
  async deadline, SDK retries0. Provider/timeout failure aborts selection. Related
  batches carry the original Social selection budget's remainder.
- Confirmation/accepted opening: no new validator, embedding or unrestricted
  reason-model call. Existing CAS/idempotency and explicit rejection-feedback
  authorization remain unchanged.

No new feature, semantic labels, policy, .82 threshold, frozen research result,
or production configuration was changed by this review. The main base was checked
against `legacy-origin/main@64bb7e7` before PR preparation.

## Two-account isolation gate — 2026-09-26

This patch is based on `main@87a860ad51bb87ec0dbcec33334feab049f5f107`.
It does not activate flags, deploy, contact production Graph, change the nine
relation outcomes, generate embeddings, bootstrap accounts or start P1-B.

### Server authority and rollout

- `matchmaker_agent/related_interest_canary.py` is the single cohort parser/gate.
  `MATCH_RELATED_INTEREST_CANARY_USER_IDS` is a JSON array of **exactly two distinct
  stable owner IDs**. Missing, malformed, duplicate, wildcard, email, whitespace
  or over-limit entries return an empty cohort. IDs are not inferred from names.
- Operator must inject the approved Sunny/Demo IDs into the environment of the
  official `start_all.sh`. Actual account identifiers are not fixtures or committed
  configuration. Startup exports the value (empty if absent) to all child
  processes; a service-specific dotenv cannot silently populate a missing cohort.
- Both services must have active semantic mode and related-interest enabled;
  Social still checks embedding-space confirmation. Non-cohort and disabled
  requests follow the existing exact-only path, even with global flags ON.
- The related-interest endpoint additionally requires the existing verified
  matching quota scope to name the same requester. Unsigned or mismatched body
  claims return 403, before Graph/provider work. This reuses the existing HMAC
  and owner/task accounting; exact/background endpoints and quota amounts are
  unchanged. No client-provided flag/cohort list grants authority.
- ANN Concept eligibility requires a PREFERS owner in the cohort. Accepted
  Concepts expand only the two indexed User IDs, before the existing per-Concept
  bound. Graph results, Social hydration, qualification and each related
  Matchmaker batch are checked again. Immediately before `insert_one`, the
  semantic pair is checked again; stop releases any reserved daily slot.
- Exact candidates outside the cohort remain eligible under existing rules.
  No profile `test_match_cohort` is changed or repurposed. Block/history/hard
  conflicts/consent/proposal dedupe still apply normally.
- Kill file takes precedence at work boundaries, including validator attempts.
  An already in-flight bounded call can finish; it cannot authorize a new
  semantic proposal after the next stop check. This is not an atomic distributed
  cancellation guarantee for an already submitted database write.
- Merge/deploy/activation are separate approvals. Upgrade Social/9001 together
  with all flags OFF. For future authorized activation, inject the same two-ID
  cohort, recheck v2 index/fingerprint and signed-owner routing, then enable only
  that cohort. Immediate rollback: shared kill file or flags OFF; leave Graph
  identity, vectors, account data and already-created consent lifecycle intact.

### Verification evidence

| Suite (same interpreter/dependencies, external I/O disabled) | Branch | Clean main |
| --- | --- | --- |
| Social full | 1821 passed / 36 failed | 1809 passed / same 36 failed |
| Matchmaker full (final rerun) | 233 passed | 220 passed |
| Contracts full | 596 passed | 572 passed |
| Risk full | 231 passed / 5 failed | 231 passed / same 5 failed |
| Branch-only failure set | **0** | comparator |

The Social baseline contains existing Appwrite/auth/Pi fixture failures. Risk
contains the same five `google.genai` ImportErrors. No baseline is suppressed,
fixed, xfailed or represented as a full CI PASS by this patch.

Commands: `python scripts/run_offline_tests.py social|matchmaker|contracts|risk`
with separate service-compatible interpreters. Focused coverage includes all
eight requested isolation/parity cases, signed owner/body mismatch, outside
owner injection, missing index, mid-flight cohort/kill changes, safe quota
release, exact-outside-cohort proposal parity and preserved truthful reasons.
Existing frozen OFF differential and confirmation/mutual-consent tests are retained.

**50ms testcase classification: confirmed timing-sensitive baseline/test issue.**
`test_real_async_validator_transport_is_cancelled_closed_and_has_no_sdk_retry`
was run five times per checkout, alternating main/branch, with the same interpreter
and offline runner. Both failed 5/5 in cold focused processes: expected one mock
transport attempt, actual zero (`assert (0 == 1)`); timeout itself occurred and
the separate elapsed <0.5s assertion passed. JUnit testcase elapsed (not provider
latency): main 0.173–0.221s, branch 0.218–0.229s. Both test-body and `_completion`
source hashes match exactly. In the full-suite order, both pass. The assumption
that transport necessarily starts inside 50ms is initialization/order-sensitive;
this patch changes neither that testcase, its timeout, SDK nor retry/deadline code.
Raw comparison logs/XML remain ignored, not Git artifacts.

Disposable Neo4j `2026.08.1` (localhost only, not a production version claim):
two actual ANN/retrieval runs PASS, vector index ONLINE/768/cosine. Among 25
synthetic PREFERS owners, only the configured counterpart is returned; two
accepted Concept hits occupy one slot, 24 outside owners never enter the pool,
AVOIDS-only is not positive evidence, and retrieval leaves graph digest unchanged.
PROFILE: vector ProcedureCall + bounded Top; expansion uses Concept/User
NodeUniqueIndexSeek, two-ID Unwind, per-Concept Limit and candidate Top. No
AllNodesScan/NodeByLabelScan. Measured synthetic/stub-validator fallback 1782.035ms
first run and 91.461ms warm repeat, not provider/p95 performance. Test container
stopped afterward; isolated synthetic volumes and ignored reports retained.

Generated vectors, provider reports, credentials, local env and query-plan dumps
are not submitted. Production settings and primary dirty Server checkout are untouched.

GitNexus is bound to the isolated `two-account-semantic-canary-server` checkout,
base `87a860a`, re-indexed with the intended diff. Pre-commit detect_changes reports
19 files / 29 indexed symbols / 692 affected flows, **CRITICAL**. Many listed flows
are expanded from shared `run` symbols; this is not evidence that hundreds of
runtime paths were edited. Initial upstream queries for dynamic pipeline/HTTP
entries returned UNKNOWN, not proof of no callers. Manual source/transport review
and the same-main differential tests supplement the index. No CRITICAL warning is
waived as a clean graph result; relation, identity, embedding, provider retry,
quota amounts and lifecycle code remain outside this patch's changes.

## Phase 2 — bounded 5–10-owner internal pilot preparation

The two-account canary was accepted by the operator as PASS. Phase 2 is a product
usage pilot, not a new semantic experiment or a reinterpretation of the frozen
strict-validator NO-GO. No relation policy, .82 threshold, prompt, generation
budget, embedding pipeline, quota, identity or consent rule changes in this patch.
The historical two-ID restriction above is superseded only by the bounded parser:
2–10 distinct server-owned IDs; two remains valid for rollback to the old cohort.
Actual Phase 2 activation targets 5–10, and never infers membership from a nickname
or a client flag. All existing requester/owner-expansion/qualification/proposal
guards use that same parser. Normal exact matches are not cohort-filtered.

### Activation prerequisites and ordering

1. Read-only select 3–8 additional accounts; retain the original two. A Graph list
   is evidence of past associations, **not** fresh owner consent. Record each
   owner's full PREFERS and AVOIDS confirmation (including explicitly empty
   AVOIDS), then generate a fresh complete-set preview and obtain approval of its
   exact owner-scoped retirement list. Do not silently split compound concepts,
   turn residence facts into likes, infer polarity, or repair legacy suffixes.
2. Use the production-tested bootstrap path unchanged (combined cap 10). Verify
   owner revision, Graph edges, Mongo projection/audit, v2-clean state and zero
   duplicate profiles. Stale or unexpected state stops that owner; systemic
   integrity failure stops the batch. Do not roll back valid real-owner commits.
3. Freeze an incremental manifest only for newly verified full semantic sources.
   Reuse existing compatible vectors; generate only missing ones. Validate all
   vectors before source-hash/fingerprint CAS writes. No silent overwrite, legacy
   vector reuse, old-index change or unapproved bulk preparation.
   Frozen canary request-contract fingerprint:
   `89cc12c59af783aa2953d856c15ba34625e2db129cf5d8bf5712e53af017b640`.
   Frozen provenance fingerprint:
   `53a1d22fd63bcde9be0fa632df532929a05dcb5aa60ded6879b47ad0c2c1c8af`.
   Preserve the recorded Gemini model/version metadata, exact prefix, source hash,
   768 dimensions and L2 normalization. Reuse `concept_embedding_v2_index`.
4. Deploy a clean release to Social and 9001 together with flags OFF, preserve the
   primary dirty Server checkout, verify health/index/fingerprints and all cohort
   members, and test that both services can observe the same writable kill file.
   Inject the approved stable-ID JSON through `start_all.sh`; it is not committed.
5. Only then activate the approved cohort. Record activation timestamp and cohort
   revision in a private operator manifest. Start the agreed observation period
   from activation, not from preparatory data reads. Keep outsiders exact-only.

### Observability and stop semantics

Telemetry version 2 observes explicit-preference jobs only while the server pilot
is active for that requester. Exact qualified count remains in existing bounded
diagnostics. `exact_only_jobs` means observed jobs without fallback, **not** proof
of successful matching. Graph errors before qualification remain errors.

- ANN observations: validated v2 hash keys and rounded Neo4j scores, maximum 12.
  These are internal search-job observations, not public cards or raw cosine.
  Network/deadline failure can prevent receiving any observations; missing is not
  evidence that ANN was never executed.
- Validator: attempts, actual second attempts, per-attempt errors, per-Concept
  final ERROR and relation distribution. A valid rejection is never retried.
  Retry/error rates use attempts; unavailable rate uses fallback jobs. Legacy
  telemetry without version 2 yields unknown retry rates, not invented zeros.
- Proposals and sent-invitation outcomes use existing lifecycle state history.
  A declined draft is not a rejected invitation. Reason counters count the two
  viewer renderings separately; opening counters count saved shared openings.
  Neutral privacy/length fallback is distinct from fact-bound copy; no wording
  or extra model call is introduced. These are render outcomes, not UI impressions.
- Latency p50/p95 uses completed minus started job timestamps; it is end-to-end,
  not individual provider latency. Missing samples and report truncation must be
  disclosed. Optional telemetry failure may undercount; no new public endpoint.

Read-only report (credentials remain in the existing secret-loading mechanism):

```bash
python scripts/report_related_interest_pilot.py \
  --cohort-file /absolute/private/pilot-cohort.json \
  --since ACTIVATION_UNIX_TIMESTAMP --limit 1000
```

The report reads bounded policy-tagged rows, counts any outside-cohort semantic
activity instead of hiding it, and emits only aggregates. Use the frozen cohort
for that period; if membership changes, start a new period. A truncated or
unavailable report is incomplete, never an integrity PASS.

Automatic systemic-provider stop: after terminal job persistence, three consecutive
version-2 fallback jobs in the cohort within 15 minutes with typed
`semantic_validator_unavailable` / `semantic_retrieval_timeout` failure touch the
existing shared kill file. A successful or normally rejected fallback breaks the
streak. The query is limited to three jobs with a 0.5s Mongo budget, adds no model
request/retry, and never resets the matching deadline. The original failed job
stays fail-closed if monitoring storage/filesystem is unavailable; safe error-type
logging reports that the operator check is required. This is a conservative
operational STOP definition, not a new model reliability SLA.

Leakage, false shared-preference claims, accepted constraint conflicts, duplicate
or corrupt preferences, and human-reported unsafe reasoning require immediate
operator kill/STOP and investigation. Existing guards prevent disallowed enum
relations/cohort writes; they cannot prove a model classification semantically
correct. Read-only integrity checks and member feedback are still required.
Do not describe the telemetry monitor as a complete semantic safety detector.

No frontend feedback widget is added in this phase. Members can report a proposal
reference plus thumbs-up/down through the test coordinator; store this feedback
separately from chat and explicit rejection reasons. Never write pilot ratings to
PREFERS/AVOIDS or use an AVOIDS-generating rejection control as a rating widget.
Schedule the product's native monitoring mechanism only after actual activation;
remain quiet for unchanged state, stop/notify on a safety finding, and report at
the agreed end time. No additional users, vector generation, or P1-B are implicit.

Preparation status: isolated source/tests only; no production bootstrap, vector
write, cohort expansion or activation has been performed by this patch. Owner
confirmation and fresh retirement previews remain blocking rollout prerequisites.

### Phase 2 verification (pre-activation)

| Gate | Isolated Phase 2 | Clean main `b10f538` |
| --- | --- | --- |
| Social full offline | 1852 passed / 34 failed; 63 subtests | 1823 passed / same 34 failed; 63 subtests |
| Same failure testcase / first-line signature | 34/34 identical; branch-only 0 | comparator |
| Matchmaker full offline | 236 passed; 15 subtests | existing canary baseline |
| Contracts full offline | 602 passed; 232 subtests | existing canary baseline |
| Disposable Neo4j 2 / 5 / 10-member isolation | PASS; 1 / 4 / 9 distinct candidates | synthetic only |

The initial Social comparison exposed environment differences, not changes to Pi:
the release checkout has a deployment kill file and pinned Pi Node dependencies.
Use a synthetic process-only kill-file path for both test runs, and install the
unchanged Pi lockfile via `npm ci --ignore-scripts` in the isolated checkout. No
production kill file, service environment, runtime assertion or timeout was changed.

Local Neo4j uses synthetic vectors and a stub validator. Index ONLINE/768/cosine;
cohort outsiders excluded (24/21/16 among 25 PREFERS fixture owners), multi-Concept
candidate dedupe and graph read-only digest checks pass. PROFILE contains vector
ProcedureCall, indexed Concept seek, early per-Concept Limit and final Top. No
AllNodesScan or NodeByLabelScan; the local cost planner **does include NodeIndexScan**
for indexed owner lookup, so this is not a claim of all-Seek physical plans or
large-corpus scalability. Recheck the deployed planner before expanded activation.
Observed stub-only fallback times: 705.272 / 17.207 / 16.573 ms for 2 / 5 / 10
members (first-run cache effects, not provider latency or a p95 comparison).
The disposable container is stopped; ignored synthetic volumes/reports retained.

All real participant lists, confirmation worksheets, local configuration, generated
reports and provider/Graph artifacts remain ignored and uncommitted. There are no
production identity/edge/vector mutations, flag changes or feedback-to-memory writes.
