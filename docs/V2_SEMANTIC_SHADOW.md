# V2 semantic shadow-only hardening and independent threshold holdout

> **Historical rollout evidence / preparation instructions superseded — 2026-09-29.**
> PR #31 was merged/deployed as `8ccfe09f688b92c105e961994e4d1da87ed480b7`.
> Strict operator-auth, production V2 shadow and subsequent active controlled
> gates passed; `SEMANTIC_PRODUCTION_READY = YES` was accepted. Current production
> is **active / related-interest ON / threshold 0.90 / kill cleared**, with
> bootstrap OFF, new V2 worker ON and historical worker OFF. Use the
> [current overview and rollback runbook](PREFERENCE_SYSTEM_OVERVIEW.md), not the
> preparation-state flags below. Holdout results, code invariants and original
> regression observations are retained unchanged; they are not a natural-flow
> reliability guarantee or authorization to start another experiment/P1-B.

## Historical release boundary — at PR preparation

Prepared on `codex/v2-semantic-shadow`, from backend main
`efa5a7f0bc8f2b4caabf9fa4ea15f2c921c31f36` (docs PR30). Its implementation
is identical to accepted production `709ffc9da2c6649ae55f27a182a9703d92694245`;
the intervening changes are Markdown only. This PR does **not** deploy, activate,
migrate owners, modify preference lifecycle, change production threshold, or start P1-B.

At that preparation stage, production remained semantic/related-interest/bootstrap OFF, new V2 worker ON,
historical worker OFF, kill engaged. The recommendation below is not applied by
this code: the existing default threshold remains0.82 until a separately approved
activation/config release. Do not interpret implementation readiness as a production shadow PASS.

## Computation is not proposal permission

```text
normal exact retrieval + existing qualification
  ├─ qualified exact >0 → existing visible exact flow; ANN/validator calls=0
  └─ qualified exact =0 + shadow gates allow
       → signed V2 ANN → existing validator → verified owner expansion
       → current Graph polarity/conflict check → qualification observation
       → semantic_shadow aggregate telemetry ONLY
       → unchanged normal exact/no-match result
```

- `shadow_requester_allowed` requires `enabled_accounts`, shadow mode, the
  user-visible related-interest flag OFF, a valid owner ID, and kill absent.
  The same authoritative Appwrite/Mongo/Graph eligibility checks run in retrieval.
- `requester_route_allowed` / `pair_route_allowed` retain active-only semantics.
  Proposal final proof still rejects shadow; no shadow evidence enters final
  candidate pools, explanation generation, drafts, invitations, or memory writers.
- The internal `shadow_only` request purpose is strict-boolean, signed and owner
  quota-bound. It must agree with server mode; it cannot enable shadow from active
  mode, enable active from shadow, or bypass kill. Normal accounting is preserved;
  shadow is not a free/background quota exemption.
- Both modes use `concept_embedding_v2_index`, verified V2 identity/source hash,
  compatible768-vector/provenance checks, the same ANN bounds, prompt, model,
  taxonomy and cancellation/retry adapter. No legacy vector/index fallback.
- Kill is rechecked before Graph/provider work, between attempts and before
  accepting results/owner expansion. An in-flight call remains bounded by its
  existing transport deadline; no subsequent evidence may escape after kill.
- The old `semantic_observation_summary` now requires V2 shadow purpose; it
  cannot select the historical observation endpoint in shadow mode.

The shadow hook is after exact qualification, including the empty-pool early
return. Raw but unqualified exact hits do not suppress observation. Profile,
block/history filters and the normal pure qualification function are reused.
Full current pair PREFERS/AVOIDS conflicts are read with the existing Graph proof
helper; only verified positive packets support the observation. This is not a
new consent grant or a claim that a hypothetical proposal has passed a later
proposal-time proof. The active final recheck remains untouched.

## Bounds / observation accounting

Related shadow uses the same38s semantic budget /27s HTTP /18s shared validator
budget /6s maximum per attempt and existing ANN bounds. Additional observation
Mongo reads are capped by remaining budget; no unbounded legacy-memory HTTP reads
are added. ERROR or unavailable results remain observation-only; they cannot
change the exact/no-match visible result. A telemetry start must match an existing
owner/job before computation; no telemetry upsert, no preference writes.

`semantic_shadow` is separate from active `related_interest_pilot` metrics. It
stores bounded validator counts, Concept-hash/score observations, qualified count,
duration and allowlisted status/error, not raw messages, provider outputs, owner
candidate lists or evidence text. It never changes the visible job status/error.

Three consecutive typed unavailable observations within900s engage the **same**
server kill file. The active and shadow terminal streams are counted separately
to avoid pretending successful visible exact/no-match status means successful
shadow validation. Both use the existing `systemic_failure` policy and limits.
The aggregate report includes shadow separately; it does not inflate active
proposal/confirmation or user-facing failure counts. No frozen five-user timer
is reintroduced.

## Independent fixed holdout

Fixture: [v2_shadow_holdout.json](../tests/readiness/neo4j/v2_shadow_holdout.json).
SHA-256 fixed **before** scoring/provider calls:
`3208f2d66e4b86e3e892429cac1701ff8a0e58fe47dab669ac14c742aa03226c`.
Results: [v2_shadow_holdout_results.json](../tests/readiness/neo4j/v2_shadow_holdout_results.json).

32 fresh synthetic pairs, eight labeled classes ×4; no owner corpus, previous
16-pair calibration inputs, production owners or Graph writes. The frozen Gemini
768-dimensional semantic-similarity pipeline generated ephemeral vectors;
vectors were not persisted and no production index was created or changed.
The deployed validator adapter/model `deepseek-v4.1-flash:cloud` classified each
pair once. Four thresholds replay **the identical scored/classified pairs** to
avoid comparing different stochastic samples. This is paired retrieval-gate
calibration, not a full-corpus ANN ranking, top-k recall, end-to-end latency or
availability benchmark.

Neo4j cosine score is `(1+raw cosine)/2`; threshold comparison is in that score
space, not raw cosine. See [Neo4j vector-index documentation](https://neo4j.com/docs/cypher-manual/current/indexes/semantic-indexes/vector-indexes/).
The preceding read-only live-index calibration verified this mapping numerically;
this holdout performs the equivalent calculation without inserting synthetic data.

### Predeclared selection rule and matrix

Choose the **lowest** tested threshold retaining16/16 labeled positive targets
(4/4 in every ACCEPT class) and removing at least3/4 clearly unrelated pairs.
Do not optimize for unrelated=0 at the expense of positive recall. Unsafe negative
ACCEPT or absent trusted positive decisions prevents a ready claim. No cases or
labels were edited after scores were observed.

| Threshold | Raw cosine | Positive /16 | Equivalent /4 | Specific /4 | Sibling /4 | Role /4 | Unrelated /4 | Retained pairs /32 | Estimated attempts¹ | ERROR² | Estimated call / latency reduction¹ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.88 | 0.76 | 16 | 4 | 4 | 4 | 4 | 3 | 31 | 32 | 0 | 3.0% /1.4% |
| 0.90 | 0.80 | 16 | 4 | 4 | 4 | 4 | 0 | 28 | 29 | 0 | 12.1% /8.5% |
| 0.92 | 0.84 | 16 | 4 | 4 | 4 | 4 | 0 | 28 | 29 | 0 | 12.1% /8.5% |
| 0.94 | 0.88 | 16 | 4 | 4 | 4 | 4 | 0 | 24 | 25 | 0 | 24.2% /21.5% |

¹ Replay estimates against all32 evaluations (33 attempts including one retry,
70.784s summed validator-stage time). Each holdout request has one designated
candidate. These are not four fresh provider runs, production batch/top-k call
counts or measured post-change latency improvements.

² Final Concept ERROR0/32. A transient attempt/retry recovered; zero final ERROR
is not evidence that production never times out. Other rejected classes still
reach the validator; thresholds do not replace the directional taxonomy.

All32 ACCEPT/REJECT outcomes agreed with labels. Exact taxonomy agreement30/32:
`eq1` was role_mismatch rather than equivalent; `eq3` was candidate_more_specific
rather than equivalent. Both are in the allowed ACCEPT set; these disagreements
are preserved, not relabeled. Actual accepted taxonomy counts were equivalent2,
specific5, sibling4, role5; all were retained at every tested threshold.

**ANN_THRESHOLD_READY = YES for this fixed activation holdout.**
**RECOMMENDED_THRESHOLD = 0.90** (not deployed at preparation; subsequently
approved/applied during the production rollout above).0.92 gave no additional benefit in
this holdout;0.94 was not chosen merely to minimize calls. Small synthetic sample,
boundary stability and real ANN competition remain limitations to verify in a
separately approved controlled shadow window. This does not certify natural-flow
reliability or authorize production activation.

## Regression / evidence

Same host, Python3.12.3, identical per-suite dependencies for clean main and head;
`scripts/run_offline_tests.py` blocks network and dotenv secrets. Clean base is a
separate checkout with Git history, so the frozen P0 parity tests do not skip.

| Suite | Exact main | Head | Branch-only failures |
| --- | --- | --- | --- |
| Matchmaker full | 353 PASS | 365 PASS | 0 |
| Social semantic focused | 165 PASS | 176 PASS | 0 |
| Contracts full | 735 PASS | 735 PASS | 0 |

Initial focused-only Matchmaker run had the same baseline failure on both sides:
`test_real_async_validator_transport_is_cancelled_closed_and_has_no_sdk_retry`,
50ms cold SDK initialization expired before mock transport entry (attempts0 vs1).
This test and production timeout/retry implementation were not modified. The
final full suites passed it on both sides; do not omit the initial observation.

New tests cover exact bypass, zero-qualified-exact fallback, related/unrelated/
ERROR visible-result parity, strict signed V2 endpoint, kill before/during
validation, no proposal authority, invalid fingerprints/source/legacy/AVOIDS,
disabled candidates/conflicts, duplicate profiles, missing telemetry, independent
3-in-15m shadow accounting and aggregate privacy. Existing active/P0 parity suites
remain. Canonical `start_all.sh` syntax passes; no new service startup or production
deployment is claimed by these unit/differential tests.
