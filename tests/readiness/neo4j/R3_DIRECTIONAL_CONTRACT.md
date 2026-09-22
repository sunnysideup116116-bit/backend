# R3 Offline Directional Relation Contract v1

Design and offline experiment only. No production caller, Graph mutation, alias,
PREFERS change, shared_persistent_preference promotion, threshold change, or P1-B.

## Question and inputs

Given query preference Q and candidate's confirmed preference C, is C sufficient
evidence that this candidate satisfies Q? This is directional evidence sufficiency,
not conversational compatibility, general relatedness, or whether two people might
enjoy meeting. A reverse case is a distinct judgment.

Input is a batch of at most 8 pairs (hard max 12), each with an opaque case ID and
two canonical labels/descriptions, each at most 120 characters. No user ID, profile,
candidate record, raw message/memory, ANN score, expected label, or rationale is sent.
Descriptions are untrusted data, not instructions. No tool use or external lookup.

Primary: `YES | NO | ABSTAIN`.
Secondary:

| Secondary | Primary | Meaning |
| --- | --- | --- |
| equivalent | YES | Same preference with all expressed constraints/roles preserved |
| candidate_specific_satisfies_broader_query | YES | Saved specific interest entails the requested broader category |
| candidate_broader_insufficient | NO | Broad saved interest does not establish the requested specialization/qualifier |
| sibling_related | NO | Neighbouring subtypes, not evidence of the specifically requested subtype |
| role_mismatch | NO | Actor/creator/teacher/consumer/audience roles cannot be inferred from one another |
| constraint_conflict | NO | Explicit activity, setting, negation, duration or other material constraints conflict |
| lexical_ambiguity | NO | Both meanings are clear but a shared word refers to different senses |
| unrelated | NO | No sufficient preference relationship |
| unknown | ABSTAIN | Ambiguous, incomplete, undecidable or uninterpretable label/description |

Missing qualifiers are not automatically conflicting; use broader-insufficient if
the saved evidence is clear but under-specific. A composite Q requires every
material conjunct. Explicit incompatible qualifiers take precedence over mere role
or topic overlap. A shared word alone never authorizes YES. ABSTAIN is not a
substitute for a clear NO, and a NO/ABSTAIN is never an AVOIDS fact about a user.

Only equivalent/specific-satisfies-broader YES could be admitted by a future reader;
even then evidence remains semantic_related. No alias, identity merge, new owner
preference or confirmed shared preference follows from this output.

## Fixed experiment protocol

- New synthetic holdout: 48 semantic groups, two explicit directed cases per group,
  total 96. Forward/reverse labels are individually specified before scoring.
- 24 cases each EN/EN, ZH/ZH, EN/ZH, ZH/EN. Group-level dependence is retained;
  96 directional cases are not 96 independent semantic groups.
- No pair (including reverse direction) from the R2.5 100 is reused. Concept surface
  overlap is checked/documented; no existing pair is repurposed as a holdout.
- Expected labels/reasons never enter the provider prompt. Fixed prompt and fixture
  hashes are written to a preflight manifest before ANY R3 scoring/model call.
- Use the currently available Ollama Cloud model (resolved from allowlisted local
  model/key/base settings), not the matchmaking selection prompt. No model tuning
  against this holdout. This model is an offline experimental validator, not a new
  production classifier.
- Three independent passes, shuffled batch/order with recorded seeds, temperature
  0 for a conservative repeatability baseline. Default 8 cases per batch, 12 calls
  per pass, 36 calls total; max 40 attempts including bounded transient retries.
- Output schema admits only ID, primary decision, secondary relation. No rationale
  generation or chain-of-thought. Missing/duplicate IDs, malformed JSON, invalid
  enum or decision/relation mismatch are errors, not silently repaired decisions.
- Provider failures/malformed outputs are measured separately from model ABSTAIN.
  No fail-open, invented answer or post-hoc prompt/label revision in this run.
- Gemini embedding semantics stay production-equivalent: canonical label,
  semantic_similarity prefix, 768 dimensions, L2 normalization. Synthetic texts only.
- Combined analysis compares ANN .92/.93/.94/.95 with validator YES on each repeat.
  Pair-index ANN scores and corpus top-K coverage remain separate; no full Graph scan.

## Metrics and stop rule

Report YES precision/recall per run and pooled, full primary confusion matrix,
secondary accuracy, ABSTAIN/error rates, all-three consistency, and unanimous-YES
performance. Report role mismatch and constraint/opposite false YES separately;
equivalent/cross-language false NO separately; and both broad/narrow directions.
Do not count ABSTAIN as a successful NO or hide it by reducing the recall denominator.

Also report category/language strata and combined ANN+YES quality, with the original
labels preserved. Thresholds are experimental cutoffs, not runtime configuration.
Repeated results are correlated and do not multiply the independent sample size.

No production implementation if role/constraint false acceptance appears or directional
decisions are unstable. Even a clean run only justifies further independent review and
holdout validation, not activation: agreed production quality/latency targets and
production provenance/readiness remain separate gates. Reusing this evaluated set
for a revised prompt requires a NEW independent holdout for final evaluation.

## Boundaries

Both semantic flags stay OFF. Production embedding fingerprint, Neo4j version and
index coverage/integrity stay unknown. No production Graph, user data, proposal,
quota writes, deployment, backfill or P1-B. Only explicitly allowlisted local secrets
may be parsed in memory; no whole dotenv loading and no secret-bearing output.
