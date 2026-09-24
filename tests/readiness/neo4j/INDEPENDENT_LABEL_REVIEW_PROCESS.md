# Independent human label review — third final holdout

Production STOP. No third final dataset is authored until a backend passes
development and a prospective final gate is committed BEFORE data creation.
R3.3 and R3.4 final sets remain retired FAIL, not model/config selection data.

User designated a separate teammate as independent blind reviewer.

## Roles and separation

A: case author/initial labeler (assistant may draft synthetic cases and labels).
A cannot self-certify human review or decide post-score that a result is reasonable.
B: independent HUMAN teammate. B receives only randomized case ID, complete
canonical Q/C and frozen relation taxonomy. Hide author gold/category hints,
backend identity, model outputs and previous per-case commentary.
C: HUMAN adjudicator (project owner or another teammate), not the case-authoring
model/evaluated provider. C resolves A↔B disagreements before scoring.

The same person cannot fill B and the author role. A human alias is sufficient;
no private email/real name needed. B attests no access to initial answers or
new-holdout model outputs. C's decisions/reasons and timestamps are recorded.

## Prospective workflow

1. Freeze backend/config/prompt/parser/map and FINAL_GATE.md first. At minimum
   retain development gates: first>=99.5%, eventual sample100%, precision>=99%,
   recall>=95%, binary consistency>=99%, zero broad/role/constraint false accept,
   ERROR fail-open0. Relation-only diagnostics cannot override blocking metrics.
2. Build a third wholly new synthetic holdout, exclude canonical pairs from
   development, R2.5, R3.3 and R3.4. No provider scoring or model-based labeling.
3. A seals proposed relation/primary labels before B reviews. Commit their hash,
   not readable gold in the blind packet. Synthetic sealed gold can remain
   restricted locally until B's independent submission is received.
4. Export blind.csv: case_id,Q,C,reviewer_relation,reviewer_notes. Preserve full
   semantic_text, no display truncation. Shuffle IDs/order without category clues.
   B independently labels ALL cases; at minimum ambiguous, directional, role and
   constraint cases require independent review in both directions.
5. Import B's signed submission. Validate exact coverage, nine relation enums,
   no duplicate/unknown ID, and no empty mandatory review. Primary is computed
   by frozen code, never chosen independently by B.
6. Compare A/B only after B's submission is sealed/hashed. Record every disagreement.
   C resolves each before scoring with explicit rationale grounded in the frozen
   semantic contract. Unclear policy blocks the run; do not silently revise the
   taxonomy. Do not drop a difficult case to raise the expected score.
7. Freeze adjudicated gold, A/B/C attestations, disagreement ledger and all hashes.
   The author cannot certify missing B/C review. Dataset edits restart review for
   affected cases BEFORE scoring. No label edit after any final output is observed.
8. Only a validated signed REVIEW_COMPLETE manifest can unlock final scoring.
   Reject absent reviewer/adjudication or any hash mismatch before loading a key.
9. Execute one fixed final run; preserve ERRORs and failures. Final FAIL means
   STOP, not a replacement holdout or config re-selection. Final PASS permits
   separately reviewed integration; it does not activate semantic runtime.

## Review artifacts

- FINAL_GATE.md + backend/config/source hashes (before cases).
- cases.json / blind.csv (synthetic text; no gold/categories in blind view).
- author_gold.sha256; sealed author_gold.json local-only before B submission.
- reviewer_B.csv + reviewer_attestation.json (reviewer alias/date/independence).
- disagreement_ledger.json + adjudication_C.json (no unresolved rows).
- adjudicated_gold.json + REVIEW_COMPLETE.json (all hashes and approval).

Required attestation example (template only, NOT a completed review):
reviewer_alias: <teammate alias>
reviewed_at: <ISO timestamp>
independent_from_author: true
saw_author_gold_before_submission: false
saw_new_holdout_model_outputs: false
coverage_confirmed: true

No fabricated signature/reviewer. Providing an empty template is not review
completion. Until actual teammate review arrives: final scoring BLOCKED.
All data synthetic; no user IDs/profiles/raw memory, secrets or production Graph.
