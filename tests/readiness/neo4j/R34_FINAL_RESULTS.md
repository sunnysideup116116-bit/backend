# R3.4 reliability hardening — development PASS; fresh final FAIL / STOP

Completed 2026-09-23. No runtime integration, deployment, production Graph,
migration, backfill, P1-B, prompt/semantic tuning or post-result retry.
R3.3 remains permanently FAIL, with its original gate/config/labels unchanged.

## Evidence sequence

- R3.3 permanent FAIL commit: 74a942206e2c6b64260cbbe7f69af89af2255665.
- R3.4 development protocol/tools: b82dc708c536a69c7cfa94d1f19b127fa7e2d03f.
- Selected config + prospective fresh final gate, before new fixture creation:
  132cba404b4ac8294308f22e93aa5573d54de23c.
- New final holdout/labels/tools before scoring:
  d84554312ebf281870b9cee2506656a42ad7327b.

Only readiness files, all local/unpushed. Generated provider artifacts and
credential values remain ignored. The R3.3 cases/scores were not used to select
configurations or tune the prompt. After candidate freeze, old pair identities
were read solely for deterministic non-overlap verification.

## Development matrix and provider support

Full protocol/results: [R34_CANDIDATE_FREEZE.md](R34_CANDIDATE_FREEZE.md).
The official [Ollama API documentation](https://docs.ollama.com/api/openai-compatibility)
lists max_tokens. Eight bounded old-development-only probes confirmed the actual
endpoint accepts 8192 and can emit more than4096 tokens: observed4347 and8192
completion tokens, the latter finish=length. Global/model revision and maximum
output limits remain unknown; larger context window was not used as proof.

Unmodified relation-only prompt, mapping, default reasoning, temperature0,
one-permitted-retry semantics. Only batch size and generation budget varied.
96 old development cases,3 fixed rounds per arm; no R3.3 inputs.
Every arm had first/eventual validity100%, precision/recall100%, binary
consistency96/96, and broad/role/constraint false acceptance0. All864 first
requests succeeded with stop; no retries or length failures in the matrix.

| Arm | Requests | First / eventual | Precision / recall / consistency | Median / p95 latency | Completion tokens total (median/p95) |
|---|---:|---|---|---|---:|
| batch2,4096 |144|100% /100%|100% /100% /100%|1.8332s /6.4668s|57524 (254.5/1161)|
| batch1,4096 |288|100% /100%|100% /100% /100%|1.0717s /3.3293s|47525 (103/468)|
| batch2,8192 |144|100% /100%|100% /100% /100%|1.7069s /5.8006s|54753 (244.5/916)|
| batch1,8192 |288|100% /100%|100% /100% /100%|1.0352s /3.9447s|51012 (104/470)|

Independent arithmetic agrees. Fixed prescoring preference order selected
**batch2 +4096**, the unchanged baseline. No causal reliability improvement was
demonstrated; this selection was not a claim that the known truncation mechanism
was fixed. Do not retroactively select another arm using the final scores below.

## New final holdout

160 directional cases from80 wholly new synthetic pairs; three rounds/480
observations. Categories: equivalent, broad_narrow, sibling, role,
action_consumption, constraint, ambiguity, unrelated, composite, long_prefix:
16 cases each. EN–EN/ZH–ZH/EN–ZH/ZH–EN:40 cases each.
Gold primary labels:32 YES,112 NO,16 ABSTAIN.

Labels were authored and fixed before scoring under user authorization;
**NOT independently human reviewed**. No evaluated-provider label generation.
Original descriptions and exact full normalized provider Q/C were frozen.
Maximum semantic input119 codepoints, bounded500; no truncation/display label.
Eight pairs deliberately share first40 codepoints but differ in later conditions/
roles. All full v2 keys differ. Pair overlap with R2.5, development and retired
R3.3 is0 (both pure/fresh canonical forms, both directions).

Model deepseek-v4.1-flash:cloud; OpenAI SDK1.30.1/HTTPX0.28.1. Default reasoning
omitted, temperature0, batch2,max_tokens4096,timeout60,SDK retries0,workers3.
Seeds345101/345102/345103; global schedule345100.240 first requests,max480
attempts. Actual243 calls (three permitted retries). All243 model names matched;
provider revision/reasoning-token split not exposed. No Graph/embedding/proposal
calls. Only synthetic ID/Q/C sent; no labels, real users or raw memories.

## Final blocking gates

Binary policy: YES accept; NO/ABSTAIN/ERROR reject. Errors retained separately.
Exact fractions, not rounded percentages, determine PASS/FAIL.

| Gate | Result | Required | Verdict |
|---|---:|---:|---|
| Acceptance precision |93/93=100%|>=99%|PASS|
| Acceptance recall |93/96=96.875%|>=95%|PASS|
| Binary repeated-run consistency |158/160=98.75%|>=99%|**FAIL**|
| Broad C→specific Q false accept |0/48|0|PASS|
| Role/action-consumption false accept |0/120|0|PASS|
| Constraint false accept |0/72|0|PASS|
| First-pass schema validity |237/240=98.75%|>=99%|**FAIL**|
| Eventual validity |238/240=99.1667%|>=99%|PASS|
| ERROR fail-open |0|0|PASS|

**Final=FAIL.** Need at least159/160 consistent cases and238/240 first-valid
batches; achieved158 and237. No rounding or waiver. Independent recomputation
matches every gate and confusion count. Its verification PASS is arithmetic
agreement, not experiment PASS.

| Round |TP|FP|FN|Binary TN including rejected ERROR|ERROR observations|
|---|---:|---:|---:|---:|---:|
|1|32|0|0|128|0|
|2|31|0|1|128|2|
|3|30|0|2|128|2|
|Total|93|0|3|384|4|

Original semantic metrics retain TN380+ERROR4 separately. All four ERRORs were
negative/ambiguous cases and fail closed. Neither binary inconsistency nor the
three false rejects were caused by ERROR: they are valid model NO decisions.

## Complete false acceptance/rejection list

**False accepts: none.**

**False rejects: exactly two unique cases /three observations:**

| Case | Q | C | Frozen gold | Round1 /2 /3 |
|---|---|---|---|---|
|v002-r|Studying Morse Code|Learning Morse Code|equivalent→YES|equivalent→YES /equivalent→YES /candidate_more_broad→NO|
|v071-f|製作親子野餐料理|Preparing Nut-Free Vegetarian Family Picnic Food|candidate_more_specific→YES|candidate_more_specific→YES /sibling_related→NO /sibling_related→NO|

Both are reported against the prescoring agent-authored gold, not independently
human-adjudicated semantic truth. Labels were not changed after seeing results.
The two binary flips fail the prospective consistency gate regardless of whether
future independent human review questions a label.

## Schema ERRORs / retry recovery

Three first attempts were empty final output +length at4096 tokens. Each received
exactly one retry; one recovered, two exhausted the permitted retry. No third
attempts or larger-budget rescue. Six first ERROR observations; four eventual
ERROR observations. finish_reason across243 attempts:stop238,length5.
No auth/quota/transport failures; diagnostics never reclassify ERROR as NO.

| Batch | Cases | First /retry | Eventual |
|---|---|---|---|
|r34final-r3-b042|v051-r,v010-r|length4096 /length4096|ERROR/reject|
|r34final-r1-b080|v050-r,v049-r|length4096 /stop3338|valid/reject|
|r34final-r2-b004|v053-r,v060-r|length4096 /length4096|ERROR/reject|

No invalid batch salvaged any decision. First case coverage474/480; eventual
coverage476/480. Prefix challenge rows remain complete; no source truncation
occurred. Errors are generation failures, not silently shortened Q/C.

## Relation diagnostics (non-blocking)

Relation accuracy468/480=97.5% overall,468/476=98.3193% among valid output.
Relation consistency152/160=95%; three-way primary consistency154/160=96.25%.
These do not add blocking gates. Binary consistency above remains blocking.

Gold ambiguity:43 ABSTAIN,3 NO,2 ERROR; all48 rejected.
All relation-disagreement/error cases:

- v002-r: equivalent gold, round3 candidate_more_broad/NO.
- v010-r: candidate_more_broad gold, round3 ERROR.
- v017-r: sibling_related gold, round3 unrelated/NO (still reject).
- v051-r: lexical_ambiguity gold; role_mismatch/NO, sibling_related/NO, ERROR.
- v053-r: lexical_ambiguity gold; ABSTAIN, ERROR, role_mismatch/NO.
- v060-r: unrelated gold; round2 ERROR.
- v071-f: candidate_more_specific gold; YES, sibling_related/NO, sibling_related/NO.
- v071-r: candidate_more_broad gold; sibling_related/NO then NO/NO.

## Final latency / usage

At concurrency3, per-batch wall time excludes queue wait and is not a production
latency guarantee. First/eventual median1.8096s,p95 10.7185s.
Eventual max45.1316s. Completion tokens median282,p95 2455,mean509.9095,
max4096,total123908. Prompt tokens149992,total273900.
Token/latency totals include failed attempts and retries.

## Disposition and scope

STOP. No runtime integration or automatic provider/model substitution. Do not
rerun this final holdout with another arm or tune the prompt against its failures.
A future independent reliability/provider investigation needs its own protocol
and new validation data; no such experiment was started here.

The development matrix did not distinguish the four configs on binary/schema
metrics, while the fresh final exposed both truncation and binary instability.
Do not describe R3.4 as a completed generation-reliability fix. R3.3 remains
permanent FAIL; R3.4 final is separately FAIL.

Combined hermetic suite:201 passed,87 subtests; one existing multipart
deprecation warning. compileall/bash-n/diff/frozen-hash/secret checks pass.
GitNexus full-rebuild staged scope LOW for the new readiness code. Dynamic/
global index limits remain; explicit imports show no runtime adoption.
All commits are readiness-only. This final results document is left uncommitted
for review; generated reports/raw outputs/secrets remain ignored.

Both flags OFF, .82 unchanged, production embedding fingerprint unknown.
No deployment, Graph/migration/backfill, P1-B, production code change or
DatingApp #42 merge.

## Final hashes (generated files ignored)

- selected config:6dd3806ee09f72eb4f9e0a3271fb2c0755a0d22e25a57af87dd1a6cfcb57421a
- holdout:17f0282dc6c54e03250b6177e88bc41e48980806981a8e39522f7af93d382c0f
- artifacts/r34_final/manifest.json:485b7d41e1cf7fcf2ac4eb7c56d81737159b535c3034cbb6b344599545375f64
- artifacts/r34_final/matrix.json:ead613d8e00a7c969294f1a079bb052a3916858fcc9c45b4a3196d5a33fa05c8
- artifacts/r34_final/summary.json:c82d340e95c03cbcdb2a4efd1262441bf293fc25e10882c231a51537e9c7375e
- artifacts/r34_final/independent_verification.json:c82babba68a22ef5c1deda74346513b33d5b81087149112959d256501bfb8449
