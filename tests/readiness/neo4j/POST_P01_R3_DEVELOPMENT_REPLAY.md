# Post-P0.1 full-fidelity R3 development replay

Date: 2026-09-23. **Production STOP; no R3.3 holdout started.**
This is a new execution of the frozen R3.2c development experiment, not a rewrite
of its historical results or a final generalization evaluation.

## Merge and isolation

Backend PR [#15](https://github.com/sunnysideup116116-bit/backend/pull/15) was merged
by merge commit under the explicitly approved documented CI exception.

- Merge commit / main HEAD: `88b1787898aba8215e3bc056b45d32de30431be0`.
- Reviewed head: `bb9149ef020cdfd59ea07c7f7221d23f3c13486f`.
- Pre-merge main: `7b781b6a8645ea8473552db449a8f1876d641a14`, unchanged at gate.
- Frozen diff: 78 files, +5130/-406; no new branch-only failures.
- Latest Social 19 and Risk 5 failures matched clean main by testcase/category;
  Contracts passed. Retry/deadline exception evidence is in PR comment 5788472153.
- Isolated branch: `codex/preference-post-p01-r3-replay`, created from merged main.
- Checkout: `.worktrees/preference-post-p01-r3-replay-server`.
- Primary Server worktree and its uncommitted changes were untouched.
- DatingApp PR #42 remains open/unmerged; no client deployment was triggered.
- No server restart/deploy, production Graph connection, migration or backfill.

GitNexus review used the frozen backend checkout's matching source tree. The
existing shared-boundary diff remains CRITICAL (78 files, 72 indexed flows);
there was no new runtime edit. Direct frozen-source/hash and hermetic execution
checks supplement bounded/static index coverage.

## Fixed experiment, before scoring

Unmodified 96 directional cases from 48 synthetic pairs, including original
28 YES / 60 NO / 8 ABSTAIN labels. Frozen relation ontology projection retained.
No fixture relabeling, prompt tuning, semantic threshold change, or post-result
case removal.

- Prompt: relation-only `r32c_prompt_v3.txt`, unchanged.
- Mapping: equivalent/candidate_more_specific → YES; candidate_more_broad,
  sibling_related, role_mismatch, constraint_conflict, unrelated → NO;
  lexical_ambiguity/unknown → ABSTAIN. Schema/API failure remains ERROR.
- Acceptance: YES accepts; NO/ABSTAIN/ERROR reject. ERROR stays a separate
  reliability outcome; it is never relabeled as a model NO.
- Ollama Cloud `deepseek-v4.1-flash:cloud`; default reasoning (effort omitted),
  temperature 0, batch 2, max_tokens 4096, timeout 60s, SDK automatic retries 0.
- Three fixed rounds/seeds 9401/9402/9403; same cross-round schedule as R3.2c.
- 144 first requests, three workers, hard maximum 288 attempts.
- At most one identical retry for allowed schema/transient failures; valid
  mapped NO/ABSTAIN never retried. Actual calls: 145 (one retry).
- OpenAI SDK 1.30.1 / HTTPX 0.28.1 / python-dotenv 1.0.1; existing packages used
  read-only, no installation or changes to running service environments.
- Only allowlisted LLM_API_KEY/LLM_BASE_URL and LLM_MODEL_ID were read into memory.
  No entire .env loading, credential printing, or credential persistence.
- Request guard restricts calls to Ollama HTTPS chat/completions, disables
  redirects/environment proxy inheritance and enforces the attempt budget.
- Only synthetic ID/Q/C sent; no gold labels, user/profile/memory/candidate data.
- Both semantic flags OFF; threshold 0.82 unchanged. No embedding or Graph calls.

All 145 responses reported the requested model name. Pinned model revision and
reasoning-token breakdown were not exposed; do not infer them.

## Full-fidelity preflight and limitation

The post-P0.1 pure canonical identity and persisted-v2 verifier preserve all 192
Q/C occurrences byte-for-byte for these fixtures. Maximum source length is 46
Unicode code points; four occurrences exceed the old 40-character boundary.
Every scheduled provider payload was checked against these full semantic sources.
Display labels are not used in provider requests.

A stricter initial check expecting *fresh OpenCC conversion* to be byte-identical
to every original fixture failed before any provider call. Inspection found four
synthetic phrases (eight directional occurrences) with deterministic conversions:
登台說相聲→登臺說相聲; 修復古董家具→修復古董傢俱;
舞台表演→舞臺表演; 划獨木舟→劃獨木舟.
These differences are recorded in the preflight artifact; no fixture was edited
to hide them. Fresh identities also pass persisted-v2 verification.

For controlled comparison, provider Q/C stays the frozen original full text,
as required by the existing R3.2c experiment. Fresh-converted spelling variants
were **not** separately scored. This is a fixed-input development replay plus
identity/read fidelity check, not an end-to-end production fresh-write/ANN test.
R3.2c already used full raw inputs, so differences in model scores cannot be
attributed causally to P0.1. No automatic relation validator exists in runtime.

## Results

Denominators: validity is batches (144); semantic observations are 96×3=288;
repeated-run consistency is distinct cases (96). Repeats are not independent
holdout samples.

| Metric | Result |
|---|---:|
| First-pass validity | 143/144 = 99.3056% |
| Eventual validity after one permitted retry | 143/144 = 99.3056% |
| First / final case coverage | 286/288 / 286/288 |
| Final ERROR observations | 2, from one failed batch |
| Final accepted TP / FP / FN | 84 / 0 / 0 |
| Acceptance precision / recall | 100% / 100% |
| Final non-accepted observations | 204: 202 valid rejects + 2 ERROR fail-closed rejects |
| Accept/reject repeated-run consistency | 96/96 = 100% (ERROR maps reject) |
| Complete-valid, consistent accept/reject cases | 94/96 = 97.9167% |
| YES/NO/ABSTAIN primary consistency, errors fail consistency | 93/96 = 96.875% |
| Relation-class consistency, errors fail consistency | 93/96 = 96.875% |
| Relation accuracy among valid outputs | 281/286 = 98.2517% |
| Relation accuracy including ERROR as incorrect | 281/288 = 97.5694% |
| Broad C → specific Q false accept | 0/36 |
| Specific C → broader Q false NO | 0/36 |
| Role/action-consumption false accept | 0/48 |
| Constraint/opposite false accept | 0/24 |
| Gold ambiguity outcomes | 23 ABSTAIN + 1 ERROR; all 24 rejected |
| finish_reason (all attempts) | stop 143; length 2 |

Both first and eventual binary precision/recall are 100%. The binary TN count
includes ERROR rejection **only for acceptance policy accounting**. Original
metrics retain TN=202 and ERROR=2 separately. The 100% acceptance consistency
does not erase incomplete outputs or make eventual validity 100%.

All four language groups have zero false accept. Valid-output relation accuracy:
EN/EN 72/72; EN/ZH 70/71 with one ERROR; ZH/EN 69/72; ZH/ZH 70/71 with one ERROR.
Mercury ambiguity cases both directions are ABSTAIN in all three rounds.

### Failure and semantic disagreement detail

Batch `r1-default-b033` (`h17-f`, `h48-r`) failed twice with
empty final content + finish_reason=length. Each used 4096 completion tokens.
Attempt latency 15.3592s / 15.7096s; total with permitted retry 31.5694s.
No third attempt, output salvage, token-budget change or provider-error masking.
Reasoning was present by metadata; no reasoning text was stored/reported.
There were no auth/quota/transport API failures.

| Case | Frozen expected | R1 / R2 / R3 |
|---|---|---|
| h17-f: 溜冰 ← 滑雪 | sibling_related / NO | ERROR / lexical_ambiguity→ABSTAIN / sibling_related→NO |
| h33-f: Training Seals ← 收藏印章 | unrelated / NO | NO / NO / lexical_ambiguity→ABSTAIN |
| h33-r: 收藏印章 ← Training Seals | unrelated / NO | lexical_ambiguity→ABSTAIN in all 3 |
| h48-r: Calligraphy Styles ← 行 | lexical_ambiguity / ABSTAIN | ERROR / ABSTAIN / ABSTAIN |

All remain rejected. h33-r is consistently wrong at relation/primary level,
illustrating why consistency alone is not accuracy. All broad/narrow directional
cases and Origami equivalence retain correct acceptance in all three rounds.

## Latency / usage

Measured per batch at concurrency three, excluding executor queue wait; this is
not a production latency guarantee. Tokens include the failed attempt and retry.

| Metric | Result |
|---|---:|
| First latency median / p95 | 1.6771s / 5.3569s |
| Eventual latency median / p95 | 1.6771s / 5.3569s |
| Eventual maximum | 31.5694s |
| Completion tokens median / p95 / mean | 260 / 1089 / 417.7931 |
| Completion tokens max / total | 4096 / 60580 |
| Prompt tokens / total tokens | 87493 / 148073 |

## Gate decision

Safety observations are maintained: no false acceptance, no dangerous
directional/role/constraint acceptance, and binary acceptance consistency 100%.

The **unchanged historical engineering gate still fails**:
eventual sample validity is not 100%; primary/relation consistency is below 99%.
First-pass >=99% passes, but does not compensate for the unsuccessful retry.
Historical R3.2c remains STOP; this replay is not retroactively labeled PASS.

Recommendation: preserve evidence and keep production STOP. Before a new unseen
R3.3 holdout, explicitly freeze the prospective acceptance-facing gate, including
separate reliability/ERROR requirements and the role of relation diagnostics.
Do not lower the ERROR gate after seeing this result or count this set as unseen.
No new holdout, prompt/budget sweep, runtime validator, or P1-B implementation
was started.

Production blockers remain independent: historical embedding fingerprint,
production Neo4j version, coverage and integrity are unknown. Local development
safety statistics do not establish production readiness.

## Regression / evidence scope

Hermetic checks:
- Relation parser/metrics/runner + identity v2/fresh normalization:
  147 passed, 87 subtests passed.
- Historical fidelity + OFF-mode parity + embedding-input + search replay:
  118 passed (one existing multipart deprecation warning).
- Total: 265 passed, 87 subtests passed.
- `bash -n start_all.sh`, generated analyzer compile, frozen hash checks,
  `git diff --check`: pass. No service was started.

Only this results document is intended for review/version control. Generated
JSON, synthetic failed outputs, one-off analysis helpers and caches remain under
gitignored artifacts; no source/test/prompt/mapping/config change or new commit.

### Frozen source hashes (unchanged)

- Dataset: `0943c22f0f3aa4e9091644f811161ca8342fa00838359f2ded9239cf303b70e7`.
- Prompt: `cad492b848cc1bab957be56f6accefdadffdee9db0961ec96b4571f5d0483911`.
- Contract: `8442571c0ec7a8d144913d6ad0fb5bb384de6534bee56a4ebe7d03ee7938a69c`.
- Relation parser/mapping: `e0bc2b43876aac9252b472bb4b25b2c42580b7322bc5e834a3135f033229d720`.
- Metrics: `ce1ad5a0c920754b1622d2edd4883f81fe9f975faa0a296806c4da536b3d3bee`.
- Runner: `b349f2db72b0eb0a705cd86c61aa7d0527f7d8c6e82a8f56bc9487570ef7ff38`.

### Ignored generated evidence hashes

All paths relative to this readiness directory; none contain credentials.

| Artifact | SHA-256 |
|---|---|
| artifacts/r32c/manifest.json | a1d793dbe59d20565abcdfac2956dbb177bba856e2d2039c835750caa8edb538 |
| artifacts/r32c/inputs.json | 985aecb819b1c9de148a01eedf2607b2173e56ad499b8cbd25d5622a809da6c7 |
| artifacts/r32c/matrix.json | 0d9317ad76610e8917ba4fdc41e3b40a7df590e438dd867bd45998fbdcc688ff |
| artifacts/r32c/summary.json | 11fce375c5526a49daba98fd57e89942a3dc5af930600e5737f5cc95a93d4fea |
| artifacts/post_p01/preflight.json | d56069e61c4902e7c7392b45ab167cd91d7a782e33af8c5f50182b9d3a266f54 |
| artifacts/post_p01/acceptance_summary.json | 2f7dabe37ea297a329f4fdb98664037089d2441e7ec8d1bcef3c9c446d168e2e |
