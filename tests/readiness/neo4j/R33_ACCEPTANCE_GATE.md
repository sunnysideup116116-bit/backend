# R3.3 prospective production-facing acceptance gate — v1

Frozen before creation of any new R3.3 fixture and before any provider scoring.
Authority: user-approved gate and explicit permission for agent-authored labels,
with disclosure that labels have NOT been independently reviewed by a human.
This document and `r33_gate.json` are a readiness experiment contract, not runtime
configuration or production activation. Earlier R3.2c/development STOP results
remain historical; do not retroactively re-grade them using this gate.

## Blocking gate (all required)

| Metric | Bound |
|---|---:|
| Acceptance precision | >=99% |
| Acceptance recall | >=95% |
| Accept/reject repeated-run consistency | >=99% |
| Broad candidate → specific query false accept | 0 |
| Role mismatch false accept | 0 |
| Constraint conflict false accept | 0 |
| First-pass schema validity | >=99% |
| Eventual validity after at most one permitted retry | >=99% |
| ERROR fail-open | 0 |

No threshold rounding, excluded failures or post-score metric/label changes.
An undefined precision (no accepted examples) is FAIL, not vacuous PASS.
A missing/incomplete experiment cannot PASS.

## Metric definitions frozen now

- 192 directional cases, each run in three fixed rounds; 576 observations.
  Reverse Q/C are separate cases but statistically dependent.
- Precision/recall pool eventual acceptance over all 576 observations, including
  ERROR fail-closed results. YES accepts; NO/ABSTAIN/ERROR reject. A positive
  ERROR is a false reject/FN. A negative ERROR is a rejected negative for binary
  policy metrics but remains explicitly ERROR in separate schema/confusion data.
- Consistency denominator is all 192 cases: all three eventual binary decisions
  must match. ERROR maps reject, as approved. Separately report the proportion
  with three valid outputs; an always-ERROR case is not semantic correctness.
- First/eventual validity denominator is all 288 scheduled two-case batches.
  A malformed batch invalidates both cases; no partial salvage. Case coverage
  and failed-batch counts are also reported.
- Directional safety: gold candidate_more_broad (C omits Q conditions) must never
  accept. Role safety uses gold role_mismatch, including action/consumption.
  Constraint safety uses gold constraint_conflict, including long suffix cases.
- ERROR fail-open independently checks that invalid schema/API/parser results
  yield no accepted case; nonempty salvaged decisions invalidate evidence.
- Per-round/category/language statistics and unique false-case lists accompany
  pooled metrics, but are not additional blocking gates.
- Relation-class accuracy/consistency, three-way primary consistency,
  NO↔ABSTAIN disagreement and secondary relation accuracy are diagnostic only.
- Gate computation uses exact integer/rational comparisons, not rounded display
  percentages. These are sample engineering gates, not confidence bounds or SLA.

## Frozen candidate config and acceptance policy

Reuse, without editing, the relation-only v3 prompt, strict relation parser,
deterministic mapping and batch evaluator/retry predicate from R3.2c.
Exact source hashes are in `r33_gate.json`.

- Provider Ollama Cloud; model deepseek-v4.1-flash:cloud.
- Default reasoning (omit effort field), temperature 0, batch 2.
- max_tokens 4096, timeout 60 seconds, SDK automatic retries disabled.
- At most one identical retry for the existing eligible malformed/empty/
  truncated or transient provider ERROR predicate. No valid NO/ABSTAIN retry.
  Existing nonretryable auth/parser/unsupported-finish behavior is not relaxed.
- OpenAI SDK 1.30.1, HTTPX 0.28.1; three workers.
- Rounds/seeds 330301, 330302, 330303; cross-round schedule seed 330300.
- 288 scheduled first requests, hard maximum 576 HTTP attempts.
- Mapping: equivalent/candidate_more_specific→YES;
  candidate_more_broad/sibling_related/role_mismatch/constraint_conflict/unrelated→NO;
  lexical_ambiguity/unknown→ABSTAIN. No model acceptance field or secondary
  rationale may override code. Invalid schema→ERROR→reject.
- No new structured-output parameter, prompt sweep, budget change, or result
  dependent regrouping. Keep repeated calls even if early semantic results fail;
  preserve the complete scheduled evaluation, then STOP on any blocking failure.
- Provider revision is unpinned/unknown unless actually exposed. Record model
  equality and SDK versions; do not infer a provider revision.

## Holdout design and input fidelity

Create 96 new synthetic concept pairs / 192 directional cases after this gate
commit, with no duplicate directional canonical pair from the 96-case development
set or R2.5. Check both pure and fresh v2 canonical forms, both directions.
Also manually inspect topic/pair reuse; a deterministic non-overlap check cannot
prove that every concept is unrelated to every development example.

Coverage: equivalent/paraphrase, broad/narrow both directions, siblings, roles,
action/consumption, constraints/opposites, ambiguity, unrelated, composite
conditions, multilingual/script variations and long suffix challenges.
Target 48 observations per language direction (EN–EN/ZH–ZH/EN–ZH/ZH–EN).
At least 16 pairs must have identical first 40 code points and materially
different suffixes after fresh normalization. Distinct full normal forms must
produce distinct v2 keys. Script variations must use genuine paraphrases, not
deterministic alias-only pairs disguised as semantic cases.

Fixture fields include source descriptions, language/category, directional
gold relation/primary and pre-score rationale. Input labels are bounded to 500
Unicode code points (backend v2 contract); overflow rejects, never truncates.
Before scoring, compute fresh deterministic full semantic_text using pinned
OpenCC 1.4.1/s2twp and v2 canonicalization. Freeze both raw sources and exact
provider Q/C, with original gold unchanged. Never use display_label as identity
or model input. Only ID/Q/C are sent to the model, never gold labels/rationales.

Labels are authored and reviewed against the approved directional contract by
the assistant before scoring, under explicit user authorization; **not independently
human reviewed**. The evaluated provider never labels the set. Dataset, expanded
inputs, gate, prompt, parser, retry implementation, mapping, schedule and runner
hashes must be frozen before any provider request. No scores may influence labels.

## Execution and integrity

Use an isolated readiness branch; no production modules with startup side effects.
Only allowlisted existing LLM credential/model fields may enter process memory.
Do not load whole .env; no credential values/fragments/raw exceptions/headers/
reasoning text in artifacts. Host/path allowlist: Ollama HTTPS chat/completions.
No redirects, Graph calls, embedding generation or candidate/proposal writes.

Freeze commits in chronological order: prior development evidence; this gate;
new fixture/tooling/labels. A prescoring manifest binds those commits/hashes.
Generated JSON/provider outputs remain gitignored. A stopped/partial run may not
be silently overwritten or restarted as a fresh “better” sample. No extra retries.

PASS only permits relation-validator runtime integration DESIGN; it never enables
production. Any blocking FAIL means STOP, no production implementation.
Both semantic flags stay OFF; .82 unchanged; production embedding fingerprint,
Neo4j version and coverage/integrity remain unknown. No deploy, migration,
backfill, production Graph access, P1-B, or DatingApp #42 merge.
