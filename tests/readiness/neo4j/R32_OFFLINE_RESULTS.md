# R3.2 offline refinement results — engineering gate FAIL / production STOP

No runtime P1-A, `.82` threshold, semantic flag, production Graph, deployment,
backfill or P1-B was changed. These 96 cases are development data only; no claim
of final generalization or production readiness is made.

## Evidence closure

- PR #13 merged by normal merge commit with the user-approved documented baseline
  CI exception. Merge/current-main-at-check SHA:
  `62da19c79809d8a25f0aec2f1ed995c1eef78d43`.
- At merge, the PR still contained only the reviewed 11 readiness files; main had
  not advanced from `88bde51`. Social 19/Risk 5 failures matched clean main exactly,
  branch-only failures=0. No unrelated CI fix was made.
- R3.1 independently frozen as `f8bf5ece523add209f47bb5efa0d9888e71a81fc`,
  `test(readiness): add directional validator reliability analysis`, 5 files/+992.
- R3.2 is on `codex/preference-semantic-r32-refinement`; no R3.2 changes are in the
  R3.1 commit. The main Server worktree was not edited/restarted.

## Human contract and immutable inputs

Before the new prompt was written, the operator confirmed ordinary-equivalent
Origami ← Folding Paper into Shapes = YES; Observing Planets ← Mercury = ABSTAIN
with ambiguity checked independently before using pair context; and ambiguity-first
secondary priority with primary authoritative. Details: `R32_CONTRACT.md`.

- Original v1 prompt, contract and 96 labels remain byte-identical.
- v2 prompt: `r32_prompt_v2.txt`.
- v2 prompt SHA: `db71bf2c677a6eef856484097141cd19a53c1a5a3a764a2587f501e031805666`.
- v2 contract SHA: `d14c2e115fe99374e23db11bb9b384fd7903e40446e024d9ecee4a982a9cd359`.
- No post-score prompt revision or label change occurred. The prompt expresses
  general rules, not fixture IDs or case-specific answer keys.
- Legal secondary disagreements are diagnostic only. Genuine schema/API failures
  remain whole-batch ERROR. YES/NO/ABSTAIN are never rewritten from secondary values.

## Verified provider controls

Provider/model: Ollama Cloud `https://ollama.com/v1`, `deepseek-v4.1-flash:cloud`.
The [official OpenAI compatibility documentation](https://docs.ollama.com/api/openai-compatibility)
lists reasoning_effort, max_tokens and stop. Native `think` is not assumed to be a
valid top-level OpenAI-compatible request field. Client timeout is not evidence of
server-side cancellation or a billing stop. Cloud structured output remains
[officially unsupported](https://docs.ollama.com/capabilities/structured-outputs).

Installed OpenAI SDK 1.30.1 does not accept the direct reasoning_effort keyword.
The initial probe's four `none` jobs failed locally before HTTP, while four baseline
calls succeeded. This is an SDK request-assembly issue, not provider rejection.
That original artifact is preserved as `control_probe.json`; it was not overwritten.

The supported SDK `extra_body={"reasoning_effort":"none"}` sends the same documented
top-level wire field without upgrading/modifying production SDK. The separate wire
probe made eight HTTP calls: 4/4 baseline valid with visible reasoning; 4/4 none valid
with reasoning char count 0. None used 43/89 completion tokens for batches 2/4 versus
baseline 359–616. This confirms observable reasoning-output suppression, not absence
of all hidden computation or a numerical reasoning-token budget.

`low`/other documented effort levels were not empirically tested in this run. No
numerical reasoning-token cap was assumed. max_tokens=4096, temperature=0 and client
timeout=60s stayed fixed. No stop sequence was added that might truncate JSON.

## Axis 1 — generation controls, original v1 prompt

Batch 2/4 × omitted effort/none × two fixed-shuffle passes, each condition 192 case
judgments. 288 first requests plus 2 permitted schema retries = 290 HTTP attempts.
There was one baseline-2 empty/truncated response and one baseline-4 partial/truncated
JSON response, both at the 4096 cap and both recovered by one retry. None conditions
had no schema/API failure and no visible reasoning output.

| Config | First valid | Eventual valid | First latency median / p95 | Mean completion tokens/attempt | Valid-first primary accuracy | Eventual all-case primary consistency |
| --- | --- | --- | --- | ---: | ---: | ---: |
| baseline, batch 2 | 95/96 =98.96% | 96/96 =100% | 2.60 / 11.95 s | 427.82 | 99.47% | 95/96 =98.96% |
| baseline, batch 4 | 47/48 =97.92% | 48/48 =100% | 4.76 / 20.75 s | 869.29 | 100% | 95/96 =98.96% |
| none, batch 2 | 96/96 =100% | 96/96 =100% | 1.17 / 2.77 s | 43.39 | 93.75% | 92/96 =95.83% |
| none, batch 4 | 48/48 =100% | 48/48 =100% | 1.31 / 3.11 s | 81.06 | 93.23% | 91/96 =94.79% |

P95 uses nearest rank. Latency includes three-worker/provider load, not a production
SLA or an isolated serving benchmark. Completion tokens are generation usage, not
just final text. All final finish reasons were stop after retry; the two first
failures were length. Reliability/speed improvement did not preserve v1 semantic
accuracy: none had one false YES per condition and YES precision 98.21%.

## Axis 2 — v2 prompt, unchanged development labels

Verified none control, batches 2/4, three new fixed-shuffle passes. 216 HTTP requests,
zero retry, zero ERROR, all finish_reason=stop, zero visible reasoning fields.
The v2 classifier is primary-authoritative by the human-confirmed contract, while
axis 1 retains the old v1 classifier. This difference is explicit, not a silent
change in the historical R3 results.

| Metric | Batch 2 | Batch 4 |
| --- | ---: | ---: |
| First/eventual valid requests | 144/144, 100% | 72/72, 100% |
| Case outputs | 288/288 | 288/288 |
| TP / FP / FN | 84 / 7 / 0 | 84 / 6 / 0 |
| YES precision | **92.31%** | **93.33%** |
| YES recall | 100% | 100% |
| Valid primary accuracy | 93.75% | 95.49% |
| Role mismatch false YES | 0 | 0 |
| Constraint/opposite false YES | 0 | 0 |
| Equivalent false NO | 0 | 0 |
| Broad/narrow primary errors | 10 | 8 |
| Expected-ABSTAIN outcomes | 21 ABSTAIN / 3 NO | 21 ABSTAIN / 3 NO |
| Primary all-three consistency | **87/96 =90.63%** | **89/96 =92.71%** |
| Median / p95 first latency | 1.18 / 1.74 s | 1.20 / 2.22 s |
| Mean completion tokens | 43.77 | 81.86 |

Every false YES was an unsupported broad-to-specific inference, for example:

- Q=Playing Badminton, C=Racket Sports.
- Q=學習義大利語, C=Studying Languages.
- Q=Basket Weaving, C=手工藝.
- Q=Studying Ancient Roman History, C=研究歷史.

These are wrong primary decisions, not secondary overriding a correct primary.
The model sometimes reversed candidate-broader-insufficient into candidate-specific-
satisfies-broader. Labels were not changed to hide these errors.

Human focus cases:

- Origami and its reverse: YES/equivalent in all 12 judgments (2 sizes ×3 runs ×2 directions).
- Q=Observing Planets, C=Mercury: ABSTAIN in all six judgments.
- Reverse Q=Mercury, C=Observing Planets: incorrect NO in all six judgments, even
  though stable. Ambiguity handling is therefore not fully solved.
- Legal secondary mismatches were retained as diagnostics (ABSTAIN with a non-unknown
  secondary) without turning ABSTAIN into YES or triggering a retry.

Metrics preserve ERROR separately; a positive ERROR remains a recall FN, so ERROR
and FN are not disjoint counts. Binary TN is non-YES rejection, not proof of correct
three-way semantics: expected ABSTAIN answered NO is still shown as a primary error.
Consistency uses eventual decisions across all 96 cases; any missing/error case is
not consistent. Repeats are correlated and do not create independent validation data.

## Proposed engineering gate assessment

| Proposed gate | Batch 2 | Batch 4 |
| --- | --- | --- |
| First-pass >=99% | PASS | PASS |
| Eventual >=99.9% / sample 100% | PASS | PASS |
| Role/constraint false YES =0 | PASS | PASS |
| Primary consistency >=99% | **FAIL** | **FAIL** |
| Development YES precision >=99% | **FAIL** | **FAIL** |

**R3.2 FAIL: keep STOP. Do not proceed to production implementation/enable or use a
fresh R3.3 holdout as acceptance evidence yet.** No additional tuning/relabeling was
performed to turn this run into a pass.

The next independently approved offline investigation should distinguish the v2
wording/primary-direction problem from reduced-reasoning effects, for example fixed
v2 under current or documented low effort and explicitly directional evidence tests.
Do not assume the more permissive paraphrase rule solved directionality. Preserve
all old labels/results; any new prompt/version remains development until it passes
the gate and then faces a genuinely fresh frozen holdout.

## Artifacts and boundaries

Ignored outputs under `artifacts/r32/`: initial SDK probe, wire probe, phase manifests,
generation/development results, sanitized failed outputs and summary. No credentials,
provider reasoning text or generated vectors are committed. Old frozen R3/R3.1 files
and their hashes remain unchanged. R3.2 files are uncommitted on their separate branch.
Production embedding fingerprint, Neo4j version and index coverage/integrity remain
unknown. Semantic flags OFF; no `.82` change, Graph access, deploy, backfill or P1-B.

Final validation: 163 tests + 97 subtests passed (one existing multipart deprecation
warning). Original frozen hashes, v2 pre-scoring hashes, source/commit separation,
secret/no-vector scans, Python syntax, shell syntax and whitespace checks passed.
R3.1 freeze is a local commit; its separate push/PR confirmation is still pending.
