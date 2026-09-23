# R3.4 development results and candidate freeze

Prospective protocol committed at b82dc708c536a69c7cfa94d1f19b127fa7e2d03f.
R3.3 remains permanently FAIL at 74a942206e2c6b64260cbbe7f69af89af2255665.
Its 192 cases were not used for selection or prompt tuning.

## Provider budget confirmation

[Official Ollama documentation](https://docs.ollama.com/api/openai-compatibility)
lists max_tokens; it does not establish a model-specific output maximum.
Eight predefined development-only probes used unchanged relation-only prompt/
default reasoning. Seven 8192-token requests were accepted; observed completions
included 4347 and 8192 tokens (the latter finish=length). A 64-token sentinel
stopped at 64. Thus 8192 support was observed, not inferred from context size.
Global output maximum and pinned model revision remain unknown.

## Four-arm development matrix (96 cases, three rounds each)

All four arms: first/eventual validity 100%; precision/recall 100%; binary
consistency 96/96; broad/role/constraint false acceptance 0; ERROR/fail-open 0.
Each arm has TP84/FP0/FN0/TN204 over 288 case observations. No retries occurred.
All 864 provider responses matched deepseek-v4.1-flash:cloud.

| Arm | Requests/attempts | finish_reason | length | Median/p95 batch latency | Completion tokens total (median/p95) |
|---|---:|---|---:|---:|---:|
| batch2 / 4096 | 144/144 | stop144 | 0 | 1.8332 / 6.4668s | 57524 (254.5/1161) |
| batch1 / 4096 | 288/288 | stop288 | 0 | 1.0717 / 3.3293s | 47525 (103/468) |
| batch2 / 8192 | 144/144 | stop144 | 0 | 1.7069 / 5.8006s | 54753 (244.5/916) |
| batch1 / 8192 | 288/288 | stop288 | 0 | 1.0352 / 3.9447s | 51012 (104/470) |

First/eventual latencies coincide except negligible bookkeeping (no retries).
Latency is measured per batch, not per case or a production p95 guarantee.
No false accepts or false rejects in any development arm. Independent arithmetic
verification agrees on all counts and consistency denominators.

## Selection and important limitation

All pass the user gates and the predeclared non-regression comparison.
The fixed simplicity/throughput order selects **b2_t4096**, the unchanged baseline.
Do not re-rank after seeing scores. This matrix **does not demonstrate that
generation reliability has improved**, nor that the old failure is repaired.
Grouping/seed and stochastic provider behavior can affect rare truncations.
The budget probe itself still produced length at 8192. Development PASS is not
final acceptance; do not retroactively change R3.3 FAIL.

The selected configuration and new final protocol are in r34_selected_config.json.
Freeze this candidate BEFORE creating any new final-holdout fixture.

## Prospective fresh final evaluation

Create 80 entirely new pairs /160 directional synthetic cases, three rounds.
Ten categories,16 cases each, include equivalent, broad/narrow, sibling, role,
action/consumption, constraint, ambiguity, unrelated, composite and long-prefix
qualifiers. EN–EN/ZH–ZH/EN–ZH/ZH–EN each40. At least8 pairs share first40 codepoints
but differ in material later qualifiers. Bound complete semantic input at500.
Labels pre-authored under user authorization, NOT independently human reviewed.

Only after this candidate freeze may old R3.3 pair identities be read for a
deterministic overlap-exclusion audit; no R3.3 labels/scores select this config.
Do not send retired R3.3 cases to the model again.

Keep all nine prospective R3.3 metric thresholds/definitions: precision>=99%;
recall>=95%; binary consistency>=99%; first/eventual schema validity>=99%;
broad/role/constraint false acceptance zero; ERROR fail-open zero.
Relation/NO↔ABSTAIN differences diagnostic only. At N=160 and batch2/3 rounds:
480 case observations,240 first requests,max480 attempts. Exact ratios determine
PASS; no rounding, dropped cases or extra retries. Any blocking failure STOPs.
The old R3.3 gate/labels/config/results remain unchanged forever.

## Integrity / scope

Frozen v3 prompt/parser/mapping/evaluator reused; only per-call max_tokens and
job batch size varied. The adapter does not change reasoning or retry predicate.
35 initial tests and 195 affected regressions +87 subtests passed. Compile/shell/
diff checks pass. GitNexus staged scope LOW, five readiness-only files; global/
dynamic index limitations supplemented by source import review.
No runtime change, production Graph, deployment, migration or P1-B.
Both semantic flags OFF, .82 unchanged, production fingerprint unknown.
No proof of production readiness.

## Ignored evidence hashes

- budget_probe.json: bd96b96c5407a4bda173076f6857938c319ed7590fb87e43aaafcce5324d476d
- manifest.json: 2e3cb829c506044e811b26d699bfaaebc0fa86c8e253d3b2314460f408887c94
- matrix.json: 3e6561a71f1acf8aa412a3b9a4af0b2c3be4b2108bc493063bd0d0c851439ebb
- summary.json: a21348bd8392f8988b70e6e177229fa366909c0e5ba90ed400154ac591cb0c3f
- independent_verification.json: ac796d1f7b0bc1c5c8a648fe413bbd039f8a811af1daa33db1b1f1de4e4ba08d

Paths under artifacts/r34/. Generated reports/provider outputs/secrets are ignored.
