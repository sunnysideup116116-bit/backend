# R3.2c offline contract v3 — full input and relation-only classification

Production STOP. No P1-A runtime, Graph, identity, embedding, .82 threshold or
feature-flag change. This contract is a new offline experiment, not a modification
to frozen v2. Freeze prompt/contract/tooling/input hashes **before** the first call;
do not tune them after seeing outputs. There is one default-reasoning configuration,
not a prompt sweep. No R3.3 until the complete proposed gate passes.

## Input fidelity

Read the original synthetic `r3_holdout.json` (frozen SHA-256
`0943c22f0f3aa4e9091644f811161ca8342fa00838359f2ded9239cf303b70e7`).
Expand its 48 pairs into 96 directional cases. Preserve exact full Q/C strings:
no canonical-label truncation, no generated synonyms, no query-specific prefix,
no semantic preprocessing. Each string must be nonempty and at most 120 Unicode
characters, as in the original fixture contract. Oversize/incomplete input fails
preflight; never silently slice. Send only ID/Q/C, not expected annotations.
This is not a production canonicalization change; keys are not created or written.

The original primary gold labels and fixture bytes remain unchanged. The dataset
is development-only. A result cannot be claimed to generalize to a fresh holdout.

## Versioned relation ontology and annotation projection

The model returns **only** `id` and `relation`. It is not asked for YES/NO/ABSTAIN.
Code uses this exact user-approved deterministic mapping:

| Relation | Mapped primary |
|---|---|
| equivalent | YES |
| candidate_more_specific | YES |
| candidate_more_broad | NO |
| sibling_related | NO |
| role_mismatch | NO |
| constraint_conflict | NO |
| lexical_ambiguity | ABSTAIN |
| unrelated | NO |
| unknown | ABSTAIN |

No secondary field may override the map; extra fields (including a model-supplied
`decision`) invalidate the whole batch. Invalid schema/enum, missing/duplicate IDs,
truncation or API/parser failure is ERROR, not NO or ABSTAIN. ERROR fails closed.

The historical v2 enum `lexical_ambiguity` meant **two already-clear but different
senses**, with primary NO. The requested v3 enum means **unresolved sense**, mapped
ABSTAIN. Therefore comparing raw old/new enum strings would be misleading.
Before scoring, project the historical annotations into the new ontology:

- `candidate_specific_satisfies_broader_query` → `candidate_more_specific`.
- `candidate_broader_insufficient` → `candidate_more_broad`.
- Old clear-domain `lexical_ambiguity` (homonym cases, original NO) → `unrelated`.
- Old `unknown` (bare ambiguous word cases, original ABSTAIN) → `lexical_ambiguity`.
- All other expected relation names stay unchanged.

Keep `source_relation` alongside the projected gold relation. Assert that mapping
the projected gold produces the **unchanged** original primary for every case.
This is an explicit prescoring annotation-version conversion, not a relabeling to
fit model results. Report relation accuracy against v3 projected annotations and
mapped-primary precision/recall against original primary annotations separately.

## Fixed run and failure handling

- Ollama Cloud `deepseek-v4.1-flash:cloud`, default reasoning (omit effort field).
- Temperature 0 sent; max_tokens 4096; batch 2; client timeout 60s; SDK retries 0.
- Three identical R3.2b shuffle seeds (9401, 9402, 9403), 144 first requests,
  three workers; hard maximum 288 attempts with at most one identical retry.
- Retry only malformed/empty/truncated output or transient provider/transport
  failure; never retry a valid relation because its mapped result is NO/ABSTAIN.
- No structured-output parameter, embedding/provider switch or budget experiment.
- Credentials: only existing allowlisted LLM fields in process memory. No key
  value/fragment, headers, exception text or reasoning text in reports/logs.
- Save only ignored synthetic experiment artifacts; no vectors or production data.

Report first/eventual validity, finish reasons, median/p95 latency, token usage,
relation accuracy, mapped precision/recall, primary and relation consistency,
directional/role/constraint/ambiguity mistakes and per-round results. Missing/error
cases fail consistency; ERROR never quietly becomes NO in offline metrics.
Pooled repeats are dependent observations, not three independent datasets.

## Proposed engineering gate

All required: first-pass validity >=99%; eventual sample validity 100%; mapped YES
precision >=99%; all-96 primary consistency >=99%; all-96 relation consistency
>=99%; broad-candidate→specific-query false YES =0; role false YES =0; constraint
false YES =0. Report recall/accuracy even though no minimum is specified for them.
No predicted YES gives undefined precision, not a pass. Do not round up thresholds.

If any gate fails, STOP: no further prompt polishing or R3.3; reconsider whether
semantic fallback merits its complexity. Passing only permits considering a new
frozen R3.3 holdout, never production activation.

This experiment changes **both** input fidelity and output-task design compared
with R3.2b. It does not by itself isolate their individual causal contributions.
Deterministic mapping removes model discretion over policy, but cannot repair a
wrong relation classification. It never establishes an alias, MERGE, PREFERS edge,
owner fact or confirmed shared preference.
