# R3.2b — reasoning × directionality isolation (offline development evidence)

Production STOP. This experiment does not modify P1-A runtime, its `.82` threshold,
either OFF feature flag, Concept identities or Graph edges. No Graph was accessed;
there was no deploy, backfill or P1-B work. Historical production fingerprint is
still unknown. This 96-case set is development data, not a fresh holdout.

## Independent R3.2 freeze

R3.2 was reviewed and frozen first in local commit
`0f065de5b965dc3b654b26d824c367eb0bdbffb8`:
`test(readiness): freeze offline validator refinement evidence`.
It contains nine readiness files, 1,413 inserted lines, no generated artifacts.
R3.2b then started on independent branch `codex/preference-semantic-r32b-isolation`.
Neither this freeze nor the R3.2b experiment has been pushed by this task.

## Fixed conditions and evidence handling

- Provider: Ollama Cloud, `deepseek-v4.1-flash:cloud`, OpenAI-compatible API.
- SDK: installed OpenAI 1.30.1; effort uses `extra_body` to place the documented
  field in the top-level wire JSON. No SDK upgrade or production SDK change.
- Same frozen v2 prompt/contract and all original human labels; temperature **0
  sent**, batch **2**, max_tokens **4096**, timeout 60 seconds, SDK auto-retries 0.
  Sending temperature 0 is not proof of deterministic provider behavior.
- Same three fixed shuffles (9401, 9402, 9403), same paired cases across controls,
  interleaved request order, three workers. Each included arm has 144 first requests
  and 288 expected case decisions. Repeated decisions are not independent samples.
- One identical retry only for schema/empty/truncation/transient provider failure.
  Valid NO/ABSTAIN is never retried. Frozen parser semantics are reused unchanged.
- ERROR is recorded separately, never relabeled NO; missing/error cases fail the
  all-96-case repeated-consistency denominator. Broad/narrow counts follow frozen
  relation labels across **all** categories, including composite cases.
- 28 YES / 60 NO / 8 ABSTAIN gold cases; EN/EN, ZH/ZH, EN/ZH, ZH/EN each 24.
- Credential remains process-only, selected from the existing explicitly approved
  LLM config fields. No whole-env load; no key/fragment, headers, exceptions or
  reasoning text in reports. No production user/profile/memory data is sent.
- Raw generated experiment reports are under ignored `artifacts/r32b/`; only
  synthetic sanitized failed final-answer text may be retained there. No vectors.

Frozen hashes:

| Input | SHA-256 |
|---|---|
| `r3_holdout.json` | `0943c22f0f3aa4e9091644f811161ca8342fa00838359f2ded9239cf303b70e7` |
| `r32_prompt_v2.txt` | `db71bf2c677a6eef856484097141cd19a53c1a5a3a764a2587f501e031805666` |
| `R32_CONTRACT.md` | `d14c2e115fe99374e23db11bb9b384fd7903e40446e024d9ecee4a982a9cd359` |
| `r32_primary.py` | `65d41b1071e1f063a82e5aee920e916bd50b311663cb4d7cd2cc287fe73a2151` |
| `r31_diagnostics.py` | `91ae45842b3e6b90cf2c905b4fe3880f94015cd9d76a77f2c498412fa911190f` |

## Actual control probe: low arm not admitted

[Ollama's API documentation](https://docs.ollama.com/api/openai-compatibility)
lists `reasoning_effort` including `none` and `low`. Documentation alone does not
establish how the deployed Cloud model implements the level.

Seven synthetic calls used two identical two-case v2 batches across none, low and
omitted/default, plus one deliberately invalid effort enum. All seven returned
valid final JSON; no retries or provider failures.

| Requested setting | Calls | Visible reasoning | Completion tokens |
|---|---:|---:|---|
| none | 2 | 0/2 | 39, 53 |
| low | 2 | 2/2 | 391, 135 |
| omitted/default | 2 | 2/2 | 259, 138 |
| deliberately invalid enum | 1 | 1/1 | 607 |

Because **even the invalid enum was accepted**, low enforcement/mapping is
unverified. `effective_effort=unknown`; the probe remains INCONCLUSIVE. Low is
**SKIPPED**, not scored as pass/fail and not substituted with another setting.
The none/default experiment proceeds because both complete probe batches were
valid, requested-model-matched and showed the expected observable reasoning
absence/presence. This establishes an output distinction, not hidden compute use.

## Completed matrix — 2026-09-22

There were **288 matrix API calls plus 7 separate probe calls**, zero retries,
zero provider/schema errors and zero length/truncation finishes. Low has no matrix
sample; no conclusion about its semantic quality is possible.

| Metric | none | omitted/default | low |
|---|---:|---:|---|
| First-pass validity | 144/144 (100%) | 144/144 (100%) | skipped |
| One-retry eventual validity | 144/144 (100%) | 144/144 (100%) | skipped |
| Retry / ERROR | 0 / 0 | 0 / 0 | N/A |
| finish_reason | stop: 144 | stop: 144 | N/A |
| Length/truncation rate | 0% | 0% | N/A |
| Median / p95 latency, seconds | 1.0765 / 1.6849 | 2.6803 / 10.9264 | N/A |
| Completion tokens, median / p95 | 42.5 / 50 | 248 / 1,332 | N/A |
| Completion tokens, mean / max | 43.4931 / 53 | 419.6806 / 3,410 | N/A |
| Total completion tokens | 6,263 | 60,434 | N/A |
| TP / FP / FN | 83 / 7 / 1 | 84 / 0 / 0 | N/A |
| YES precision | 83/90 = 92.2222% | 84/84 = 100% | N/A |
| YES recall | 83/84 = 98.8095% | 84/84 = 100% | N/A |
| Primary accuracy | 266/288 = 92.3611% | 284/288 = 98.6111% | N/A |
| Primary three-round consistency | 91/96 = 94.7917% | **95/96 = 98.958333...%** | N/A |
| Secondary three-round consistency | 83/96 = 86.4583% | 87/96 = 90.625% | N/A |
| Broad candidate → specific query false YES | 7/36 | 0/36 | N/A |
| Specific candidate → broad query false NO | 1/36 | 0/36 | N/A |
| Role mismatch false YES | 0/48 | 0/48 | N/A |
| Constraint/opposite false YES | 0/24 | 0/24 | N/A |
| Equivalent false NO | 0/48 | 0/48 | N/A |
| All directional wrong outputs, including ABSTAIN | 14 | 4 | N/A |
| Gold-ambiguous outcomes | 19 ABSTAIN, 5 NO | 24 ABSTAIN | N/A |
| Proposed complete gate | **FAIL** | **FAIL** | not evaluated |

Latency is request wall time at concurrency three, p95 nearest-rank, not a
production latency SLA. No retries means first/eventual latencies coincide up to
runner overhead. Provider did not supply reasoning-token detail; completion tokens
are the reported field, not an independently measured hidden-reasoning budget.

Per-round precision: none 28/31, 28/30, 27/29; default 28/28 in every round.
Per-round recall: none 28/28, 28/28, 27/28; default 28/28 in every round.
The none arm fails precision, consistency and broad→specific safety gates.
Default passes every listed gate **except primary consistency**. Do not round
98.958333...% to 99% or silently substitute binary acceptance consistency for
the required three-way YES/NO/ABSTAIN consistency. No R3.3 recommendation.

## All broad/narrow primary mistakes

`Y/N/A` mean YES/NO/ABSTAIN; rounds are 1/2/3. Every row below is a case with at
least one wrong primary against the unchanged gold label. Other directional cases
were primary-correct in all three rounds. Secondary disagreements are separately
retained in the ignored summary; they never rewrite primary.

| Arm | ID | Actual Q ← actual C | Gold | R1/R2/R3 |
|---|---|---|---|---|
| none | h03-r | Playing Badminton ← Racket Sports | N | Y/N/Y |
| none | h04-r | Collecting Vintage Postage Stamps ← Collecting Stamps | N | Y/Y/Y |
| none | h15-r | 打網球 ← 球拍運動 | N | Y/N/N |
| none | h23-r | 在平坦無階梯的河濱步道散步 ← 河邊散步 | N | N/Y/N |
| none | h27-r | 划獨木舟 ← Water Sports | N | A/A/A |
| none | h39-f | 手工藝 ← Basket Weaving | Y | Y/Y/N |
| none | h39-r | Basket Weaving ← 手工藝 | N | A/A/A |
| default | h11-r | `Watching Waterfalls on Short Accessible ` ← Watching Waterfalls | N | A/A/A |
| default | h47-r | `Visiting Quiet Castles without Guided Gr` ← 參觀城堡 | N | N/A/N |

In the none arm h03-r, h04-r and h15-r's false YES also choose the reversed
`candidate_specific_satisfies_broader_query` relation; h23-r chooses equivalent.
Thus merely mapping the current secondary enum to acceptance would **not** fix
these errors. A relation-only model task needs independent evaluation.

Origami ↔ Folding Paper into Shapes is YES for all six directional judgments in
each arm. For Mercury: none gives Mercury ← Observing Planets NO/NO/NO (wrong),
the reverse A/A/A; default gives A/A/A for both directions. The none arm also
gives 舞台表演 ← 拍 N/N/A where the gold is ABSTAIN. All other gold-ambiguous
cases are A/A/A in both arms.

## Important inherited input-integrity confound — unchanged this round

Read-only tracing found two full fixture labels are cut to **40 characters** by
the pre-existing canonical-label boundary before submission:

| Full frozen fixture label | Actual canonical input | Directional IDs |
|---|---|---|
| Watching Waterfalls on Short Accessible Trails | `Watching Waterfalls on Short Accessible ` | h11-f / h11-r |
| Visiting Quiet Castles without Guided Groups | `Visiting Quiet Castles without Guided Gr` | h47-f / h47-r |

Actual path: `run_r32b_offline.frozen_v2_cases` →
`run_r31_reliability.frozen_cases` → `run_readiness.canonical_module` →
`matchmaker_agent/concept_identity.py:canonicalize_concept` → `_clean_label`
(`text[:40]`). Original R3 already canonicalized these inputs; this is **not**
new R3.2b preprocessing. Human gold labels still describe the complete phrases.

Both default wrong-primary cases occur at this inherited input-vs-label mismatch,
and its only unstable case is h47-r. Lost qualifiers plausibly contribute to
ABSTAIN, but causal attribution has not been proven by a controlled follow-up.
We did not restore words, remove cases, relabel, change parsing, rerun or waive the
gate. The official all-96 result stays FAIL. This also means the residual default
errors cannot responsibly be described as proven direction-reversal failures.

## Decision and next offline design (not implemented)

Keep STOP: no R3.3, production changes or further v2 prompt tuning. Default
reasoning is much stronger than none on this development sample; however it did
not pass the agreed complete gate. Low is unconfirmed, not another failed arm.
There is **no length failure in this sample**, so it does not justify increasing
max_tokens or starting a budget experiment now.

The next separately versioned offline investigation should first inventory exact
fixture→canonical→submitted text, preserving current evidence. Any correction to
the bounded-description input contract must be explicitly approved and evaluated
as a new experiment, not folded into this result.

Then test the requested relation-only design, without asking the model for a
primary acceptance policy:

```text
bounded Q/C labels or approved descriptions
→ model returns a directional relation enum only
→ deterministic policy mapping
→ YES / NO / ABSTAIN (ERROR remains distinct and fail-closed)
```

Proposed offline mapping, subject to a separately frozen experiment:

| Relation | Deterministic primary |
|---|---|
| equivalent; candidate_specific_satisfies_broader_query | YES |
| candidate_broader_insufficient; sibling_related; role_mismatch; constraint_conflict; lexical_ambiguity (two clear but different senses); unrelated | NO |
| unknown / unresolved meaning | ABSTAIN |
| Invalid/missing schema or API/parser failure | ERROR → reject |

Keep ambiguity-first semantics. Do not confuse the existing `lexical_ambiguity`
enum (clear different senses) with unresolved meaning. A deterministic map cannot
repair a wrong relation or missing input qualifier. Test relation orientation and
mapping separately, freeze new prompt/version, leave the 96 cases development-only,
and propose a truly new R3.3 holdout only after an independently passing gate.

## Tooling checks and scope

R3.2b adds seven readiness-only files: this report, probe/runner/metrics modules,
and three test modules. No tracked R3.2/frozen input or production file changed.
The seven files remain uncommitted; generated provider outputs stay ignored.

Focused R3/R3.1/R3.2/R3.2b plus preference-semantic/search/OFF-parity/qualification
regressions: **196 passed, 142 subtests passed**, one existing multipart deprecation
warning. Compileall, `git diff --check` and `bash -n start_all.sh` pass.
GitNexus scoped diff analysis reports LOW for readiness-only symbols after a
successful index rebuild. Its global process index has traversal limits, so zero
reported flows is not treated as an exhaustive proof; explicit path/import review
and hermetic tests establish the offline boundary.

Ignored evidence SHA-256 (files themselves are not committed):

| Artifact under `artifacts/r32b/` | SHA-256 |
|---|---|
| control_probe.json | `0696431e79458c6229b15424d7a1ecf0223ef22a4afe1cad197dc44d286834d2` |
| manifest.json | `8e91116db538089cee9b6cadaa8df942fd29cee12711eae912d2c0d0c30d1975` |
| matrix.json | `c17f9bf611bfcf3aac568c42344f3a79acef1ba55c656da4a2843aa7f2eaf6cf` |
| summary.json | `284d029e7471f7605eab7fef26e017760a44d50cf41675b62e849f6b89908fb3` |

## Reproduction

Use an approved local Python environment containing the existing OpenAI SDK,
httpx and offline test dependencies. Supply only local config **paths**, never
credential values on the command line. Scripts select only LLM_API_KEY,
LLM_BASE_URL and LLM_MODEL_ID; they do not load DB or Graph settings.

```bash
export AYUE_SKIP_DOTENV=1
export MATCH_PREFERENCE_SEMANTIC_MODE=off
export MATCH_PREFERENCE_SEMANTIC_EMBEDDING_SPACE_CONFIRMED=off
python tests/readiness/neo4j/r32b_controls_probe.py \
  --model-config <approved-existing-model-config> \
  --matchmaker-config <approved-existing-llm-config> \
  --confirm-synthetic-provider-use --include-invalid-effort
python tests/readiness/neo4j/run_r32b_offline.py run \
  --model-config <approved-existing-model-config> \
  --matchmaker-config <approved-existing-llm-config> \
  --confirm-synthetic-provider-use
python tests/readiness/neo4j/run_r32b_offline.py summarize
```

The runner refuses to overwrite an experiment. Preserve original artifacts
before any deliberate new run; do not silently rerun failures until favorable.
