# R3.1 reliability investigation — development set only

Freeze note: the experiment/status descriptions below preserve the R3.1 closeout
snapshot. PR #13 was subsequently merged with the approved baseline CI exception
as main `62da19c79809d8a25f0aec2f1ed995c1eef78d43`. This independent freeze contains
only R3.1 evidence/tooling/tests; no R3.2 prompt/contract or generation refinement.
Original generated reports and frozen R3 hashes are not rewritten by this freeze.
The freeze audit encountered corrupt incremental GitNexus symbol IDs and an apparent
CRITICAL cross-project blast radius. A full local index rebuild restored exact test
callers; repeat detect-changes reported 5 readiness files / 78 symbols / LOW. Manual
imports and staged diff checks independently confirmed no production callers/edits.

This work is separate from frozen commit `60d3142b063793039cc7dc88fc6fc1736caba982`
and PR #13. No R3 prompt, contract, label, production runtime, threshold or semantic
flag is modified. The current 96 cases are a development/debugging set, not a new
final validation holdout. All provider results remain under ignored `artifacts/r31/`.

## What can and cannot be established

The original R3 third-pass batches 3 and 9 saved only `invalid_output`, not the raw
response, finish reason or token usage. It is impossible to prove the exact contents
of those two historical responses retrospectively. R3.1 replays their exact ordered
IDs and unchanged canonical inputs/prompt/model/temperature/token budget to collect
new evidence. Reproduction of a failure mechanism is not proof of the historical
response bytes.

## Fixed protocol

- Provider: Ollama Cloud `/v1/chat/completions`, `deepseek-v4.1-flash:cloud`.
- Same R3 prompt/labels/canonicalization; frozen hashes checked before calls.
- Temperature 0, `max_tokens=4096`, timeout 60s, SDK auto-retry disabled.
- Original failed batches replayed twice: 4 first requests, at most 8 attempts.
- Matrix: batch sizes 1/2/4/8, 96 cases per size per repeat, two repeats using
  shuffle seeds 7303/7304; 360 first requests, hard maximum 720 attempts.
- Interleave conditions with fixed seed 93131; up to three concurrent requests.
  Latency includes network/provider queuing and is not an isolated serving benchmark.
- One identical retry only for schema or transient API failure. Valid YES, NO and
  ABSTAIN never trigger a retry. Authentication/contract/internal failures do not
  get repaired or repeatedly retried.
- No tools, profiles, user IDs, ANN score or human labels are sent to the model.
- No Graph connection or production service endpoint is used in R3.1.

## Failure taxonomy and retention

`r31_diagnostics.py` classifies empty response; provider-indicated truncation;
invalid JSON; missing/duplicate cases; primary/secondary enum errors; ID mismatch;
unexpected fields; decision/relation mismatch; provider/API error; and parser/internal
failure. Multiple observed categories may describe one attempt, so category totals
may exceed failed-request totals. Invalid JSON is not assumed to be truncation.

Any failure returns `valid=false, decisions={}` for the entire batch. No codefence
stripping, missing-ID fabrication, partial salvage or ERROR-to-NO conversion occurs.
Metadata retains only bounded counts, known synthetic missing/duplicate IDs and
allowlisted finish reason. Exceptions are never stringified or serialized.

Only this synthetic experiment's failed final-answer text may be saved. It is checked
against the actual credential, prefix/suffix and credential-like patterns before
writing. Suspicious/oversize output is withheld. Provider request objects, headers,
credentials and reasoning text are never retained. Reasoning presence/character count
and reported reasoning-token count may be recorded when available, not its text.

## Structured output support

The [official Ollama structured-output documentation](https://docs.ollama.com/capabilities/structured-outputs)
explicitly says Cloud currently does not support structured outputs. The
[OpenAI-compatible API page](https://docs.ollama.com/api/openai-compatibility)
contains both Cloud access and local-server examples; its general response-format
listing does not override that Cloud restriction.

For the current Cloud endpoint/model, a JSON-schema/constrained-enum comparison is
**unsupported / not run**. Local Ollama's schema support is not a guarantee about
Cloud. Even HTTP 200 or several valid JSON responses would not prove enforcement.
Generic JSON mode is not a substitute for enum/ID/schema constraints and is not
silently substituted in this experiment. No provider or model switch is made.

## Metrics

Report first-pass request-level schema validity and case-output coverage separately
for each batch size. Semantic primary/secondary accuracy is computed only among valid
outputs, alongside coverage. ERROR remains ERROR and is never counted as a correct NO.
Report first-attempt and one-retry eventual success, retry count/recovery, latency,
request count and reported completion token usage.

Within each batch size, repeated-run consistency compares only cases with valid
first outputs in both rounds and explicitly reports that denominator. Category and
special-case reports include equivalent, broad/narrow, roles, constraints, unknown,
`Origami ← Folding Paper into Shapes`, and both Mercury directions. Retries are not
new independent semantic trials and do not replace first-pass metrics.

## Proposed production error semantics — not implemented

A future validator must fail closed on schema/API/internal ERROR: no semantic candidate
can be admitted based on failed output. Diagnostics must preserve the typed ERROR.
If exact candidates exist, preserve exact-only behavior; if zero exact and infrastructure
fails, return a typed transient failure, not a fabricated semantic NO/no-candidate result.
A valid NO/ABSTAIN is never retried to seek YES. At most one bounded retry, if approved,
shares a total deadline and can only recover transport/schema failures.

No decision here changes production architecture. Any later prompt/precedence change
requires a new frozen independent holdout; these 96 development cases cannot certify it.
Production embedding fingerprint, Neo4j version and coverage/integrity remain unknown.

## Completed results

### Part A freeze / PR

Commit `60d3142b063793039cc7dc88fc6fc1736caba982` contains only the reviewed 11
readiness files (+1634/-19), based on main `88bde51`. It was pushed to
`codex/preference-semantic-quality-gate`; [PR #13](https://github.com/sunnysideup116116-bit/backend/pull/13)
targets main and remains open, not merged. R3.1 work is on the separate
`codex/preference-semantic-r31-reliability` branch and is not in that commit/PR.

PR CI: Matchmaker 119 passed/1 skipped/15 subtests; Contracts 95 passed. Social's
19 failed testcases and Risk's five `google.genai` import failures match clean main
by testcase and failure summary; no branch-only failure was found. Evidence:
[PR run](https://github.com/sunnysideup116116-bit/backend/actions/runs/35690449963),
[clean-main run](https://github.com/sunnysideup116116-bit/backend/actions/runs/35680698020).
No unrelated CI/runtime fix or merge was performed.

### Observed failure mechanism versus historical uncertainty

Exact replay of original third-pass batch 3 reproduced an empty final response with
`finish_reason=length` and `completion_tokens=4096` (equal to unchanged max_tokens),
both initially and after one identical retry. Another replay succeeded with 4030
completion tokens. Original batch 9's two replays both succeeded. These four first
requests required five total attempts: 3/4 first-pass and 3/4 eventual validity.

The matrix independently reproduced batch 3 with the same budget-bound failure.
Its failed attempts had zero final content characters and reasoning fields of
16,931 / 17,432 characters. Only counts were retained, never the reasoning text.
All five invalid matrix attempts hit 4096 completion tokens and had empty final
content; no final JSON existed to validate.

**Established for reproduced attempts:** generation was cut off at the configured
limit before final JSON. This was not an enum, missing-ID, duplicate-ID, or malformed
JSON-body problem. **Strongly supported explanation:** reasoning consumed the shared
generation allowance before a final answer. The provider did not return reasoning
token breakdown (`reasoning_tokens=null`), so do not claim an exact measured number
of reasoning tokens or audited Cloud internals. The
[Thinking documentation](https://docs.ollama.com/capabilities/thinking) distinguishes
reasoning/final fields, and the
[official OpenAI adapter](https://github.com/ollama/ollama/blob/main/openai/openai.go)
maps max_tokens to generation limits; local source is not proof of Cloud deployment.

The exact historical causes of the original two R3 responses remain unproven because
their bodies/finish reasons were not retained. The replay is evidence of a mechanism,
not retrospective reconstruction. A single-case request (`h33-r`) and another
single-case ambiguous request (`h12-f`) also hit the same limit. Therefore batch 8
is not the sole possible cause and smaller batches do not guarantee validity.

### Batch size comparison

Each condition has two fixed-shuffle passes over all 96 cases: 192 case judgments.
The matrix made 360 first requests + 4 retries = 364 requests. All four retries were
caused by empty/truncated schema failure; three recovered, one remained ERROR. No
auth, quota, transport or other provider error occurred. No valid NO/ABSTAIN was retried.
Including the historical replay, this R3.1 experiment made 369 provider requests.

| Cases/request | First requests | First-valid requests | Eventual-valid requests | Valid case outputs first/eventual | Primary accuracy among first-valid outputs | Secondary accuracy among first-valid outputs |
| --- | ---: | --- | --- | --- | --- | --- |
| 1 | 192 | 190/192 (98.96%) | 192/192 (100%) | 190/192 → 192/192 | 96.84% | 93.68% |
| 2 | 96 | 96/96 (100%) | 96/96 (100%) | 192/192 → 192/192 | 98.44% | 92.71% |
| 4 | 48 | 48/48 (100%) | 48/48 (100%) | 192/192 → 192/192 | 98.44% | 92.19% |
| 8 | 24 | 22/24 (91.67%) | 23/24 (95.83%) | 176/192 → 184/192 | 98.30% | 93.18% |

Semantic accuracy is against unchanged human development labels, not a universal
semantic truth. ERROR is excluded from valid-output accuracy and shown in coverage;
it is not silently counted as NO. Eventual primary accuracy was 96.35%, 98.44%,
98.44%, 98.37% respectively: format recovery is not semantic improvement.

| Cases/request | Median first latency | Mean first latency | First latency max | Total requests incl. retry | Mean completion tokens/attempt | Total completion tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 2.66 s | 3.56 s | 30.58 s | 194 | 220.47 | 42,772 |
| 2 | 3.67 s | 4.56 s | 15.31 s | 96 | 377.58 | 36,248 |
| 4 | 5.53 s | 7.02 s | 39.57 s | 48 | 682.67 | 32,768 |
| 8 | 10.08 s | 13.13 s | 31.52 s | 26 | 1,872.92 | 48,696 |

Completion usage is provider-reported generation usage, not just final JSON tokens.
The three concurrent workers and provider load influence latency. Conditions have
different request counts and only two repeats; no formal causal/p95 production claim.

Taxonomy totals: first attempts had 4 empty_response + 4 truncated_response labels
on **four**, not eight, failed requests. All attempts had 5+5 labels on **five**
failed attempts. Invalid JSON, missing/duplicate case, enum, ID, unexpected-field,
provider/API and parser/internal failures were each zero in this run. The taxonomy
is still unit-tested for all those categories; unobserved does not mean impossible.

### Effective-output repeated consistency

Compare only first-pass cases valid in both rounds, with denominator shown:

| Cases/request | Both rounds valid | Same primary | Same secondary |
| --- | ---: | ---: | ---: |
| 1 | 94/96 | 93/94 (98.94%) | 88/94 (93.62%) |
| 2 | 96/96 | 93/96 (96.88%) | 90/96 (93.75%) |
| 4 | 96/96 | 93/96 (96.88%) | 90/96 (93.75%) |
| 8 | 80/96 | 80/80 (100%) | 79/80 (98.75%) |

The 8-case consistency excludes 16 cases lost in failed batches; do not treat its
surviving 100% as better overall stability. Stable wrong answers remain wrong against
the frozen label and are not corrected by schema validation.

- All valid broad/narrow, role/action-consumption and explicit constraint decisions
  were correct in this run; no false YES was observed there.
- Equivalent cases had false NO/primary flips, notably Origami and Stargazing.
- Origami ← Folding Paper into Shapes: NO→YES for batch 1/2/4; NO→NO for batch 8.
  The reverse query stayed YES but sometimes differed between equivalent and
  candidate_specific_satisfies_broader_query. Labels were not changed.
- Observing Planets ← Mercury: NO→NO for batch 1; ABSTAIN→NO for batch 2/4;
  ERROR→ABSTAIN for batch 8. These ambiguity judgments remain unresolved.
- Homonym secondary labels varied among lexical_ambiguity, role_mismatch and
  sibling_related even when primary NO was stable. This suggests a taxonomy/priority
  adjudication need, not a transport retry problem.

### Decision

The reliability investigation establishes a reproducible budget-exhaustion mechanism
and a useful provisional batch-2/4 direction. It does **not** authorize changing
production batch size, token budget, thinking control, threshold or prompt.

Recommended next offline work separates two issues:

1. Reliability: compare explicitly bounded generation/thinking controls or a provider
   with confirmed constrained output, on a separately approved protocol. Do not
   assume one identical retry resolves a hard output-budget problem.
2. Semantics: independent human adjudication of equivalent-versus-broader and
   ambiguous/unknown precedence is justified by the valid-output flips. It is worth
   proceeding to **offline contract/prompt-refinement design**, not production
   implementation. Do not relabel this run to improve accuracy.

If contract/prompt is changed, the current 96 cases remain development-only and a
new independently frozen holdout is mandatory. The original prompt/contract/labels
and all earlier evidence remain unchanged. Cloud schema-constrained comparison is
unsupported/not run; valid plain JSON is not proof of provider enforcement.

No R3.1 changes were committed or pushed into PR #13. Production flags stay OFF;
production Graph, deployment, backfill and P1-B remain untouched.

Final checks: 128 tests + 30 subtests passed (one existing multipart deprecation
warning), including 28 new hermetic taxonomy/runner tests. Syntax, shell syntax,
secret/no-vector scans, frozen prompt/contract/label hashes and commit-separation
checks passed. R3.1 leaves five new uncommitted readiness files; the pushed quality
gate branch still points exactly to `60d3142`.
