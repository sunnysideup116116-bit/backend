# R2.5 Semantic Quality Gate — synthetic measurements and design only

Status: experiment completed, production activation **NO-GO**. No runtime threshold,
SDK, prompt, candidate qualification or feature flag was changed. No P1-B, Event
pipeline, production database, deployment, proposal or migration action occurred.

## Frozen dataset and methodology

- Source baseline: merged main `97ee0b5e2172cca6b985df2a4c4a53b1a31de489`.
- Fixture version 3: 100 directed semantic pairs / 166 unique concept texts.
- Pre-scoring SHA-256: `428214c4cd645b3a5037022e419e480b59e9b0910cabf076d8c6003309b6eeb0`.
- The original 25 pair objects/labels and four alias/normalization groups are unchanged.
- 20 clear/compatible positives, 20 cross-language positives, 20 borderline,
  15 lexical hard negatives, 25 topic-adjacent/constraint/role negatives.
- New scenarios include paraphrase, broad/narrow direction, sibling interests,
  different roles, action/consumption, homonyms, opposite constraints and composite
  descriptions. Alias groups are excluded from scoring.
- Languages: EN–EN 68, EN–ZH 22, ZH–ZH 9, ZH–EN 1. These are not balanced groups.
- Labels were fixed before scoring; no score-driven relabeling. They remain small,
  manually authored task judgments, not an authoritative preference ontology.
- Existing broad/narrow borderline judgments are retained even where a future
  independent label review might dispute them. No post-hoc relabeling was performed.
- The old 25 are previously observed cases; the new 75 were labeled before scoring,
  but this is still an exploratory set, not a statistically independent production
  holdout or a valid basis for choosing a definitive threshold.

Embedding contract: `models/gemini-embedding-2`, provider metadata version `2`,
logical `semantic_similarity`, 768 dimensions, exact prefix
`task: sentence similarity | query: `, finite/nonzero checks and L2 normalization.
Primary SDK is `google-generativeai 0.8.3`; two samples match `google-genai 2.11.0`
exactly when each input is a separate explicit Content. A flat string list in the
new SDK groups text into a single Content for embedding-2 and is not an equivalent
batch. No production SDK change was made.

There were 13 API attempts / 12 successes / one quota-exhausted failover; zero auth
or other provider failures. No key identity or fragment is logged. Unique inputs
were embedded once in process memory, except the two explicit SDK-parity samples.
Only approved key assignments and the nonsecret model identifier were parsed;
production DB/Appwrite/Neo4j settings were not loaded.

## ANN measurement and thresholds

Neo4j Community `2026.08.1` is a localhost-only disposable baseline, not evidence
of the production version. The existing 768-d cosine Concept index is ONLINE.
For this run `ANN = (1 + raw cosine) / 2` within `8.01e-8` maximum absolute error.
Thus `.90` ANN means approximately `.80` raw cosine; the scales are not interchangeable.

Corpus ANN stays bounded to 32 neighbors. It recalled 99/100 labeled partners,
including all 40 positives. The one missing partner is `adjacent-negative-22`.
To avoid treating a top-K miss as an unknown/zero pair score, all 100 pair scores
were separately measured through a disposable two-Concept `R25Pair` vector index
with the same dimension/cosine contract and `k=2`. Corpus scores/ranks and pair-index
scores are separate report fields. No full-scan cosine retrieval was introduced.
Temporary R25Pair labels were removed; Concept identities were not merged/aliased.

Binary metrics below use 40 positives + 40 negatives. The 20 borderline cases are
reported separately, never silently assigned a binary label. These are directed
pair threshold metrics, not final proposal precision or population-wide ANN recall.

| ANN threshold | Approx. cosine | TP | FP | FN | TN | Precision | Recall | Borderline accepted |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| .82 baseline | .64 | 40 | 40 | 0 | 0 | 50.0% | 100.0% | 20 |
| .90 | .80 | 40 | 36 | 0 | 4 | 52.6% | 100.0% | 20 |
| .91 | .82 | 40 | 31 | 0 | 9 | 56.3% | 100.0% | 20 |
| .92 | .84 | 40 | 24 | 0 | 16 | 62.5% | 100.0% | 19 |
| .93 | .86 | 39 | 19 | 1 | 21 | 67.2% | 97.5% | 15 |
| .94 | .88 | 39 | 9 | 1 | 31 | 81.3% | 97.5% | 11 |
| .95 | .90 | 39 | 6 | 1 | 34 | 86.7% | 97.5% | 8 |
| .96 | .92 | 32 | 3 | 8 | 37 | 91.4% | 80.0% | 3 |
| .97 | .94 | 22 | 1 | 18 | 39 | 95.7% | 55.0% | 1 |
| .98 | .96 | 9 | 0 | 31 | 40 | 100.0% | 22.5% | 0 |

ANN distributions (rounded; raw/full-precision fields remain in the local report):

| Label | Count | Min | Median | Mean | Max |
| --- | ---: | ---: | ---: | ---: | ---: |
| Clear/compatible positive | 20 | .928016 | .979029 | .974966 | .992071 |
| Cross-language positive | 20 | .952352 | .968050 | .967341 | .979324 |
| Borderline | 20 | .913642 | .946898 | .945602 | .971659 |
| Hard negative | 15 | .907958 | .931280 | .928882 | .946300 |
| Topic-adjacent negative | 25 | .882714 | .928498 | .927036 | .970134 |

At `.95`, false positives are listening/performing live music (.962481),
reading/writing mystery novels (.970134), watching/playing football (.967286),
watching/performing ballet (.956021), listening/producing podcasts (.954823), and
reading/writing poetry (.956267). The false negative is broad Music → Listening
to Jazz (.928016), labeled compatible in this directed search.

At `.96`, retained cross-language positives fall from 20/20 to 15/20; at `.97`,
only 8/20 remain. Examples lost include Playing Chess → 下西洋棋 (.952352),
Gardening → 園藝 (.956201), Cycling → 騎自行車 (.957071), and Board Games → 桌遊
(.957878). Related sibling K-pop → J-pop remains .957989, while a role-negative
Reading Mystery Novels → Writing Mystery Novels scores .970134.

## Source-backed downstream evaluation

Boundaries:

1. Actual synthetic local Graph embeddings and PREFERS/AVOIDS.
2. Source-loaded, unchanged exact/semantic Graph retrieval, Social profile filtering,
   hard-conflict qualification, compact payload and controlled evidence.
3. In-memory Mongo profiles/history, empty synthetic Risk block set; no production
   data. No raw private chat. Requester/candidate deep profiles and recent contexts
   are empty to avoid inventing independent reasons to match.
4. Source-owned 9001 graph-memory enrichment and prompt construction captured locally.
5. Identical captured evidence replayed through the unchanged 9001 evaluator and
   actual `MatchmakerAgent.match_async` using the current Ollama Cloud
   `deepseek-v4.1-flash:cloud`, temperature .7, normal output budgets/deadlines.
6. Source-owned Social response validation checks that selection belongs to the
   supplied batch. No real 9001 HTTP endpoint or full server startup is used.
7. Proposal boundary disabled; quota usage hook is an in-memory test double. No
   proposal, quota, consent, Appwrite or production DB write is performed.

This is a split-stage local integration/replay test with a real model, not a full
authenticated HTTP/server E2E or a certification of proposal lifecycle behavior.
Captured prompts and replayed prompts are byte-equal. Dummy capture responses are
explicitly NOT counted as model rejections.

| Pair | Base qualification | Real Matchmaker selected / rejected (3 trials) | With explicit saved AVOIDS | Missing profile |
| --- | --- | --- | --- | --- |
| Vegetarian Cooking → Steak Tasting | eligible, semantic-related | 3 / 0 | blocked | blocked |
| Reading Mystery Novels → Writing Mystery Novels | eligible, semantic-related | 3 / 0 | blocked | blocked |
| Listening to Live Music → Performing Live Music | eligible, semantic-related | 3 / 0 | blocked | blocked |
| K-pop → J-pop (borderline, not a negative relabel) | eligible, semantic-related | 3 / 0 | blocked | blocked |
| Python Programming → Keeping Pet Pythons | eligible, semantic-related | 0 / 3 | blocked | blocked |

Five positive controls each selected 1/1. Total real requests: 20, no retry or
provider failure. These repeated, intentionally difficult cases are not independent
population samples and must not be advertised as a global final-matching precision.
Three of four tested negative pairs still reach selection; Python ambiguity is
rejected by the model. Borderline acceptance is recorded separately.

The existing hard-conflict rule requires explicit opposite stances on the same
canonical key. Vegetarian PREFERS does not automatically become AVOIDS Steak.
Qualification treats owner-grounded semantic-related evidence as an adjacent
qualifying basis. The model prompt permits related conversation bridges while
forbidding false shared-preference claims; that is not the same guarantee as strict
fulfillment of an explicit preference search. Selection therefore is not a reliable
second-stage semantic compatibility filter.

## Decision

Do not change `.82` in runtime and do not activate the feature. `.95–.96` is a
useful exploration range, not an approved production setting: the remaining
role-based false positives and cross-language recall loss are material. No single
threshold perfectly separates these frozen labels. No universal acceptable
precision/recall target has yet been agreed.

### Proposed minimal relation-validation stage (NOT IMPLEMENTED)

Only explicit preference fallback after qualified exact count is zero:

```text
exact retrieval + existing filtering/qualification
→ qualified exact count = 0
→ existing bounded ANN Concept shortlist
→ bounded, directional relation validation
→ only validated equivalent/compatible Concept hits
→ existing bounded PREFERS candidate expansion
→ existing profile/block/history/hard-conflict/qualification/Matchmaker
→ unchanged quota/dedupe/confirmation/mutual-consent lifecycle
```

- Preserve current bounds: ANN <=32, Concept shortlist default 8 / hard max 12,
  fanout <=20, IDs <=50, hydrated pool <=20. Do not widen/refill the shortlist after
  rejection and do not add generic/vector hybrid retrieval.
- Input is only canonical query label and bounded saved Concept labels plus their
  literal qualifiers. No user IDs, profiles, raw chats, private memory snippets or
  inferred owner attributes. Query direction matters; a narrow saved interest may
  support a broad query, but the reverse needs evidence.
- Outcomes: `equivalent`, `compatible`, `related_but_different`,
  `conflicting_or_opposite`, `unrelated`; include `unknown`/abstention for uncertainty.
  Only equivalent/compatible pass. Roles, negation, constraints and composite
  conjunctions must be explicitly respected; unknown qualifiers cannot be invented.
- K-pop/J-pop should be related-different, not automatically conflicting. A concept
  rejected for compatibility is not an AVOIDS fact about either user.
- Output schema: bounded enum, input pair index, bounded reason codes; no free-form
  public explanation or fabricated preference facts. Similarity never authorizes
  alias creation, Concept merge, durable identity change or PREFERS mutation.
- Even a passed semantic relation remains `semantic_related`, not exact identity or
  confirmed shared preference; existing public disclosure policy is unchanged.
- Do not assume current Matchmaker selection or a higher score substitutes for this
  validator. Candidate owner/PREFERS evidence still must be present and grounded.
- Implementation choice requires separate approval: a small bounded classifier or
  other validated relation model is only a proposal, not a new service/prompt here.
  Avoid a large hand-written synonym ontology and avoid score-derived labels.
- At most one bounded batch per fallback; cache only versioned Concept-pair results
  (label hashes + embedding/validator contract version), never raw chats/user IDs.
  Cache capacity/expiry must be bounded and invalidated on label/contract changes.
- Share the existing total fallback deadline; timeout/provider failure cannot become
  `no_candidates`. Exact results remain usable; zero exact plus infrastructure error
  returns a typed transient failure. Successful validation with no sufficient support
  returns an explicit insufficient-ground result. No fail-open or full Graph scan.
- Begin with separate offline observation, not added synchronous shadow latency.
  Validate accuracy and latency on an independently labeled holdout before proposing
  any canary budget or threshold. No deployment/activation is implied.

Required future tests: all four relation outcomes plus abstention; role reversal;
broad/narrow direction; sibling interests; negation/constraint conflict; all composite
qualifiers; multilingual parity; adversarial label text; provider timeout/malformed
response; bounded batch/cache; unchanged aliases/Graph; exact-only/OFF parity;
existing safety/consent gates and prompt/privacy projection. Include held-out
positive controls and report per-language precision/recall, not only aggregate.

## Remaining blockers and artifacts

Historical production embedding fingerprint, production Neo4j version, and production
index coverage/integrity remain unknown. Current model metadata version `2` and
768 dimensions do not prove historical vector compatibility. Both semantic flags
remain OFF. Neither local ANN success nor an improved offline classifier is production
readiness.

Local ignored outputs: `artifacts/r25-report.json`, `artifacts/r25-downstream.json`,
`artifacts/r25-matchmaker-live.json`, and derived `artifacts/r25-summary.json`.
They contain synthetic inputs/results only; no API secrets or serialized vectors.
The reusable calibration and deterministic downstream-preparation scripts were
subsequently promoted to `calibrate_r25.py` and `prepare_r25_downstream.py` for review,
with explicit CLI confirmation and no import-time provider/Graph access. Replay
reports have separate names so frozen originals are retained. One-off real-model
selection and summary helpers remain ignored artifacts. These are not runtime
changes. No commit or push is performed as part of this gate.

Validation: 81 existing affected tests + 3 subtests passed (one existing multipart
deprecation warning). The run used `AYUE_SKIP_DOTENV=1`, both semantic flags OFF,
and the tests' in-memory/mocked service boundaries:

```text
python -m pytest -q social/tests/test_preference_semantic_service.py
  social/tests/test_preference_match_search.py social/tests/test_preference_off_parity.py
  social/tests/test_match_qualification.py
```

Fixture invariants, original-label/alias preservation, pre-score hash, experiment
script syntax, `git diff --check`, and `bash -n start_all.sh` passed. Secret-value
and serialized-vector scans passed for the changed docs/data and local reports.
Only fixtures/docs are tracked changes. The disposable container was stopped again;
synthetic volumes and ignored artifacts were retained.
