# R3.2c full-input relation-only results — STOP

Completed 2026-09-22. This is one offline experiment on the **96-case development
set**, not a fresh holdout or production validation. No prompt sweep, relabeling,
post-result case exclusion, threshold change or production integration occurred.
The agreed gate fails; do not enter R3.3 or continue prompt polishing.

## A. R3.2b evidence frozen first

Independent local commit: `12f738848eb8f983997196d58727e3132c0910ba`

`test(readiness): freeze reasoning directionality isolation evidence`

Seven reviewed readiness files, +1,487 lines; no generated reports, provider
outputs, vectors or credentials. R3.2c started afterwards on separate branch
`codex/preference-semantic-r32c-fidelity`. No push was performed. The R3.2b
pre-freeze report remains a historical snapshot; its findings were not rewritten.

## B. Input fidelity finding

The full trace, synthetic loss/collision probes, production-code boundaries and
future-only correction design are in [R32C_INPUT_FIDELITY.md](R32C_INPUT_FIDELITY.md).

The source is `matchmaker_agent/concept_identity.py::_clean_label`:
`return text[:40]`, a hardcoded slice **before** canonical key construction.
Additional 40-character cuts occur in extraction and storage boundaries; 60/500
character embedding limits cannot recover lost text. It is not UI-only.

Synthetic long inputs lose accessibility, wheelchair/step-free, no-guided-tour,
vegetarian/alcohol-free and participation-role qualifiers. Prefix collisions
were reproduced for vegetarian/alcohol-free, watching/playing, reading/writing.
Short role/constraint controls remain intact. These are deterministic text-loss
defects, not failures of embedding similarity.

Production durable/exact **source paths** use this code even with semantic mode
OFF. P1-A query embedding would also receive the shortened label when enabled.
ANN currently returns keys/scores and has **no production relation-validator
step**. We did not inspect deployed records or production configuration/Graph;
actual affected record counts and deployment-specific state remain unknown.

No production correction was made. A minimal separate design must separate full
bounded identity/semantic text from display clipping, reject oversize input rather
than silently slice, reconcile all writer/worker caps and version key/provenance
compatibility. Do not guess lost suffixes, silently rename/MERGE old Concepts or
reuse embeddings generated from incomplete text. `_clean_label` upstream impact
is CRITICAL; a future fix needs its own review and authorization.

## C. Experiment fixed before scoring

- New versioned [v3 contract](R32C_CONTRACT.md) and [relation-only prompt](r32c_prompt_v3.txt).
- Raw synthetic Q/C preserved exactly, bound 120 characters with rejection on
  overflow; longest actual fixture input 46. No canonicalization/truncation.
- Original 28 YES / 60 NO / 8 ABSTAIN primary gold labels unchanged; source fixture
  bytes unchanged. Projected relation gold and `source_relation` are both retained.
- V3 lexical_ambiguity means unresolved word sense and maps to ABSTAIN. Historical
  clear-but-different-sense homonyms therefore project to unrelated (still NO);
  historical bare-word ambiguity projects to lexical_ambiguity (still ABSTAIN).
  This conversion was declared before scoring, not inferred from model outputs.
- Model returns `{id, relation}` only. Extra model decision/secondary fields are
  schema errors. Strict code mapping is rechecked independently in metrics.
- Ollama Cloud `deepseek-v4.1-flash:cloud`, installed OpenAI SDK 1.30.1, omitted
  effort/default reasoning, temperature 0 sent, batch 2, max_tokens 4096, timeout
  60 seconds, three workers, SDK automatic retries disabled.
- Same seeds 9401/9402/9403 as R3.2b; 144 scheduled first requests, hard maximum
  288 attempts. At most one identical retry for eligible schema/transient failure;
  no retry for valid mapped NO/ABSTAIN or unexpected finish such as content_filter.
- Credentials selected into memory from only approved LLM config fields. No
  production user data, Graph access, embeddings, proposals or writes. No key,
  fragment, headers, raw exceptions or reasoning text saved. Generated outputs
  remain under ignored `artifacts/r32c/`; no generated vectors exist in this run.

### Deterministic mapping

| Relation | Code-owned primary |
|---|---|
| equivalent; candidate_more_specific | YES |
| candidate_more_broad; sibling_related; role_mismatch; constraint_conflict; unrelated | NO |
| lexical_ambiguity; unknown | ABSTAIN |
| Schema/API/parser ERROR | ERROR, reject/fail closed |

Every final decision in the report was re-derived from the relation; none came
from a model acceptance field. No model output can override this policy.

## D. Results

All 146 actual responses matched the requested model name. The provider did not
expose a pinned model revision or reasoning-token breakdown. Two first attempts
finished `length` at 4,096 completion tokens with empty final content. Both were
retried exactly once and succeeded; no auth, quota or transport failures occurred.

| Metric | Result |
|---|---:|
| First-pass schema validity | 142/144 = **98.6111%** |
| Eventual schema validity | 144/144 = **100%** |
| Calls / retries | 146 / 2 |
| finish_reason, all attempts | stop 144; length 2 |
| First-case coverage / eventual coverage | 284/288; 288/288 |
| Eventual ERROR | 0 |
| Relation accuracy, eventual | 284/288 = **98.6111%** |
| Mapped primary accuracy, eventual | 284/288 = 98.6111% |
| Mapped TP / FP / FN / TN | 84 / 0 / 0 / 204 |
| Mapped YES precision / recall | **100% / 100%** |
| Primary repeated-run consistency | **93/96 = 96.875%** |
| Relation repeated-run consistency | **93/96 = 96.875%** |
| Broad candidate → specific query false YES | **0/36** |
| Specific candidate → broad query false NO | **0/36** |
| Any directional primary or relation errors | **0/72** |
| Role/action-consumption false YES | **0/48** |
| Constraint/opposite false YES | **0/24** |
| Gold ambiguity handling | **24/24 ABSTAIN** |
| First latency median / p95 | 1.7111s / 5.0992s |
| Eventual latency median / p95 | 1.7111s / 5.0992s |
| Eventual latency max | 44.3549s |
| Completion tokens median / p95 / mean | 248 / 1,139 / 426.9452 |
| Completion tokens max / total | 4,096 / 62,334 |

Latency uses per-request wall time at concurrency three and nearest-rank p95;
it is not a production latency guarantee. Tokens include failed attempts/retries.
Repeated decisions are dependent development observations, not 288 independent
holdout samples. Precision 100% in this small set does not prove production safety.

### All eventual errors and instability

| ID | Q ← C | Projected gold relation → primary | Round 1 / 2 / 3 |
|---|---|---|---|
| h17-f | 溜冰 ← 滑雪 | sibling_related → NO | lexical_ambiguity→ABSTAIN / sibling_related→NO / sibling_related→NO |
| h33-f | Training Seals ← 收藏印章 | unrelated → NO | unrelated→NO / lexical_ambiguity→ABSTAIN / lexical_ambiguity→ABSTAIN |
| h33-r | 收藏印章 ← Training Seals | unrelated → NO | unrelated→NO / unrelated→NO / lexical_ambiguity→ABSTAIN |

These account for all four eventual wrong judgments and all three inconsistent
cases. They are conservative NO→ABSTAIN differences, **not false acceptance**;
nevertheless they fail the explicitly required primary/relation stability gates.
No labels were changed to match them and ABSTAIN was not silently treated as NO.

The two first-failure batches were round 1 `[h33-r, h32-f]` and round 2
`[h36-f, h33-f]`; each failed as a whole and contained no salvaged rows. Their
first-attempt ERROR state remains in first-pass metrics, not recoded as NO.

With full input, h11/h47 in both directions are relation/primary-correct in all
three rounds. Origami equivalence is correct in both directions; all Mercury and
other gold-ambiguous cases consistently map ABSTAIN. Input fidelity and output
task both changed from R3.2b, so the improvement is not a controlled attribution
to either one alone.

### Category and language observations

Relation/primary accuracy: every category 100% except sibling (23/24 = 95.8333%)
and homonym (21/24 = 87.5%). The homonym result uses the predeclared v3 annotation
projection, not raw comparison with incompatible historical enum names.

| Q/C languages | Relation / mapped-primary accuracy |
|---|---:|
| EN/EN | 72/72 = 100% |
| EN/ZH | 70/72 = 97.2222% |
| ZH/EN | 71/72 = 98.6111% |
| ZH/ZH | 71/72 = 98.6111% |

All language groups have zero false YES in this sample. No claim of broad
cross-language generalization is justified from 24 distinct cases per group.

## E. Gate and decision

| Proposed gate | Verdict |
|---|---|
| First-pass validity >=99% | **FAIL**, 98.6111% |
| Eventual sample validity 100% | PASS |
| Mapped YES precision >=99% | PASS |
| Primary consistency >=99% | **FAIL**, 96.875% |
| Relation consistency >=99% | **FAIL**, 96.875% |
| Broad→specific / role / constraint false YES =0 | PASS |

**STOP. Do not create or evaluate R3.3, tune this prompt, change the frozen labels,
raise the token budget to rerun this result, or integrate into P1-A runtime.**

The policy separation and full-input directionality are promising, but the
complete engineering gate fails. Retain this as frozen research evidence rather
than authorizing a rollout. Current recommendation is to keep semantic fallback
OFF and prioritize a separately reviewed input-fidelity/identity correction design
because it also affects durable/exact paths. Do not remove or rewrite existing
semantic runtime code in this task.

Whether semantic fallback is worth keeping as an eventual product feature still
needs a deliberate product/engineering decision: bounded latency/cost, safe ERROR
behavior, acceptance-vs-relation stability requirements and expected benefit over
exact retrieval. Do not quietly lower the agreed gate just because observed errors
are conservative. No next provider experiment has been started.

Production blockers remain: historical embedding fingerprint, production Neo4j
version and production coverage/integrity are unverified; this work connects to
none of them. `.82` and both default semantic flags remain unchanged/OFF.

## F. Regression, scope and reproducibility

Readiness R3 through R3.2c plus affected semantic/search/OFF-parity/qualification
regressions: **265 passed, 229 subtests passed**, one existing multipart deprecation
warning. Compileall, shell syntax and diff checks pass. During the full suite a
new test's global `time.sleep` mock was contaminated by service background threads;
only the test clock binding was isolated. Frozen experiment sources/hashes did not
change, and the call-count/no-retry assertions remain intact.

GitNexus scope audit initially returned an anomalous CRITICAL result after an
incremental index update: the new pure metrics function was linked to unrelated
runtime functions that its source does not call. A full no-cache rebuild corrected
its UID/call graph; callers then resolved only to the offline runner/tests, and
the eleven-file diff reports LOW (114 symbols). The global process index remains
bounded, so zero reported affected flows is not treated as exhaustive proof;
explicit source/import checks and regressions supplement it. This tooling-index
anomaly is separate from the genuine CRITICAL blast radius of a future production
`_clean_label` change, which was **not** made.

R3.2c adds eleven readiness-only files (contract, prompt, fidelity/results docs,
three helpers and four test modules). It is uncommitted. No original fixture/v2
prompt/old parser/R3.2b evidence/production file was modified. Generated reports,
sanitized failed outputs and caches remain ignored, never staged for this task.

Run tests with an existing local interpreter and `AYUE_SKIP_DOTENV=1`, both flags
OFF, and `PYTHONPATH=social:.`. The four new hermetic test modules are
`test_r32c_relation.py`, `test_r32c_metrics.py`, `test_r32c_input_fidelity.py`, and
`test_r32c_offline.py`. Do not run provider experiments during unit tests.

For deliberate authorized reproduction, the runner actions are `prepare`, `run`
and `summarize`. `prepare` freezes hashes before scoring; `run` requires the
explicit synthetic-provider confirmation and selected existing config paths.
It refuses to overwrite the experiment or use a changed freeze. No credential
value should ever appear in arguments or documentation.

### Frozen inputs and ignored artifacts

| Frozen source | SHA-256 |
|---|---|
| r3_holdout.json | `0943c22f0f3aa4e9091644f811161ca8342fa00838359f2ded9239cf303b70e7` |
| r32c_prompt_v3.txt | `cad492b848cc1bab957be56f6accefdadffdee9db0961ec96b4571f5d0483911` |
| R32C_CONTRACT.md | `8442571c0ec7a8d144913d6ad0fb5bb384de6534bee56a4ebe7d03ee7938a69c` |
| r32c_relation.py | `e0bc2b43876aac9252b472bb4b25b2c42580b7322bc5e834a3135f033229d720` |
| r32c_metrics.py | `ce1ad5a0c920754b1622d2edd4883f81fe9f975faa0a296806c4da536b3d3bee` |
| run_r32c_offline.py | `b349f2db72b0eb0a705cd86c61aa7d0527f7d8c6e82a8f56bc9487570ef7ff38` |

| Ignored artifact under artifacts/r32c/ | SHA-256 |
|---|---|
| manifest.json | `a1d793dbe59d20565abcdfac2956dbb177bba856e2d2039c835750caa8edb538` |
| inputs.json | `985aecb819b1c9de148a01eedf2607b2173e56ad499b8cbd25d5622a809da6c7` |
| matrix.json | `cc43de7cf77b77070b57eae168aba10e6ed5b87ad8acbc103b3d594ac226fae3` |
| summary.json | `d17df3c97e3a25f632ace1c40c403f834bcf1fadd113e9f51885f304335546a5` |
