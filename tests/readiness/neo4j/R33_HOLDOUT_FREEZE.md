# R3.3 holdout / tooling freeze (before scoring)

The prospective gate was committed first:
`3ee4d91395c2f4648f02bd107f016f33651bf819`.
The prior post-P0.1 development evidence is separately committed at
`6a2505b`. No model call has been made on this R3.3 holdout at this freeze.

## Dataset

- 96 completely new synthetic pairs / 192 directional cases.
- Twelve categories, 16 directional cases each: equivalent, broad_narrow,
  sibling, role, action_consumption, constraint, ambiguity, unrelated,
  composite_directional, long_prefix_constraint, long_prefix_role,
  multilingual_equivalent.
- Languages: en-en 48, zh-zh 48, en-zh 48, zh-en 48.
- Frozen primary labels: YES 48, NO 128, ABSTAIN 16.
- New labels were agent-authored and contract-reviewed before scoring under
  explicit user authorization; **NOT independently human reviewed**. The evaluated
  provider did not create, approve or adjust labels.
- Raw Simplified/Traditional Chinese and cross-language paraphrases are included.
  Fourteen source descriptions undergo the pinned fresh normalization. Raw
  sources and complete normalized provider inputs are both retained.
- Source and semantic maximum length: 98 Unicode code points, bounded by 500.
  No display text/prefix truncation is used.
- Sixteen pairs (u073–u088) deliberately share the first 40 code points after
  normalization while differing in later constraints or participation roles.
  Full canonical keys are distinct. Reverse directions yield 32 such cases.
- Canonical directional-pair overlap with old 96-case development/R2.5 is zero
  under both pure and fresh v2 identity, checking both pair directions.
- Some broad topic families naturally recur (arts, making/consuming, access
  constraints). “Unseen” refers to held-out experiment pairs, not a claim that
  general topics or phrases never occurred in model pretraining.

Gold annotations include a pre-score rationale. No labels/categories/rationales
are sent to the provider. Normalization is fixed before scoring; it does not
alter directional gold labels or choose relations.

## Fixed execution

Exactly the prospective gate in R33_ACCEPTANCE_GATE.md / r33_gate.json applies.
No relaxation based on these future scores. Three rounds, seeds 330301/330302/
330303, schedule seed 330300; 288 batches, at most 576 attempts. Same default
reasoning, temperature 0, max_tokens 4096, batch 2 and existing retry predicate.

The new runner calls the unmodified R3.2c relation_request and
evaluate_relation_batch. Only its ignored output directory is redirected.
Existing prompt/parser/mapping/retry sources retain their gate-pinned hashes.
No SDK switch, production import, Graph/embedding request or real proposal.

r33_current requires frozen files committed and unchanged before prepare/run.
The generated pre-scoring manifest records the eventual freeze commit plus all
source/input/schedule hashes. It cannot be overwritten; incomplete/changed runs
cannot be silently replaced. Provider wire hashes enforce the exact planned
synthetic requests; endpoint and attempt limits fail closed.

## Pre-scoring tests and scope review

148 hermetic tests + 72 subtests passed, including new gate arithmetic,
diagnostic-only NO↔ABSTAIN changes, zero-tolerance safety classes, 99% validity
boundary, invalid-output acceptance detection, no valid-result retries, complete
input fidelity, pair novelty, deterministic schedule and frozen-source hashes.
compileall, bash -n start_all.sh and diff checks pass.

GitNexus incremental indexing produced a spurious CRITICAL cross-runtime mapping
for the new metrics symbol. A full forced index rebuild corrected its symbol UID.
Its dynamic/module-qualified callers remain UNKNOWN to impact traversal; direct
source checks resolve references only in the new offline runner/tests. Runner
context resolves the existing frozen evaluator/config loader/report helpers.
No production source imports the new r33 modules. Index-wide process/call caps
remain a limitation; no zero-flow result is treated as exhaustive proof.

New files are readiness-only: fixture, loader/novelty checks, metric calculator,
bounded runner, tests and this freeze note. No old prompt/fixture/mapping/runtime
file was edited. Generated artifacts, provider outputs, vectors, credentials and
local environment files are excluded from commits. No push/deploy.

## Source hashes bound before scoring

| File | SHA-256 |
|---|---|
| r33_holdout.json | affedd59406975620e318e9e178d884b253c6ff9b69ae79082679c0f96193bf5 |
| r33_gate.json | 01c04e12d1345ae2dc70296a9aa25d244f87c68fa53ac6892b929888c7737990 |
| R33_ACCEPTANCE_GATE.md | 7493c6b76993282399d0e34412e591f242373b20a30d89370659dd87989647da |
| r33_data.py | 5e77882f8539f38c23e9d4ce681a67ef337cc58934463f128c0b3638df570c04 |
| r33_metrics.py | ddfa7414083f0280caecc4e945c8489917dc8537e4a41e99860b7ac721fba707 |
| run_r33_offline.py | b53a216a93ce4639b1b19d4346007a902ee002256c2cfc7e745ccdeb6c72fdb0 |
| test_r33_offline.py | 8528f1495a3d54e26bc7467c3344de59445fe0b5a37341ee424bd8c0092b65fa |

Frozen reused dependency hashes are in r33_gate.json (committed before fixture
creation), including the full identity module and v3 prompt/parser/retry helpers.
Runtime semantic flags remain OFF, .82 unchanged, production fingerprint unknown.
