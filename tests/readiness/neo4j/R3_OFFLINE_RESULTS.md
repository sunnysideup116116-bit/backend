# R3 offline results — STOP before production implementation

The directional validator is promising as an evidence-sufficiency filter, but the
frozen v1 experiment **does not pass the stability/output gate**. No prompt/label
repair, threshold adjustment, production integration or feature activation follows.

## Frozen evidence

- Branch: `codex/preference-semantic-quality-gate`, base main
  `88bde51b4c9c06f325f6d733e8cc2a6f8117f93c`.
- Holdout SHA: `0943c22f0f3aa4e9091644f811161ca8342fa00838359f2ded9239cf303b70e7`.
- Prompt SHA: `97e12b04f4295399e9ea555c2cf4de81833ca13fccb51f9f46882d9ad47fce9d`.
- Contract SHA: `2764daabe42f3a373e68b7c3ddac348f3a5c39ea8c083354ff3dfa21d9511bfd`.
- These hashes were written before any R3 model/scoring call and verified afterwards.
- 96 directional cases from 48 groups, 28 YES / 60 NO / 8 ABSTAIN; each of
  EN–EN, ZH–ZH, EN–ZH, ZH–EN has 24 cases. Reverse queries are separate cases.
- No canonical pair/reverse pair from R2.5 appears in R3. Cases are manually authored
  synthetic holdout examples, not private user data or a population sample. Groups
  and translations share some concepts, so neither cases nor repeats are independent.
- Input to the validator contains only opaque case ID/Q/C; no expected label,
  human rationale, category, ANN score, profile, user ID or raw memory.

## Model and execution

Ollama Cloud `deepseek-v4.1-flash:cloud`, temperature 0, fixed offline prompt, three
passes with shuffle seeds 7301/7302/7303, 8 cases per batch. 36 real requests,
no auth/quota/transport failure and no retry/repair. Model revision is not an
immutable pinned provider build; results apply to this recorded run only.

34/36 batches passed the strict JSON/ID/decision/relation schema. Third-pass
batches 3 and 9 failed schema validation, producing **16 ERROR cases**, not NO or
ABSTAIN. The batch validator fails closed rather than accepting partial/malformed
output. Raw invalid provider text was not retained, so the exact formatting/enum/
truncation cause is not established; do not assert a more specific root cause.

Gemini `models/gemini-embedding-2`, the same semantic_similarity prefix, 768-d and
L2 contract, embedded 95 unique labels in five requests without failure. Raw
vectors remain in memory and the synthetic local Graph, not Git or JSON reports.
Local pair ANN uses `r3_pair_embedding_index` over temporary `R3Pair` labels on
dedicated `R3SyntheticConcept` nodes, `k=2`. It is not production Concept mutation.
Every directed case was queried separately. The index was ONLINE on the disposable
Community 2026.08.1 baseline. ANN mapping error versus `(1+cosine)/2` is <=6.76e-8.
Pair-threshold quality below is NOT an end-to-end corpus top-K/latency benchmark.

## Validator-only quality

YES precision compares predicted YES to all human non-YES labels (including human
ABSTAIN). YES recall keeps every expected YES in the denominator; ERROR/ABSTAIN do
not disappear from recall. The primary three-way confusion remains separate from
binary acceptance metrics. Never count successful fail-closed behavior as a correct
semantic NO.

| Run | TP | FP | FN | YES precision | YES recall | Valid outputs | Primary accuracy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 28 | 0 | 0 | 100% | 100% | 96/96 | 95/96 |
| 2 | 28 | 0 | 0 | 100% | 100% | 96/96 | 95/96 |
| 3 | 22 | 0 | 6 | 100% | 78.57% | 80/96 | 79/96 |
| Pooled descriptive totals | 78 | 0 | 6 | 100% | 92.86% | 272/288 | 269/288 |

Pooling repeats does not create 288 independent examples or establish production
precision. The zero observed false YES count is not proof of zero false acceptance.

- Primary all-three consistency: 79/96 (82.29%) including output failures.
- All three outputs valid: 80 cases; 79/80 have the same primary decision.
- Decision+relation all-three consistency: 78/96 (81.25%).
- Unanimous YES accepts 22/28 positives with no observed false YES: recall 78.57%.
  This is an observation, not a proposed three-model-call production architecture.

### Required quality strata

| Stratum | Directed cases | Across 3 passes |
| --- | ---: | --- |
| Role mismatch, including action/consumption | 16 | 45 NO, 3 ERROR, **0 erroneous YES** |
| Explicit constraint/opposite | 8 | 23 NO, 1 ERROR, **0 erroneous YES** |
| Equivalent | 16 | 43 YES, **1 erroneous NO**, 4 ERROR |
| Cross-language equivalent subset | 8 | 22 YES, 2 ERROR, **0 erroneous NO** |
| Broad/narrow (excluding composite groups) | 16 | 23 YES, 24 NO, 1 ERROR; all valid directions correct |
| Ambiguous / human ABSTAIN | 8 | 19 ABSTAIN, **2 overconfident NO**, 3 ERROR |

Concrete instability: `h02-f`, Q=Origami, C=Folding Paper into Shapes, changed from
YES/equivalent in runs 1/2 to NO/candidate_broader_insufficient in run 3. Its human
label was not changed. The reverse case remained YES but chose the specific-to-broad
secondary rather than the human equivalent label. This merits independent label/
granularity adjudication, not relabeling to improve the score.

ABSTAIN issue: `h12-r`, Q=Observing Planets, C=Mercury, was labeled NO/role_mismatch
in runs 1/2 instead of abstaining on the ambiguous C. Run 3 had an output error.
Some secondary labels also disagree on homonym versus role/sibling precedence
(for example Training Seals versus 收藏印章). Primary NO can be right while the
explanation type is wrong. Secondary accuracy was 94.79%, 94.79%, 80.21%.

Per-language YES recall: all four groups were 100% in runs 1/2. Run 3 was EN–EN
71.43%, ZH–ZH 71.43%, EN–ZH 100%, ZH–EN 71.43%. The losses mostly reflect the two
invalid batches, not evidence of intrinsic language degradation. All valid predicted
YES decisions were correct in every language group in this small run.

## ANN → validator combined quality

Measured ANN cutoffs are analysis-only; runtime `.82` is unchanged. Combined results
are gate acceptance metrics: ANN filtering is NOT a validator NO decision. Model
ABSTAIN and output ERROR remain non-acceptance, not fabricated semantic negatives.

| ANN | Approx. cosine | ANN-only precision/recall | Combined precision (all runs) | Combined recall runs 1 / 2 / 3 | Pooled descriptive recall |
| --- | --- | --- | --- | --- | --- |
| .92 | .84 | 35.90% / 100% | 100% | 100% / 100% / 78.57% | 92.86% |
| .93 | .86 | 40.91% / 96.43% | 100% | 96.43% / 96.43% / 75% | 89.29% |
| .94 | .88 | 46.30% / 89.29% | 100% | 89.29% / 89.29% / 67.86% | 82.14% |
| .95 | .90 | 52.38% / 78.57% | 100% | 78.57% / 78.57% / 57.14% | 71.43% |

The broad-ANN-plus-directional-validator hypothesis is supported by runs 1/2;
`.92` retained all heldout positives while the validator removed false acceptance.
However the third-run schema failures and semantic flip fail the requested gate.
Do not turn `.92` (or any other value) into a production threshold on this evidence.

## Decision and next step

**STOP production implementation.** No production classifier, prompt, routing,
threshold or flag is changed. Preserve this failed v1 experiment as evidence.

Suggested independently approved next offline step:

1. Add secret-safe detailed schema error categories and retain only synthetic invalid
   output when explicitly permitted, so format failure can be distinguished from
   ID mismatch, inconsistent enum or truncation. Do not call it a semantic rejection.
2. Verify provider support for constrained structured output; evaluate smaller batches
   or bounded output handling offline. Do not silently salvage/change this run.
3. Independently adjudicate equivalent-versus-broader and ambiguous-label rules,
   including secondary-type priority. Keep original scores and labels unchanged.
4. If prompt/contract/parser handling is revised, use this set only for development
   and freeze a NEW holdout for final evaluation. Repeat the role/constraint/direction
   gates, per-language metrics and latency bounds before asking about implementation.

Remaining production blockers are unchanged: historical embedding fingerprint,
Neo4j version, and index coverage/integrity unknown; both semantic flags OFF. No
production Graph, deployment, backfill, P1-B or proposal mutation occurred.

## Final intended diff, not committed

All paths are under `tests/readiness/neo4j/`.

R2.5 freeze (5 files): `fixtures.json`, `README.md`, `R25_SEMANTIC_QUALITY_GATE.md`,
`calibrate_r25.py`, `prepare_r25_downstream.py`.

R3 offline only (6 files): `R3_DIRECTIONAL_CONTRACT.md`, `r3_holdout.json`,
`r3_prompt.txt`, `run_r3_offline.py`, `test_r3_offline.py`, `R3_OFFLINE_RESULTS.md`.

Ignored: all `artifacts/*.json`, generated vectors/provider outputs, old one-off
helpers, local secret/config, venv, query-plan dumps and Docker volumes. Frozen
original R2.5 reports were not overwritten. No production source file is in the diff.

Final validation: 100 tests + 3 subtests passed, including 19 hermetic R3 contract
tests and existing affected P1-A/OFF-parity regressions. Script syntax, `bash -n
start_all.sh`, tracked/untracked whitespace checks, frozen hash/label checks, secret
scans and no-serialized-vector checks passed. Promoted R2.5 downstream replay passed
20 local cases without LLM calls. Disposable Neo4j was stopped again; synthetic
volumes retained. Nothing is staged or committed.
