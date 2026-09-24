# Relation Validator Backend Selection — development STOP

Completed 2026-09-24. No qualifying backend; no third final holdout created.
This is a development comparison, not final validation or production readiness.
R3.3 and R3.4 final FAIL remain permanent; neither dataset was used here.

## Evidence and frozen task

- R3.4 final result freeze: `a6b4b9969e40843418193aec7903593db4055f77`.
- Prospective backend protocol/tooling/human-review process:
  `34c79297367e6777807db7344999d3b71c1c47ad`.
- Original 96-case development set, three fixed shuffled rounds, batch 2:
  144 first requests / 288 case observations per backend.
- Same full Q/C, relation-only v3 prompt, frozen labels, parser and deterministic
  mapping. No semantic prompt/config sweep. Temperature 0, output limit 4096,
  timeout 60 seconds, at most one eligible error retry; valid NO/ABSTAIN never retried.
- Model outputs relation only. Equivalent/more-specific accept; all other relations,
  ABSTAIN and ERROR reject. No model explanation or primary-policy override.
- Independently recalculated pooled TP/FP/FN and binary consistency from final
  observations: identical to frozen summarizer. Missing token metadata is unknown,
  not evidence of zero usage. No lost/removed cases or replacement requests.

Frozen source/prompt/label/config hashes remain in the ignored manifest. Generated
artifacts are not committed, including provider responses and request-level data.
SHA-256 evidence references (not credential metadata):

| Artifact | SHA-256 |
| --- | --- |
| development_manifest.json | 98f7c76e6a1a8276145a0f32d23414ba93be554a799f1a5d24f8e9bb40ece2bf |
| development_matrix.json | ae0a85805d2d940a7d462f09c7f1765aaeef24b3ddeafdb7a902a9a6ae127b4e |
| development_summary.json | 254fa1cff1af263af20f240b68b02552c9dc61105447b14cb9c4e3d8b5e8e037 |

## Backend capability and availability

| Provider/model | Native schema / actual probe | Reasoning control | Development status / integration delta |
| --- | --- | --- | --- |
| Google Gemini 2.5 Flash | Documented JSON Schema subset; relation schema, contradictory sentinel, enum/required/no-extra/cardinality probes passed; invalid schema rejected | Fixed thinkingBudget 1024 | Scored; separate native adapter, existing approved Google pool; quota reliability failed |
| Google Gemini 3.1 Flash-Lite | Documented schema support; positive probes returned 503, including one permitted retry; invalid schema rejected | Documented minimal | NOT_RUN: availability, not semantic failure; native adapter exists offline |
| Google Gemini 3.5 Flash-Lite | Configured latest alias resolved to this pinned model; sentinel/invalid-schema probes passed, relation probe 503/cardinality timeout | Documented minimal | NOT_RUN: complete capability gate not satisfied; no silent model substitution |
| Local Ollama qwen3.5:0.8b | Actual installed GGUF, not cloud alias; native format schema passed all four capability probes | think=false | Scored; local HTTP adapter, no new service/download; semantic failure |
| Ollama Cloud / current DeepSeek | Official cloud structured-output support absent | Not retuned | Excluded; no further generation/batch/budget tuning |
| OpenAI gpt-4o-mini snapshot | Documented strict structured output, but existing project credential model lookup returned 401 | Not exercised | NOT_RUN; no generation or inferred credential permission |
| Local gemma4:e2b | Installed actual model, not capability-qualified | Not exercised | Not selected in bounded initial comparison (memory/time); no claim of PASS/FAIL |

Google metadata access did not prove generate access. Per-model capability setup
used only schema-only synthetic probes, reported aggregate availability, and kept
credential identity solely in process memory. No failed development request was
hidden. Gemini 2.5 successful generation responses resolved to `gemini-2.5-flash`;
local responses resolved to `qwen3.5:0.8b`. No immutable Google revision was supplied
in those successful responses; do not infer a historical model fingerprint.

These probes demonstrate the tested schema subset, not universal JSON Schema
support or semantic correctness. The frozen parser still enforces unique/missing
case IDs and rejects truncated/refused/unparseable output. Both adapters use the
same frozen system prompt and complete serialized id/Q/C input.

## Development gates

Counts below are per 288 case observations unless stated as request/case counts.
Operational recall includes rejected ERROR positives; ERROR is not relabeled NO.

| Metric | Required | Gemini 2.5 Flash | Local Qwen 0.8B |
| --- | --- | --- | --- |
| First-pass valid requests | >=99.5% | 56/144 = 38.89% | 144/144 = 100% |
| Eventual valid requests | 100% in sample | 62/144 = 43.06% | 144/144 = 100% |
| Acceptance precision | >=99% | 40/42 = 95.24% | 43/128 = 33.59% |
| Acceptance recall | >=95% | 40/84 = 47.62% | 43/84 = 51.19% |
| Binary consistency across all three rounds | >=99% | 75/96 = 78.13% | 35/96 = 36.46% |
| Broad-to-specific false accept | 0 | 0 observed, incomplete coverage | 14 |
| Role false accept | 0 | 0 observed, incomplete coverage | 18 |
| Constraint false accept | 0 | 0 observed, incomplete coverage | 19 |
| ERROR fail-open | 0 | 0 | 0 |
| Selected | all gates | NO | NO |

### Availability versus semantic quality

Gemini: 232 attempts = 144 first + 88 permitted retries. 170 attempts returned
quota_exhausted (HTTP 429); 62 returned valid STOP output. Six first failures
recovered; 82 request batches remained ERROR, affecting 164 case observations.
There was no length/schema failure among successful responses. The 44 positive
false rejects were ERROR fail-closed outcomes, not valid semantic NO decisions.
Conditional on the 124 valid final observations: relation accuracy 117/124=94.35%,
YES precision 40/42=95.24%, YES recall 40/40=100%. This quota-selected subset is not
a substitute for the full gate or an unbiased generalization estimate.

Both Gemini false accepts were real valid answers, not quota artifacts:

| Case | Query | Candidate confirmed concept | Frozen gold | Model relation |
| --- | --- | --- | --- | --- |
| h12-r, round 1 | Observing Planets | Mercury | lexical_ambiguity / ABSTAIN | candidate_more_specific / YES |
| h48-r, round 1 | Calligraphy Styles | 行 | lexical_ambiguity / ABSTAIN | candidate_more_specific / YES |

Only seven Gemini cases had valid answers in all three rounds; all seven relations
were consistent. Reporting 7/7 alone would hide missing coverage. The 7/96 complete
relation-consistency rate is diagnostic, not proof of semantic instability caused
by quota. The all-case binary consistency remains the blocking operational metric.
Ambiguity observations: 4 ABSTAIN, 2 NO, 2 YES, 16 ERROR (24 total).

Qwen: 144 attempts, no retries, all valid STOP; no schema/API/length errors.
Relation accuracy 71/288=24.65%, relation-consistent cases 17/96=17.71%.
There were 85 false-accept observations across 51 unique cases, and 41 false-reject
observations across 23 unique cases. Examples include:

- Playing Badminton <- Racket Sports: broader evidence accepted once.
- Watching Waterfalls on Short Accessible Trails <- Watching Waterfalls: accepted
  in all three rounds despite the missing qualifier.
- Attending Stand-up Comedy Shows <- Performing Stand-up Comedy: accepted twice.
- 無糖甜點 <- 高糖甜點 and Mild Curries <- 超辣咖哩: each accepted in all three rounds.
- Ambiguity: 18 NO, 6 YES, zero ABSTAIN across 24 observations.

Schema constraint success therefore did not imply reliable relation classification.
No Qwen prompt/temperature/reasoning refinement was attempted after seeing scores.

### Language diagnostics

Each language has 24 cases x 3 rounds = 72 observations. Numerators are correct
relation classes; Gemini denominators exclude ERROR solely for this diagnostic.

| Language | Gemini valid-only relation accuracy (ERROR count) | Qwen relation accuracy |
| --- | --- | --- |
| EN-EN | 22/24 = 91.67% (48) | 19/72 = 26.39% |
| ZH-ZH | 36/38 = 94.74% (34) | 21/72 = 29.17% |
| EN-ZH | 31/34 = 91.18% (38) | 16/72 = 22.22% |
| ZH-EN | 28/28 = 100% (44) | 15/72 = 20.83% |

## Latency, usage and cost

p95 uses nearest rank; timings are representative local measurements, not a
production SLA. Provider latency excludes shared Google pacing/local queue wait;
wall includes pacing/queue plus a permitted retry. Google quota replies must not
be used to claim fast classification.

| Metric | Gemini | Qwen local |
| --- | --- | --- |
| Successful provider request p50 / p95 | 3.344 / 5.180 seconds, n=62 | 4.356 / 4.479 seconds, n=144 |
| Full batch wall p50 / p95 | 5.901 / 12.551 seconds | 8.796 / 13.252 seconds |
| Reported prompt tokens | 36,200 | 85,782 |
| Reported completion tokens | 31,489 (29,459 reasoning + 2,030 visible) | 4,370 |
| Reported total tokens | 67,689 | 90,152 |

At public Gemini 2.5 standard text rates ($0.30/M input, $2.50/M output including
thinking), those reported development tokens imply about $0.0895825, NOT an actual
bill. Capability/setup probes excluded; error responses supplied no usage. Free
tier, caching and actual account billing were not inspected. Local API fee is zero;
hardware/electricity/time cost is not zero and was not estimated.
After the bounded local keep-alive expired, read-only /api/ps showed zero loaded
models. No model pull, server restart or global service configuration change.

## Decision, independent review and boundaries

STOP. No backend met the predeclared development gate, so no new final gate/data
or third final scoring was started. This does not prove all providers unusable:
3.1/3.5 were unavailable in this window, not evaluated semantically. It does prove
there is no qualified candidate in this experiment. Do not retune/retest final
holdouts or erase the quota failures to manufacture a PASS.

Recommendation: retain deterministic exact matching as the active behavior and
leave semantic fallback OFF. Further investment requires a separately approved,
bounded provider-availability/cost decision rather than endless prompt/holdout
iterations. Gemini 2.5 would still need to address observed ambiguity acceptance;
quota recovery alone cannot justify moving it to final evaluation.

The user selected another teammate for independent blind review. The prospective
process is in INDEPENDENT_LABEL_REVIEW_PROCESS.md: hidden author gold/model output,
full Q/C only, independent labels, human adjudication before hashes/scoring. No
human review is represented as completed; no blind packet is needed until a
backend qualifies. Do not ask the teammate to review retired holdouts as a new final.

Runtime flags remain OFF, .82 unchanged. No production source, Graph, deployment,
backfill, Concept identity/ownership change or P1-B. Production historical embedding
fingerprint/version/coverage/integrity remain independent unresolved blockers.
Generated artifacts/outputs stay ignored; all committed material is readiness-only.

Verification: focused readiness, identity-v2/fresh-normalization and OFF parity
regressions: 195 passed, 87 subtests. Syntax/diff/secret/hash checks performed;
GitNexus offline dynamic-call edges are UNKNOWN, not an all-clear: explicit caller
search confines these new adapters/metrics to readiness runner/tests. No claim of
a full production regression or universal provider capability guarantee.

## Closeout review (2026-09-24)

The owner accepted this result and stopped the current P1-A production
qualification. See [rollout decision](../../../docs/PREFERENCE_SEMANTIC_ROLLOUT_DECISION.md).
No more prompt/model/backend sweeps or fresh holdouts are authorized by this
report. Existing experimental protocols describe history, not an active plan.

Final review found no production imports of the new native adapters, metrics or
runner, no secret values in intended evidence, and no generated artifacts selected
for Git. No prompt/labels/mapping/config was changed during closeout. Provider
availability failures remain distinct from semantic failures; all gates and both
previous final FAIL verdicts remain frozen.

Tooling limitations are retained explicitly rather than refactored during closeout:
the provider wrapper uses the previously approved local credential-source path;
the historical readiness probe expects ignored capability/follow-up artifacts;
and the runner verifies the original source commit and artifact hashes. These are
bounded offline components, not a portable one-command fresh calibration setup.
Archival source checkout plus the original ignored inputs is needed to reproduce
the old summary; no automatic regeneration/scoring should be attempted. Pure
parser/metrics/adapter tests remain hermetic and do not require credentials.

Closeout verification expanded the previous focused run to include R3.3/R3.4
offline harness regressions: **224 passed, 90 subtests passed**, one existing
python-multipart deprecation warning. No real provider/Graph/scoring request was
performed. Frozen source/gate/label hashes and generated-report ignores were
rechecked; production runtime files and .82/OFF defaults are unchanged.

## Primary contract references

- [Google structured output](https://ai.google.dev/gemini-api/docs/generate-content/structured-output)
- [Google thinking controls](https://ai.google.dev/gemini-api/docs/generate-content/thinking)
- [Google pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Ollama structured outputs / cloud limitation](https://docs.ollama.com/capabilities/structured-outputs)
- [Ollama native chat API](https://docs.ollama.com/api/chat)
- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
