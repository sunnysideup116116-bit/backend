# P0.1 Preference Identity v2

Status: implementation in `codex/preference-identity-v2`, pending review. No production migration, re-key, Graph scan, backfill, deploy or semantic activation was performed. R3.3 and P1-B remain stopped. Historical production embedding fingerprint remains unknown.

Current disposition (2026-09-23): **both approved blockers resolved; no new branch-only regression, ready for commit review, not committed**. The historical RED language-parity test now passes through a pinned fresh-input boundary. The independent DatingApp worktree now sends up to 500 Unicode codepoints intact. Historical review snapshots below are retained as evidence, not current blockers. Production remains STOP.

## Authoritative text contract

`matchmaker_agent/concept_identity.py` owns the contract shared by Social and 9001:

Fresh text has one preceding boundary: `normalize_fresh_preference_text` / `canonicalize_fresh_concept`, with **OpenCC==1.4.1, config `s2twp`**, policy `opencc-1.4.1-s2twp-nfkc-whitespace-v1`. It checks raw bounds, NFKC/whitespace, fixed conversion and converted bounds before the existing alias/digest stage. Missing or mismatched dependency fails closed; there is no UI fallback. Both service requirements pin the dependency; `start_all.sh` preflights it before log creation, port cleanup or service launch. Matchmaker requirements preserve their original UTF-16/CRLF encoding; the only dependency delta is the OpenCC pin.

`canonicalize_concept` remains the pure frozen v2 key function. `stored_concept_identity`, legacy verification, embedding reads and persisted request replay do **not** invoke fresh conversion. Existing v2 Simplified text therefore keeps its original identity. No converter availability can silently change a stored key.

| Field | Meaning |
| --- | --- |
| `semantic_text` | Complete bounded NFKC/whitespace-normalized concept, retaining constraints and roles. The identity and embedding source. |
| `display_label` | Explicit presentation-only abbreviation, at most 40 characters including ellipsis. Never used to reconstruct identity. |
| `canonical_key` / compatible `key` | `v2_` plus the first 48 hex characters of SHA-256 over `preference:v2\0` plus the complete semantic normal form. |
| `canonicalization_version` | New records: `v2`. Missing historical version: `legacy_unknown`, not implicitly v1 or v2. |
| `semantic_input_hash` | Full SHA-256 of the exact normalized semantic text supplied for embedding; distinct from the case/separator-normalized identity digest. |
| `fidelity_status` | `complete` only for verified v2 source. |

The compatible `label` field contains full semantic text, not `display_label`. Stored v2 readers verify all supplied key aliases, source, version, hash and fidelity. Changing a display label cannot change identity. Models cannot choose keys.

The key is 51 ASCII characters with 192 bits of digest, fitting the existing `^[a-z][a-z0-9_]{1,50}$` contract. Only the digest representation is shortened; its input is not. Generic case/space/hyphen/underscore normalization and the existing centralized deterministic alias registry remain. Kpop/K-pop/K pop/k-pop have one v2 identity. Punctuation that can distinguish meaning is preserved. No semantic synonym is an alias.

## Bounds and basis

| Input | Bound | Existing basis / behavior |
| --- | --- | --- |
| One semantic concept | 500 characters | Existing `ai_service.get_embeddings` input envelope was 500; replace its prefix slicing with whole-batch validation. This is an application resource bound, **not** a claimed Neo4j/Mongo maximum. |
| Profile extraction source | 800 characters | Existing extraction envelope; oversize messages do not create partial memories. |
| Evidence span | 160 characters | Existing typed extraction contract; overflow rejects instead of truncating. |
| Registration interest / owner lexical query | 120 characters each | Existing profile/API envelope, validated before/after normalization. |
| Durable candidates | Default 6, hard max 8 | Existing `DURABLE_MEMORY_MAX_CANDIDATES_PER_MESSAGE`; overflow, including atomic expansion, fails the operation. |
| Concept embedding batch | 20 | Existing worker/provider bound; an invalid member rejects the entire batch before provider or write. |

Validation checks both raw and normalized length, type, Unicode and control characters. NFKC expansion cannot silently overflow. No DB schema with a smaller concept-string limit was found in repository code; actual 500-character payloads and 51-character keys are exercised locally. Production DB/version compatibility was not inspected.

## Flow and atomicity

```text
owner assertion / registration / explicit correction
  → pinned fresh-input normalization (not a display transform)
  → typed validation, source/evidence/atomic-count bounds
  → deterministic complete-source v2 identity
  → /api/v2/memory/apply or /api/v2/memory/action
  → transaction: validate existing identity, then marker + owner edges
  → verified Mongo preview/facts as appropriate
  → complete semantic text → embedding worker → verified vector projection
```

Ordinary extracted active memory is held in Graph relations; Mongo preview is a read cache. Explicit match feedback additionally projects validated successful API results into `preference_facts`. Do not assume every ordinary memory has an independent original-source Mongo fact.

New Social uses versioned mutation URLs (`/api/v2/memory/apply`, `/api/v2/memory/action`, `/api/v2/feedback`, `/api/v2/concepts/embeddings/project`). An old 9001 returns unavailable/404 instead of silently accepting and truncating new payloads. Existing route aliases on new 9001 share strict handlers; they do not bypass validation. Optional feedback failure does not undo an already committed decline.

Exact and semantic retrieval distinguish raw topic-only requests from verified internal packets. A raw topic uses the fresh boundary. Internal Social adapters send `canonical_key`, `canonicalization_version`, `semantic_text`, `semantic_input_hash`; 9001 requires the complete matching packet and topic, verifies it without conversion, and rejects partial/mismatched packets before Graph access. This preserves existing bound v2 queries and is not permission to trust model-supplied keys.

Writer preflight covers the whole operation before effects. Graph transactions revalidate existing targets; mismatched source/hash or vector projection counts roll back. Embedding projection never overwrites Concept label/source from an old 60-character projection. The legacy context writer cannot mutate or occupy the reserved v2 key namespace. Event traversal/relevance algorithms are unchanged.

Correction of a verified v2 owner edge is explicit owner mutation, not migration. Same-identity correction is a no-op. Correction of unverified legacy identity requires reconfirmation and does not relocate edges.

## Exact read and legacy policy

```text
explicit preference → complete source → v2 key
  → indexed v2 Concept + source verification → bounded PREFERS users
  → optional indexed legacy key lookup + verified owner-assertion proof
  → one shared max-100 ID pool, v2 first
  → existing block/history/cohort/profile filters + hard-conflict qualification
  → existing max-20 hydrated pool and Matchmaker / consent lifecycle
```

Legacy equality is **not** proven by a shared prefix, key, short label, dimension or current configuration. A read-only bridge requires owner-scoped `legacy_evidence_scope=owner_assertion`, `legacy_fidelity_status=complete`, full `legacy_semantic_text`, matching `legacy_semantic_input_hash`, and the matching historical key derivation. No writer in this change manufactures that proof or backfills it. Unknown legacy is excluded from confirmed shared/exact evidence; potentially contradictory cross-version evidence fails closed with `indeterminate_legacy_conflict`. Existing same-key negative checks remain conservative.

Owner-only memory display can still show stored legacy text as legacy text. This does not authorize an exact identity bridge, a public shared-preference claim, or inventing missing suffixes. Disable/restore remains owner-scoped with existing expiry semantics.

Persisted preference confirmations/jobs must contain verified v2 search metadata. Legacy or altered persisted input returns `preference_search_reconfirmation_required` before search execution/quota use. Already-committed proposal recovery remains before this guard. Fresh generic/activity input and recent-context expiry policy are unchanged.

The frozen v1 derivation is a read/test oracle only. `scripts/audit_canonical_preferences.py` deliberately retains v1 audit semantics; it does not turn an old alias audit into a v2 re-key proposal. No migration script was applied.

## Retained display/privacy boundaries

- Display abbreviation (40), preview summary (300), list/item caps and non-preference presentation limits remain bounded.
- Owner context consumes verified complete text, at most 8 items; inactive/other-owner/protected data remains excluded.
- Counterparty/public rationale privacy projections remain bounded. Verified source is checked before presentation; internal key/hash/proof does not enter public rationale.
- The R3 validator remains offline and unmodified. A regression executes its actual payload expression for all 96 frozen development cases and verifies complete Q/C. No new provider scoring or R3.3 was run.
- `MATCH_PREFERENCE_SEMANTIC_MODE=off`, `MATCH_PREFERENCE_SEMANTIC_EMBEDDING_SPACE_CONFIRMED=off`, and `.82` are unchanged.

## Verification and scope

New regression files cover the 24 frozen collision pairs; a frozen main P0 implementation is retained under `tests/fixtures/` solely as a differential oracle. Short-case pipeline differentials compare identity equivalence, ordered Matchmaker inputs, filters, qualification, proposal outcomes, confirmation and privacy, allowing only version/verified metadata representation differences. Unknown historical provenance is an intentional fail-closed change, not claimed parity.

`tests/readiness/neo4j/run_identity_v2_local.py` runs actual component functions/Cypher against the existing localhost-only synthetic readiness harness, never a configured production Graph. Local Neo4j baseline is `2026.08.1`, not evidence of production version. It checks indexed v2/legacy lookup plans, bounds, source immutability, whole-operation rollback, and unchanged legacy ownership. Generated state, reports, vectors and credentials stay ignored.

Affected source groups:

- Identity / Graph: `concept_identity.py`, `agent_api.py`, `registration_graph.py`, Matchmaker search sanitizer, historical audit import.
- Memory: profile contracts/skill/extractor, memory facade/outbox, preference projection, registration bootstrap and optional decline feedback.
- Search: Social/Pi search sanitizer, persisted confirmation/job guard, qualification and verified public rationale; no ranking rewrite.
- Embedding / owner context: provider input validation, worker metadata/cache, owner wording and the two context consumers that previously re-truncated it.
- Manual/voice adapters: request schema, server catalog and dispatch/confirmation adapters; no voice transport redesign.
- Tests: identity/collision oracle, Graph writer/read tests, Social parity/replay/ingress/embedding/context tests, voice tests and disposable local component harness.

## Rollout and rollback risks — no authorization to execute

### Previous implementation regression gate (2026-09-22)

This earlier snapshot predates the additional architecture-review cases below; its zero branch-only failure count does not override the newly discovered RED language-parity test.

Baseline: clean `main@62da19c79809d8a25f0aec2f1ed995c1eef78d43`, isolated checkout. Full offline suites use `scripts/run_offline_tests.py` to disable dotenv/provider/DB networking. The local environment uses the existing Matchmaker Python with Social test dependencies appended; no dependency or environment configuration was changed.

| Gate | Result |
| --- | --- |
| Pure identity | 47 passed, including all 24 frozen pairs and full 96-case R3 payload fidelity |
| Frozen short P0 differential | 61 passed (included in Social), ordered pipeline/confirmation/qualification/public rationale |
| Social | 1707 passed, 19 existing baseline failures, 63 subtests |
| Matchmaker | 154 passed, 1 existing clock-sensitive Event fixture failure, 15 subtests |
| Contracts | 142 passed |
| App Voice | 358 passed, 1 existing missing-sibling-generated-catalog fixture failure |
| Disposable Neo4j | 21 actual component calls passed, zero query errors; index ONLINE, 768 cosine; v2 and legacy plans use `NodeUniqueIndexSeek` + `Limit`, no full scan |
| Static | compileall, `git diff --check`, `bash -n start_all.sh` passed |

Failed testcase sets and categories exactly match clean main in the same environment: branch-only failures = 0. The Event fixture sets a one-hour interval relative to wall time, and after 23:00 crosses a date boundary absent from its evidence; this is not an Event runtime regression. The Voice subprocess expects a sibling DatingApp checkout, which neither isolated checkout has. These issues were not fixed in P0.1. Local PASS does not claim full Server/APK E2E or production readiness.

Reproduce pure tests with `python -m pytest -q tests/test_preference_identity_v2.py`; run each full suite with `python scripts/run_offline_tests.py social`, `matchmaker`, and `contracts` using the compatible test environment. The disposable component command is `python tests/readiness/neo4j/run_identity_v2_local.py --confirm-disposable-synthetic-reset` only after the existing readiness harness `up` verifies the owned localhost container. Stop it with `run_readiness.py stop`; volume deletion requires the separate explicit confirmation documented in its README. Generated reports/state/vectors are gitignored, and the last local container was stopped, retaining disposable data.

GitNexus full index + final diff: 67 files, 368 changed symbols, 68 affected indexed flows, CRITICAL due to the shared identity boundary. Global flow enumeration is bounded and some cross-language/dynamic callbacks are unresolved, so source review and component regressions supplement, not replace, the index. No runtime commit was created.

### Remaining risks

1. Coordinated Social + 9001 deployment is required; mixed versions fail closed. Existing unverified pending writes/search confirmations need owner reconfirmation, not automatic replay.
2. Existing legacy records remain unchanged and may lose exact candidate coverage because provenance is unknown. Audit/reconfirmation/migration requires separate approval; never guess a truncated suffix or merge nodes by prefix.
3. The separate DatingApp generated voice catalog and actual `memory.add` executor are now synchronized in `codex/preference-identity-v2-voice`, not the shared frontend checkout. They must be released with the supported backend contract; old APKs retain the 40-character write limitation.
4. Rolling back binaries after v2 writes is not inherently safe: old code can misread/relabel new data. Pause writes/workers and use a separately reviewed v2-aware rollback reader; never rewrite keys or edges as a rollback shortcut.
5. A model may itself omit an unexpressed qualifier during extraction. This patch preserves validated source through server boundaries; it does not prove arbitrary model extraction completeness.
6. Semantic production activation remains blocked by R3 quality STOP, unknown historical fingerprint, unknown production Neo4j compatibility and unverified production coverage/integrity. Local functional PASS is not production readiness.

## Historical architecture review (2026-09-22)

The two blockers documented here were subsequently explicitly approved for repair and are resolved in the next section. The original evidence labels and failing-test history have not been relabeled as an earlier pass.

### Single-source audit: central digest, one unresolved ingress discrepancy

All v2 key construction resolves to `canonicalize_concept` in `matchmaker_agent/concept_identity.py`: bounded full input → NFKC/whitespace → preference prefix/polarity policy → centralized deterministic aliases → case/separator normal form → versioned SHA-256. `stored_concept_identity` and `verified_legacy_identity` call the same function. There is no second runtime `v2_` digest implementation.

The following other hashes do **not** create durable v2 Concept identity: historical v1 oracle/audit, context-only `_concept_key` in Social/9001, registration observation/source IDs, search fingerprints, and legacy/Event embedding cache hashes. The context writer is barred from the v2 namespace. Mongo's fresh-write adapter calls the central function; its only current runtime caller verifies stored v2 metadata before projecting feedback. It is not a generic legacy-data upgrade API.

However, raw-input locale preprocessing is not uniform. `routers/system.add_profile_memory` and `profile_skills._validate_memory` apply `normalize_zh_tw` first; the new preference branch of `safe_search_context` does not. The old P0 Social sanitizer did apply that preprocessing. For the same raw input `安静的咖啡厅`, the manual writer creates `安靜的咖啡廳` / `v2_d06b6e6a174d7b035bc732b75b5974b236d97ab50c5b8861`, while the new query stays Simplified and has a different key. `test_identity_v2_language_parity.py` deliberately fails, without xfail, against actual functions with offline I/O. Thus “one digest factory” does **not** yet prove end-to-end normalization parity.

Minimum proposed follow-up (not implemented): restore and centralize the existing full-text locale adapter at **fresh Social input** boundaries, then invoke the common bounded canonicalizer. Validate raw and converted lengths; never run display conversion or re-key persisted v2 source during reads/replay. A stronger choice to include S2T inside the cross-service identity normal form requires an explicitly pinned conversion policy/dependency; the current optional OpenCC-versus-fallback behavior is unsuitable for silently defining identity across environments. Do not copy translation code into each service or add semantic synonyms.

### Digest and length verification

- 18 literal golden vectors fix expected keys for K-pop aliases, EN punctuation/separators/case, Traditional Chinese qualifiers, decomposed/composed accents, NFKC full-width characters and Japanese text. No expected key is calculated from the live normalizer during the test. Pure identity suite: 65 passed.
- New Social matrix: 33 cases (11 boundaries × 499/500/501), exercising real extractor, sanitizer/replay, manual API/confirmation, Mongo projection, exact adapter and embedding provider wrapper with synthetic I/O.
- New Graph matrix: 15 cases across writer/registration component/correction/exact/vector projection. All 500-character semantic paths keep the complete source at 499/500 and reject 501 before effects.
- The actual registration **form/API** contract is independently 120 characters. All three 499/500/501 form inputs are rejected; Graph registration component accepting a 500-character Concept is not permission to enlarge that form. Evidence spans remain independently bounded at 160. No API/schema limit was inflated to make this matrix green.
- Disposable local Graph: 33 actual component calls; added 499/500 persisted semantic/hash/label, indexed exact, vector projection and owner-correction roundtrips. Invalid 501 operations leave Graph snapshots unchanged. Runtime source hashes remain identical to the prior gate; local harness hash is recorded too. This is component integration, not a deployed Server/APK E2E claim.

### Legacy and lifecycle verification

Legacy lookup still starts from indexed v1 key, but no candidate is accepted until that specific owner's relation has complete source/hash proof deriving the requested v2 identity. A collided prefix with a different proven suffix is rejected. No proof is fabricated from a Concept label. Both legacy-PREFERS/v2-AVOIDS and legacy-AVOIDS/v2-PREFERS are tested in both owner/candidate directions, with otherwise-sufficient shared activity, and remain `indeterminate_legacy_conflict` / ineligible.

Persisted confirmations/jobs require verified v2 metadata before new search/quota effects. Already committed proposal recovery remains ahead of the new-input gate; it reconciles an existing lifecycle rather than rerunning retrieval. Original 61 short frozen-P0 differentials, deterministic aliases, mixed polarity, feedback cap, ordered exact input, payload privacy and consent tests remain. The new Simplified-Chinese failure is an additional P0 parity gap, not excused by those passing cases.

### Required writer/deploy ordering — design only

1. Pause preference writes and in-flight extraction/outbox/registration/feedback/correction/embedding jobs before first v2 write. Inventory all old 9001 processes and direct Graph writers, not merely the process currently on port 9001.
2. Stop/drain old Graph-writing 9001 instances and old Social embedding jobs. Old `project_concept_embeddings` can overwrite a v2 node's label/vector while leaving newer source metadata intact; old correction can move an owner edge to a v1 identity. New endpoint validation cannot protect the DB from a different old process with direct credentials. No claim of DB-level protection is made.
3. Upgrade protected 9001 writer/read APIs first, then the coherent Social bundle: extraction/facade/outbox, registration, feedback/manual/correction, embedding worker, search sanitizer/job executor/confirmation and qualification. Keep new writes paused until both sides are verified. New Social→old 9001 versioned mutations fail closed; this is not a supported rolling-write arrangement.
4. V2-aware read-only projections can be installed before writes, but legacy exact coverage may drop to unknown. Old matching/qualification/replay readers must not remain active after v2 writes. A purely display-only reader may remain only if it never re-hashes, writes, or asserts shared preference identity. Voice is **not** such a reader.
5. Resume only after separate deployment authorization and integration checks. `start_all.sh` remains the sole Server entry point, with unchanged ports; none of these deployment steps was executed.

Rollback after any v2 write is permitted only to a v2-aware protected writer/reader build. No released pre-P0.1 baseline is claimed safe for writes against v2 data. A pre-P0.1 binary rollback requires writes/workers to stay disabled and separate compatibility review; never rewrite keys/edges or relaunch old mutation workers as a shortcut.

### DatingApp write-path blocker and minimum proposed patch

Read-only trace in the separate repository: `lib/services/app_voice/app_voice_action_executor.dart` dispatches `memory.add` to `_addMemory`; it checks `label.length > 40` before calling `AyueV3ApiService.addProfileMemory`, whose API forwards the label to `/api/profile/memories/add`. The generated `lib/services/app_voice/app_voice_capabilities.dart` also contains the old 40-character argument schema. This is not presentation shortening.

The minimal separately authorized frontend patch is: regenerate the catalog from backend capabilities; replace the executor's hard-coded 40-character validation/message with the shared supported semantic bound; preserve the entire label without substring; keep UI display abbreviation separate; test 41/499/500 accepted, 501 rejected before API, and unchanged transmitted payload. Account for Dart UTF-16 length versus Python Unicode codepoints (do not make a second lossy normalizer); server validation remains authoritative after NFKC. Run the catalog consistency check and executor/API tests. No frontend file was modified in this review.

### Final architecture-review gate results (2026-09-22)

| Suite | Result | Branch-only failures |
| --- | --- | --- |
| Pure identity including 18 golden vectors | 65 passed | 0 |
| Social | 1743 passed, 63 subtests, 20 failed | **1: script-normalization parity**; other 19 match clean main |
| Matchmaker | 169 passed, 15 subtests, 1 failed | 0; same clock-sensitive Event fixture as clean main |
| Contracts | 160 passed | 0 |
| Added Social 499/500/501 matrix | 33 passed (included above) | 0 |
| Added Graph 499/500/501 matrix | 15 passed (included above) | 0 |
| Mixed legacy/v2 conflict directions | 4 passed (included above) | 0 |
| Actual local Neo4j transactions | 33 calls passed, zero query errors | local synthetic only |

The original 61 frozen short-P0 differential checks still pass, but did not cover the newly found Simplified-Chinese input case. Compileall, diff whitespace checks and `bash -n start_all.sh` pass. Sandbox TestClient/AnyIO hangs were terminated; completed full-suite results came from the unchanged offline runner with DNS/network protection outside that sandbox. No failed or aborted run is represented as a pass.

Final indexed impact snapshot: 69 files, 399 changed symbols, 68 affected flows, CRITICAL; two `canonicalize_concept` receiver call sites are unresolved and global traversal is bounded. Actual source/call-site and cross-service reviews remain necessary; the index cannot establish absence of dynamic or old deployed writers. No unrelated cleanup was removed because none was identified.

Readiness-only [PR #14](https://github.com/sunnysideup116116-bit/backend/pull/14) was merged using a merge commit, preserving four logical evidence commits. Main was unchanged at `62da19c` before merge; 32 files remain readiness-only. PR CI Social 19/Risk 5 failures exactly match clean-main testcase/category summaries; Matchmaker/Contracts pass and there were no requested changes. The explicitly authorized baseline exception is recorded in the PR and merge message. New remote/tracking main is `7b781b6a8645ea8473552db449a8f1876d641a14`. P0.1 remains uncommitted at base `62da19c`; main's intervening change is evidence-only, with no runtime interaction. The dirty primary Server checkout was not updated.

The recommendation at that historical gate was STOP. The subsequent approved fixes and final results below supersede it; registration, migration and semantic-activation boundaries remain unchanged.

## Approved blocker fixes and final gate (2026-09-23)

### Fresh versus stored contract

Extraction (including source/evidence handling), manual writes, new preference queries/confirmation, correction, registration and raw supported APIs share the fixed fresh converter. Social no longer uses optional `normalize_zh_tw` as the fresh preference identity policy. Existing display-only language conversion remains outside this contract. Deterministic K-pop aliases still run at the existing identity stage; no semantic synonym or automatic Graph alias is introduced.

Typed v2 writes/reads, Mongo cache/facts, 9001 apply/registration, internal exact/semantic packets and persisted confirmation replay validate complete source/key/hash and retain them unchanged. Wrong version/source/hash fails closed. The tests preserve a historical Simplified-v2 record even when the fresh converter is unavailable. A **new** Simplified query now canonicalizes to Traditional and will not retroactively match an old Simplified-v2 identity; explicit bound replay remains valid. That coverage limitation is intentional under the no-rekey/no-backfill requirement.

### Unicode length contract

The raw input bound is **Unicode codepoints**, before trimming; it is not bytes, UTF-16 code units, or grapheme clusters. Python uses `len` on valid Unicode; Dart uses `runes.length` and rejects unpaired surrogates. A supplementary emoji counts as one; an `e` plus combining accent counts as two before normalization. At 40/41/499/500 codepoints raw input is allowed; 501 is rejected without retaining a prefix, even if trimming/composition would shorten it.

The server separately requires normalized/converted semantic text to remain ≤500. Consequently raw text within the bound can still be rejected if NFKC or fixed S2T conversion expands it past 500. The client does not implement a second converter or promise that every raw ≤500 input will pass this additional semantic check. The registration form stays at its independent 120-codepoint bound.

### Separate DatingApp changes

Branch `codex/preference-identity-v2-voice` is based on clean frontend `98667fd`. Exactly three files changed: `lib/services/app_voice/app_voice_action_executor.dart`, regenerated `lib/services/app_voice/app_voice_capabilities.dart`, and `test/preference_identity_v2_voice_test.dart`. The generator source remains backend `capabilities.json` (500), the existing generator/check process is used, and the executor reads the generated bound instead of another hard-coded constant. It sends full raw text to the API; trim is only an empty-value check. Permission/confirmation behavior is unchanged. Main DatingApp and Server worktrees were not edited.

### Final results

| Gate | Result |
| --- | --- |
| Original language-parity RED | PASS |
| Pure identity + pinned fresh normalizer | 104 passed; includes 24 collisions, 18 fixed golden keys, missing/wrong converter, Unicode and persisted-v2 immutability |
| Social | 1770 passed, 63 subtests; 19 identical clean-main baseline failures |
| Matchmaker | 186 passed, 15 subtests |
| Contracts on updated main | 366 passed, 226 subtests |
| App Voice backend | 373 passed; 1 identical missing-sibling-catalog fixture failure |
| DatingApp | 61 passed (28 new, 33 existing executor); direct Dart analysis no issues; generator check passed |
| Disposable Neo4j | 37 actual component calls, zero query errors; fresh Simplified/Traditional equality and preserved historical Simplified-v2 snapshot |
| Static/hygiene | compileall, shell syntax, diff whitespace and intended-file secret/artifact scans passed |

New branch-only failures = **0**. Existing 61 frozen short-P0 differentials, OFF-mode parity, mixed polarity, feedback cap, exact ordering/payload, consent and expiry tests remain passing. Chinese/emoji/combining 40/41/499/500/501 cases pass at both client and backend gates. Flutter's analyzer wrapper encountered an LSP JSON initialization error; direct Dart analysis succeeded, without changing code to suppress diagnostics. The App Voice isolated-checkout catalog fixture failure is distinct from the explicit paired-checkout generator check, which passes.

The backend branch fast-forwarded to `main@7b781b6` without conflict or a new commit. Its 32 new main files are frozen readiness evidence only. Their historical truncation test now explicitly loads the SHA-pinned frozen v1 oracle under `tests/fixtures/`, preserving every original label/assertion; the new v2 tests validate repaired behavior. No R3 prompt, labels, report hashes or experiment were changed/re-scored. Startup tests now provide fake runtimes for the new dependency preflight, and separately assert dependency failure precedes ports/services; the real guard was not relaxed.

The local container was stopped, leaving only ignored synthetic data/report artifacts. No production Graph/provider calls, migration, deployment, backfill, R3.3, P1-B or feature activation occurred. `.82` and both semantic default-OFF flags remain unchanged. Recommend proceeding to commit only after user approval; backend and frontend changes remain uncommitted.

## Final intended file inventory

78 backend intended files; uncommitted. Generated reports, secrets, vectors, environments and provider outputs are excluded. Memory skill instructions are counted as required runtime. Separate frontend changes total 3 files.

| Category | Files |
| --- | ---: |
| Required runtime / pinned dependency | 19 |
| Compatibility adapter | 14 |
| Test / fixture | 37 |
| Tooling | 2 |
| Docs | 6 |

Required changes only: fixed fresh-input contract/pinned dependency, protected existing identity/Graph reads, canonical packet adapters, full-source transport, tests, local tooling and documentation. No unrelated cleanup or future P1 preparation was introduced. Historical readiness assertions use the frozen v1 oracle; new v2 tests remain independent.

### Required runtime / pinned dependency

- `matchmaker_agent/agent_api.py`
- `matchmaker_agent/concept_identity.py`
- `matchmaker_agent/registration_graph.py`
- `matchmaker_agent/requirements.txt`
- `skills/memory/SKILL.md`
- `social/routers/match.py`
- `social/services/ai_service.py`
- `social/services/concept_embedding_service.py`
- `social/services/match_action_service.py`
- `social/services/match_search_context.py`
- `social/services/match_search_job_service.py`
- `social/services/memory_outbox_service.py`
- `social/services/memory_service.py`
- `social/services/owner_memory_projection.py`
- `social/services/preference_store.py`
- `social/services/profile_contracts.py`
- `social/services/profile_skills.py`
- `social/services/registration_graph_service.py`
- `start_all.sh`

### Compatibility adapter

- `app_voice_assistant/capabilities.json`
- `app_voice_assistant/contracts.py`
- `app_voice_assistant/duplex_session.py`
- `app_voice_assistant/router.py`
- `app_voice_assistant/template_dispatcher.py`
- `matchmaker_agent/matchmaker.py`
- `social/models.py`
- `social/routers/system.py`
- `social/services/ayue_agent/context.py`
- `social/services/ayue_agent/pi/match_tools.py`
- `social/services/ayue_agent/shared/write_executors.py`
- `social/services/mediator_context_service.py`
- `social/services/preference_candidate_service.py`
- `social/services/preference_semantic_service.py`

### Test / fixture

- `app_voice_assistant/tests/test_preference_identity_v2.py`
- `matchmaker_agent/test_concept_identity.py`
- `matchmaker_agent/test_context_projection.py`
- `matchmaker_agent/test_decline_feedback.py`
- `matchmaker_agent/test_identity_v2_graph.py`
- `matchmaker_agent/test_memory_actions.py`
- `matchmaker_agent/test_preference_candidates.py`
- `matchmaker_agent/test_preference_search_context.py`
- `matchmaker_agent/test_preference_semantic_candidates.py`
- `matchmaker_agent/test_registration_graph.py`
- `social/tests/test_identity_v2_fresh_normalization.py`
- `social/tests/test_identity_v2_language_parity.py`
- `social/tests/test_identity_v2_length_e2e.py`
- `social/tests/test_identity_v2_memory_boundaries.py`
- `social/tests/test_identity_v2_parity.py`
- `social/tests/test_identity_v2_search_replay.py`
- `social/tests/test_match_qualification.py`
- `social/tests/test_memory_outbox_service.py`
- `social/tests/test_onboarding_decline_feedback.py`
- `social/tests/test_pi_feedback_regressions.py`
- `social/tests/test_preference_candidate_service.py`
- `social/tests/test_preference_identity_v2_boundaries.py`
- `social/tests/test_preference_identity_v2_context.py`
- `social/tests/test_preference_identity_v2_embedding.py`
- `social/tests/test_preference_match_search.py`
- `social/tests/test_preference_off_parity.py`
- `social/tests/test_preference_semantic_service.py`
- `social/tests/test_preference_store_canonicalization.py`
- `social/tests/test_profile_skills.py`
- `social/tests/test_registration_graph_service.py`
- `tests/fixtures/concept_identity_p0.py`
- `tests/fixtures/preference_identity_collision_pairs.json`
- `tests/readiness/neo4j/test_r32c_input_fidelity.py`
- `tests/test_preference_fresh_normalization.py`
- `tests/test_preference_identity_v2.py`
- `tests/test_start_all_lifecycle.py`
- `tests/test_start_all_provider.py`

### Tooling

- `scripts/audit_canonical_preferences.py`
- `tests/readiness/neo4j/run_identity_v2_local.py`

### Docs

- `AYUE_V3_ARCHITECTURE.md`
- `docs/MEMORY_CONTEXT_ENGINE_GUIDE.md`
- `docs/NEO4J_SCHEMA.md`
- `docs/PREFERENCE_IDENTITY_V2.md`
- `docs/REGISTRATION_GRAPH_BOOTSTRAP.md`
- `docs/architecture/09-runtime-interfaces.md`

### Separate DatingApp worktree

- `lib/services/app_voice/app_voice_action_executor.dart`
- `lib/services/app_voice/app_voice_capabilities.dart`
- `test/preference_identity_v2_voice_test.dart`
