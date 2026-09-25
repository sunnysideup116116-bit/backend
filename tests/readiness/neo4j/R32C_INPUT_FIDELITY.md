# R3.2c input-fidelity investigation

Status: source-level defect reproduced with synthetic pure-function tests.
This document proposes a future fix; it does not implement a production fix.
The tests deliberately describe current loss, not a desired retention contract.

Scope: the independent readiness checkout after R3.2b freeze `12f7388`.
No production data, environment/credential loading, provider request, Graph
connection, migration, deployment, feature activation or runtime modification
was used for this investigation. Runtime semantic flags remain OFF, `.82` is
unchanged, and historical production embedding fingerprint remains unknown.

## Root cause and limit classification

[`_clean_label`](../../../matchmaker_agent/concept_identity.py#L69) returns
`text[:40]` at line 74. The value is a hardcoded Python-character slice, not a
named configuration constant. It runs after NFKC/whitespace/prefix cleanup but
**before** canonical key construction. `canonicalize_concept` at lines 84–97
derives both its returned label and slug/hash from that shortened string.

Therefore the 40-character rule is an **identity/storage and semantic-input
limit**, not merely a presentation limit. There is no word-boundary protection,
truncation marker or fidelity status. Two different full descriptions with the
same cleaned first 40 characters can share one deterministic Concept key.

`DurableMemoryCandidate.label_zh_tw` is an unrestricted string in
[`profile_contracts.py:51`](../../../social/services/profile_contracts.py#L51).
The first durable extraction cap is imperative code, not a schema rejection.
Independent caps mean changing `_clean_label` alone would not fix the path.

## Trace: durable preference to semantic evidence

| Boundary | Actual behavior and source |
| --- | --- |
| Extracted durable candidate | [`profile_skills._validate_memory:362`](../../../social/services/profile_skills.py#L362) uses `_clean(..., 40)`, then canonicalizes at line 375; returned key/label are canonical at line 386. |
| Canonical identity | [`concept_identity._clean_label:74`](../../../matchmaker_agent/concept_identity.py#L74) slices to 40; [`canonicalize_concept:84`](../../../matchmaker_agent/concept_identity.py#L84) uses that result for alias/slug/hash. |
| Social memory projection | [`memory_service.normalize_memory_item:39`](../../../social/services/memory_service.py#L39) calls `normalize_zh_tw(..., max_length=40)`. Read/cache projections also reuse this function. |
| Mongo preference facts | [`preference_store._clean_item:40`](../../../social/services/preference_store.py#L40) slices to 40 before canonicalization. |
| Graph durable writer | [`agent_api.apply_memory:1610`](../../../matchmaker_agent/agent_api.py#L1610) slices at line 1613, canonicalizes at line 1627, and writes Concept identity/label at lines 1671–1673. |
| Registration/correction | [`registration_graph:65`](../../../matchmaker_agent/registration_graph.py#L65) and [`agent_api.memory_action:1797`](../../../matchmaker_agent/agent_api.py#L1797) independently cap labels to 40. |
| Pending Concept embedding | [`agent_api.list_missing_concept_embeddings:880`](../../../matchmaker_agent/agent_api.py#L880) reads persisted `concept.label`. |
| Embedding input | [`concept_embedding_service:135`](../../../social/services/concept_embedding_service.py#L135) caps persisted labels to 60; lines 150–155 embed them with `semantic_similarity`, dimension 768. This cannot recover an earlier missing suffix. |
| Provider preprocessing | [`ai_service.get_embeddings:244`](../../../social/services/ai_service.py#L244) caps text to 500. For Gemini embedding-2, lines 253–260 add `task: sentence similarity \| query: `; this is not the origin of the observed 40-character loss. |
| Vector projection | [`agent_api.project_concept_embeddings:898`](../../../matchmaker_agent/agent_api.py#L898) has another 60-character cap and at lines 923–927 writes the label/vector back to the matched Concept. |
| Preference search topic | [`match_search_context:83`](../../../social/services/match_search_context.py#L83) canonicalizes preference topics despite its outer 80-character topic allowance. [`preference_candidate_service:36`](../../../social/services/preference_candidate_service.py#L36) does the same for exact lookup. |
| Semantic query embedding | [`preference_semantic_service:215`](../../../social/services/preference_semantic_service.py#L215) canonicalizes topic; line 238 sends the canonical label; lines 254–258 embed only that label when needed. |
| ANN retrieval evidence | [`agent_api.preference_semantic_candidates:1480`](../../../matchmaker_agent/agent_api.py#L1480) queries the vector index. It returns bounded Concept **keys/scores**, not full Concept labels, at lines 1490/1508/1560. |
| Relation validator | No such production step currently exists. R3 experiments below are offline readiness tooling, not a deployed relation-validation surface. |

The 60/500-character downstream limits are separate resource guards, but they
cannot restore information already lost during extraction/identity/storage.
Storage therefore can lose a constraint before the first embedding is created.

## Offline experiment path and affected development cases

Original R3 [`run_r3_offline.prepare:116`](run_r3_offline.py#L116) canonicalized
fixture Q/C and saved `query_label`/`candidate_label`; its embedding path at
line 148 used those labels. R3.1
[`frozen_cases:29`](run_r31_reliability.py#L29) verified original frozen hashes,
then **overwrote Q and C** with `canonicalize_concept(...).label` at lines 37–38.
R3.2b [`frozen_v2_cases:39`](run_r32b_offline.py#L39) inherited that behavior;
[`request_kwargs:74`](run_r32b_offline.py#L74) sent those bounded Q/C values.

The unchanged 48-group/96-direction dataset has maximum full-label length 46.
Only these two labels differ after canonicalization, affecting four directional
cases (`h11-f`, `h11-r`, `h47-f`, `h47-r`):

| Group | Original full label | Actual previous model input |
| --- | --- | --- |
| h11.b | Watching Waterfalls on Short Accessible Trails | `Watching Waterfalls on Short Accessible ` |
| h47.b | Visiting Quiet Castles without Guided Groups | `Visiting Quiet Castles without Guided Gr` |

Prior frozen reports must remain intact. This finding limits attribution of
their errors to reasoning alone; it does not retroactively remove difficult
cases or turn a failed quality gate into a pass. R3.2c instead uses full bounded
fixture text and rejects oversized input rather than silently truncating it.
The 96 cases remain development data, not a fresh final holdout.

## Synthetic qualifier/role probes

[`test_r32c_input_fidelity.py`](test_r32c_input_fidelity.py) loads only the pure
identity module through the existing `run_readiness.canonical_module` helper.
It does not import application configuration/services or call any external
system. Tests include current long-input loss and intact short-input controls.

| Meaning | Full synthetic input | Observed loss |
| --- | --- | --- |
| Accessible | Watching Waterfalls on Short Accessible Trails | `Trails` lost. |
| Wheelchair | Visiting Historic Gardens with Wheelchair Accessible Paths | Ends at `Wheelchai`; `r Accessible Paths` lost. |
| Step-free | Walking around Historic Castles using Step-Free Routes | Ends at `St`; `ep-Free Routes` lost. |
| Quiet/no guided tours | Visiting Famous Castles Quietly with No Guided Tours | `Guided Tours` lost, leaving an incomplete negation. |
| Vegetarian | Attending Community Dinner Parties with Vegetarian Food Only | Entire `Vegetarian Food Only` constraint lost. |
| Alcohol-free | Attending Community Dinner Parties with Alcohol-Free Drinks Only | Entire `Alcohol-Free Drinks Only` constraint lost. |
| Watching/playing | Enjoying Weekend Football Matches through Watching / Playing | Both full inputs become the same first-40-character identity. |
| Reading/writing | Exploring Historical Mystery Novels through Reading / Writing | Both become `Exploring Historical Mystery Novels thro`. |

The vegetarian/alcohol-free pair also produces the same key:
`attending_community_dinner_parties_with`. Reading/writing produces the shared
key `exploring_historical_mystery_novels_thro`. These are deterministic prefix
collisions, not semantic similarity or automatic synonym matching.

Short controls such as `Watching Football`, `Playing Football`, `Reading Mystery
Novels`, `Writing Mystery Novels`, `Vegetarian Food`, `Alcohol-Free Drinks`, and
`Step-Free Routes` retain their complete labels. Information loss depends on
length/position, not a special role or language rule.

## Production impact: what is and is not established

The production **code paths** for durable extraction, storage and exact lookup
unconditionally use these caps. Feature OFF does not protect those operations.
P1-A active query embedding would also use the shortened canonical label.

Semantic fallback itself is gated by `semantic_mode == "active"` in
[`social/routers/match.py:1933`](../../../social/routers/match.py#L1933); defaults
remain OFF in [`preference_semantic_service:68`](../../../social/services/preference_semantic_service.py#L68).
There is no production directional relation validator to diagnose as running
with truncated labels. Whether actual deployed records contain these long
labels, their count, and whether historical text is recoverable are **unknown**:
no production configuration or Graph was accessed to answer those questions.

## Minimal future correction design — NOT implemented

1. Separate full bounded semantic/identity text from display text. Presentation
   clipping must never feed identity, embedding or relation input.
2. Define one explicit full-text bound and reject/clarify oversized concepts;
   never silently slice qualifiers. Reconcile all existing 40/60-character
   storage, API and worker boundaries together.
3. Compute deterministic identity from complete normalized text, maintaining
   centralized deterministic aliases and existing short-concept behavior. A
   key-length bound can use a digest without removing semantic input text.
4. Version the identity/input contract. Merely increasing the cap changes keys
   for long labels and does not recover historical suffixes. Do not silently
   rename/MERGE old Concepts, create semantic aliases or rewrite owner edges.
5. Retain full owner-grounded evidence only within its existing privacy policy.
   Historical truncated records without reliable evidence stay fidelity-unknown;
   a later separately authorized reconciliation/reconfirmation plan is needed.
6. Bind embedding provenance/cache validity to exact full-input hash and
   preprocessing version. A repaired label must not reuse a truncated-input
   embedding. No such migration/backfill is authorized in this experiment.

This is an input/identity correction proposal, not permission to expand R3.2c
into production implementation or Event behavior changes.

## GitNexus review

Graph queries explicitly bound the independent readiness checkout. At trace
time the indexed HEAD matched the R3.2b parent; staleness was confined to its
report/ignored artifact, and every production source above was read directly.

`impact(_clean_label, upstream)` reports **CRITICAL**, 34 symbols, six reported
processes and five modules. Direct callers are `canonicalize_concept` and
`canonical_query_provenance`; upstream includes memory writes, registration,
Pi matching, exact/semantic search and rationale construction. The separate
`riskSharedAxes=MEDIUM` value does not waive that CRITICAL warning. Some graph
callers can be unresolved, so these counts are not a complete safety proof.

No production function was edited. New evidence-only test symbols were absent
from the index (`UNKNOWN`); a source search confirmed they were new standalone
tests rather than existing runtime symbols. Future production correction must
have its own impact analysis and regression/migration review.
