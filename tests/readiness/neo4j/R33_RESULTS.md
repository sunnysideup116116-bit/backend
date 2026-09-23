# R3.3 unseen directional holdout — FAIL / STOP

Completed 2026-09-23. **Do not enter production implementation or runtime
integration design on this result.** One prospective blocking gate failed.
No gate, prompt, label, mapping, reasoning setting or retry policy was changed
after scoring began. No cases were removed and no extra retries were added.

## Freeze chronology

1. Prior post-P0.1 development evidence, separate commit:
   `6a2505b` — one report only.
2. Prospective acceptance gate, committed before creating any holdout:
   `3ee4d91395c2f4648f02bd107f016f33651bf819`.
3. New holdout, tooling, tests and pre-score freeze:
   `6b38662619fc6981be9de3792a32362c4ef2269f`.
4. Only then prepare/run the provider experiment. Manifest binds all source,
   expanded input and schedule hashes to that freeze commit.

Branch: `codex/preference-r33-holdout`, isolated readiness checkout; no push.
Source base is merged backend main `88b1787898aba8215e3bc056b45d32de30431be0`.
The production Server worktree was not modified. DatingApp #42 was not merged.

## Holdout and annotation provenance

192 directional synthetic cases from 96 new pairs; three fixed rounds,
576 eventual observations. All labels were fixed and hashed before scoring.
**Labels were agent-authored under explicit user authorization and have NOT been
independently human reviewed.** The evaluated provider did not author labels.
These are sample engineering results, not a production precision confidence bound.

Canonical-pair overlap with prior 96-case development and R2.5 is zero (pure and
fresh v2 keys, both directions). “Unseen” means new experiment pairs; it does not
claim unseen general topics or absence from model pretraining.

| Category | Cases | Observations | Final false accepts | Final false rejects |
|---|---:|---:|---:|---:|
| equivalent / paraphrase | 16 | 48 | 0 | 1 |
| broad_narrow | 16 | 48 | 0 | 0 |
| sibling | 16 | 48 | 0 | 0 |
| role | 16 | 48 | 0 | 0 |
| action_consumption | 16 | 48 | 0 | 0 |
| constraint / opposite | 16 | 48 | 0 | 0 |
| ambiguity | 16 | 48 | 0 | 0 |
| unrelated | 16 | 48 | 0 | 0 |
| composite_directional | 16 | 48 | 0 | 0 |
| long_prefix_constraint | 16 | 48 | 0 | 0 |
| long_prefix_role | 16 | 48 | 0 | 0 |
| multilingual_equivalent | 16 | 48 | 0 | 0 |

Gold primary labels: 48 YES, 128 NO, 16 ABSTAIN.
Simplified/Traditional Chinese and cross-language paraphrases are included.
Fourteen source phrases underwent fixed OpenCC 1.4.1/s2twp conversion before the
provider-input freeze; raw and full normalized Q/C are preserved. Maximum input
length is 98 Unicode code points, bounded to 500 with rejection, not truncation.

Sixteen pairs / 32 directions share an identical first 40 code points while
later qualifiers/roles differ. All full v2 keys are distinct. These 96 repeated
observations have correct relation classes and no false acceptance. The model
received full semantic_text, never display_label. No Graph identity/edge changed.

| Q/C language | Cases | Observations | False accept | False reject | Relation correct |
|---|---:|---:|---:|---:|---:|
| EN–EN | 48 | 144 | 0 | 1 | 143 |
| ZH–ZH | 48 | 144 | 0 | 0 | 144 |
| EN–ZH | 48 | 144 | 0 | 0 | 144 |
| ZH–EN | 48 | 144 | 0 | 0 | 142 |

## Fixed execution

Unmodified relation-only v3 prompt/parser, deterministic relation→primary map,
default reasoning (effort omitted), temperature 0, batch 2, max_tokens 4096,
timeout 60s, SDK automatic retries disabled, three workers.
OpenAI SDK 1.30.1 and HTTPX 0.28.1; Ollama Cloud deepseek-v4.1-flash:cloud.
All 291 responses matched that requested model name. No pinned model revision
or reasoning-token breakdown was exposed; neither is inferred.

Seeds 330301/330302/330303, schedule seed 330300. 288 first requests, three
permitted retries, total 291 HTTP calls under the frozen hard maximum 576.
Each retry followed ERROR/length only; no valid NO/ABSTAIN was retried.
All final 288 batches were valid. No provider authentication/quota/transport
failures occurred. No raw reasoning, credential/header or production user data
was saved or sent.

## Blocking gate result

Percentages below are display-only; verdicts use the frozen exact fractions.
ERROR/ABSTAIN/NO reject. ERROR remains separately counted, never recoded as a
model NO. Repeated observations are dependent; consistency denominator is 192.

| Blocking metric | Observed | Required | Verdict |
|---|---:|---:|---|
| Acceptance precision | 143/143 = 100% | >=99% | PASS |
| Acceptance recall | 143/144 = 99.3056% | >=95% | PASS |
| Accept/reject consistency | 191/192 = 99.4792% | >=99% | PASS |
| Broad C → specific Q false accept | 0/48 | 0 | PASS |
| Role/action-consumption false accept | 0/144 | 0 | PASS |
| Constraint conflict false accept | 0/96 | 0 | PASS |
| First-pass schema validity | 285/288 = **98.9583%** | >=99% | **FAIL** |
| Eventual validity after one retry | 288/288 = 100% | >=99% | PASS |
| ERROR fail-open | 0 | 0 | PASS |

At this sample size first-pass validity needs at least 286/288. It achieved 285,
so “approximately 99%” is not a PASS. No rounding, threshold waiver, additional
retry or replacement run is used. **R3.3 = FAIL.**

An independent one-off verifier re-derived acceptance from frozen gold/mapping
and raw saved relation decisions; it agrees on every gate, confusion count and
consistency count. The verifier passing means arithmetic agreement, not R3.3 PASS.

| Round | TP | FP | FN | TN | Eventual ERROR |
|---|---:|---:|---:|---:|---:|
| 1 | 47 | 0 | 1 | 144 | 0 |
| 2 | 48 | 0 | 0 | 144 | 0 |
| 3 | 48 | 0 | 0 | 144 | 0 |
| Total | 143 | 0 | 1 | 432 | 0 |

## Complete false-accept / false-reject list

**False accepts: none (zero cases, zero observations).**

**False rejects: exactly one case / one eventual observation:**

- ID: `u001-r`; category equivalent; language EN–EN.
- Q: `Recognizing Animals by Their Footprints`.
- C: `Identifying Animal Footprints`.
- Frozen gold: equivalent → YES/accept.
- Round 1: role_mismatch → NO/reject (false reject).
- Rounds 2 and 3: equivalent → YES/accept.
- No label adjustment or retry because the first-round NO was undesired.

The false-reject list is for the frozen **eventual acceptance** metric.
First-attempt schema ERRORs below are separately retained, not deleted.

## Schema failures and fail-closed evidence

Three first attempts had empty final content and finish_reason=length at 4096
completion tokens. Each invalidated both cases (six initial ERROR observations).
All three used exactly one identical retry, succeeded, and left zero eventual
ERROR. Invalid attempts contained no salvaged decisions; ERROR fail-open=0.

| Batch | Cases | First / retry completion tokens | First / retry latency | Eventual batch latency |
|---|---|---:|---:|---:|
| r33-r3-b003 | u070-r, u067-r | 4096 / 1623 | 15.8409s / 6.8551s | 23.1963s |
| r33-r1-b018 | u040-r, u085-f | 4096 / 1490 | 17.3219s / 6.7820s | 24.6042s |
| r33-r1-b058 | u040-f, u029-f | 4096 / 3821 | 22.4492s / 14.4289s | 37.3788s |

First-case coverage 570/576; eventual coverage 576/576.
finish_reason across attempts: stop 288, length 3.
Error taxonomy: empty_response 3 + truncated_response 3 (two classifications
of each of the same three failures, not six failed requests).

## Relation diagnostics (not blocking)

- Relation accuracy: 573/576 = 99.4792%.
- Relation-class consistency: 190/192 = 98.9583%.
- Three-way primary consistency: 190/192 = 98.9583%.
- NO↔ABSTAIN disagreement: `u054-r` only.
- Gold ambiguity observations: 47 ABSTAIN + 1 NO, all 48 rejected.
- No extra secondary field is allowed by the relation-only schema; relation
  class is the diagnostic output and never overrides deterministic acceptance.

All relation errors are confined to these two cases:

| Case | Gold | R1 | R2 | R3 |
|---|---|---|---|---|
| u001-r (footprint pair above) | equivalent / YES | role_mismatch / NO | equivalent / YES | equivalent / YES |
| u054-r: 研究國家文化 ← Turkey | lexical_ambiguity / ABSTAIN | unrelated / NO | lexical_ambiguity / ABSTAIN | unknown / ABSTAIN |

u054-r contributes two relation errors but no acceptance error. Its conservative
NO/ABSTAIN variations do not become new blocking gates after seeing results.
Relation consistency below 99% is **not** the cause of this prospective gate FAIL.

## Latency / token usage

Measured per request/batch at concurrency three; excludes executor queue waiting
and is not a production latency guarantee. Tokens include retries/failures.

| Metric | Result |
|---|---:|
| First latency median / p95 | 1.7482s / 5.5998s |
| Eventual latency median / p95 | 1.7482s / 5.5998s |
| Eventual maximum | 37.3788s |
| Completion tokens median / p95 / mean | 265 / 1221 / 408.2887 |
| Completion tokens max / total | 4096 / 118812 |
| Prompt tokens / total tokens | 180728 / 299540 |

## Regression / boundaries / disposition

281 hermetic tests + 90 subtests passed; one existing multipart deprecation
warning. Frozen source/input hashes, independent arithmetic verification,
compileall, bash -n start_all.sh, git diff checks and exact-key artifact scan pass.
No source/config changes after scoring began. No generated report/vector/
provider output/secret is committed; this result document is left for review.

All committed changes are under tests/readiness/neo4j/.
The GitNexus incremental-index anomaly and corrected full-rebuild/source
cross-check are documented in R33_HOLDOUT_FREEZE.md. Dynamic-call/global-index
limits remain explicit; no runtime import of R3.3 modules was found.

**STOP:** no runtime validator implementation or integration design, no prompt
tuning or new scoring condition. Preserve this failure as the frozen result.
Any future experiment needs separate authorization and its own prospective
protocol; do not silently rerun this holdout until it passes.

Both semantic flags remain OFF; .82 unchanged; no deploy, Graph access,
backfill, migration, Concept/alias/PREFERS mutation or P1-B. Historical production
embedding fingerprint, Neo4j version and coverage/integrity remain unknown.
This is offline classification, not production ANN/Matchmaker integration.

## Generated artifact hashes (all gitignored)

| Artifact | SHA-256 |
|---|---|
| artifacts/r33/manifest.json | b9fa632a96de93542932f37d35d5f40f84639dc3826a0c7749ed472e5ddc3e51 |
| artifacts/r33/inputs.json | bf7d09694a425e9041d9ae2704c0ae51d35fb39d1ddb5f50eb0c018b76955b21 |
| artifacts/r33/matrix.json | 77d4ac03b6ee376c34f7617252f4e103c7c5a180ad6576a0a6854d8abc901d17 |
| artifacts/r33/summary.json | 971ce2c58b24167698b7436dee44e2a75f918bcc4e262ce8bdd5f1045f189812 |
| artifacts/r33/independent_verification.json | 56c299089a4f8973adb253b3c01b7ee3a90c52f19103e30ec10f881b03ca8b73 |
