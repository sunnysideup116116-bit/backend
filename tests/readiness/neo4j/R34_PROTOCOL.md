# R3.4 prospective reliability hardening protocol

R3.3 is permanently FAIL at commit 74a942206e2c6b64260cbbe7f69af89af2255665.
Its 192 cases, scores and labels MUST NOT select configurations or tune prompts.
No retired R3.3 case will be sent to the provider in this experiment.

Only independent variables: batch size (1/2), max_tokens (4096/higher).
Higher candidate proposed = 8192, subject to a bounded provider contract probe.
Do not infer output-token limits from the model's 1M context window.

Fixed: original 96-case development set, frozen full Q/C/labels, relation-only
v3 prompt, deterministic relation→acceptance mapping, default reasoning (effort
omitted), temperature 0, timeout 60 seconds, SDK retries 0, three workers.
Retain exact R3.2c retry implementation through a per-call client adapter that
changes only max_tokens. At most one eligible ERROR/transient retry; never retry
valid NO/ABSTAIN. No semantic contract change or provider/model substitution.

## Provider budget gate

Official documentation lists max_tokens on /v1/chat/completions:
https://docs.ollama.com/api/openai-compatibility
Model page: https://ollama.com/library/deepseek-v4.1-flash
Neither page establishes a model-specific maximum output budget.

Probe uses the frozen relation-only prompt on preselected old development cases;
no labels/decisions are retained, no semantic scoring/selection. Eight calls:
one 64-token sentinel and seven requests at 8192, fixed before calls.
Higher arm requires all high requests accepted without API/contract failure,
requested model equality, and at least one reported completion >4096 tokens
(to rule out an observed 4096 clamp). Otherwise STOP higher-budget evaluation as
unconfirmed; do not invent support or silently substitute another budget.
This confirms observed support, not a pinned revision or universal maximum.

## Matrix and bounds

On confirmation compare all four arms:
b2_t4096, b1_t4096, b2_t8192, b1_t8192.
Three fixed rounds/seeds 340101/340102/340103; same case shuffle per round across
arms, global schedule seed 340100; 96 cases/arm/round.
Batch-2: 144 first requests per arm. Batch-1: 288 per arm.
Total 864 first requests, hard maximum 1728 attempts (separate from 8 probes).
All requests synthetic-only; no Graph, embedding, profile or proposal operations.
First-pass errors are preserved even when retry recovers.

For each arm report first/eventual batch validity, finish reasons, length errors,
first/eventual median/p95 latency, completion tokens, requests, binary acceptance
precision/recall/consistency and broad/role/constraint false acceptance.
YES accepts; NO/ABSTAIN/ERROR reject. Errors stay separately reported.
Precision/recall denominator includes all 288 case observations per arm.
Consistency denominator is all 96 cases; relation-class/NO↔ABSTAIN differences
are diagnostics only. Use exact integer comparisons; never round gate results.

## Prospective selection

Eligible only if all user gates pass: first/eventual validity >=99%;
acceptance precision>=99%; recall>=95%; binary consistency>=99%;
broad→specific/role/constraint false acceptance zero; ERROR fail-open zero.
Additionally, precision/recall/consistency must be no lower than contemporaneous
b2_t4096 baseline (semantic non-regression). Undefined precision is not PASS.

Among eligible arms use fixed simplicity/throughput preference order:
b2_t4096, b2_t8192, b1_t4096, b1_t8192.
Do not pick an arm by R3.3 scores or alter this order after seeing results.
If none qualify, STOP and assess provider/model alternatives only as a next
separately authorized step; no prompt polishing.

If one qualifies, commit/freeze candidate config, hashes and selection evidence
BEFORE authoring a new unseen final holdout. Never reuse R3.3 for final acceptance.
The new final gate retains the nine prospective R3.3 metric definitions and
thresholds, adapted only to the newly frozen sample size/batch configuration.
Labels are pre-authored under user authorization, not independently human reviewed.
No real final-holdout scoring until its labels/source/schedule are frozen.

## Evidence / boundaries

Freeze old prompt/parser/retry/development hashes. Generated provider reports,
raw outputs, vectors, keys/local env never enter Git. Only allowlisted LLM fields
are read into memory; no whole .env loading. Secret-safe logging and HTTPS
Ollama endpoint/budget allowlist, no redirects, no automatic repeat runs.
Keep all failed cases. No changed labels, extra retries or early semantic pruning.

Even a final PASS does not enable runtime integration. Both semantic flags OFF,
.82 unchanged; no deployment, production Graph, migration, backfill, P1-B or
DatingApp #42 merge. Historical production embedding fingerprint stays unknown.
