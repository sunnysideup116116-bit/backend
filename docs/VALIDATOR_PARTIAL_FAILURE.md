# Validator partial-failure contract

This is request-result hardening only. Relation taxonomy/prompt, accepted relation policy, ANN/index/thresholds, embeddings, exact matching, candidate readiness/semantics and proposal final proof are unchanged.

## Trusted decisions and error scopes

A trusted decision is an existing strict-parser result for the whole requested batch: matching model, `stop` completion, exact schema/IDs, known relation, within the original validator deadline. Invalid/missing/truncated/late output never becomes a relation. Existing `unknown` remains a valid rejection, not an inferred acceptance.

- Local Concept/batch ERROR: exclude every affected Concept from owner expansion and candidate/proposal evidence. Retain numeric error/retry telemetry.
- At least one trusted decision, no job-wide failure: use only accepted Concepts. If all trusted decisions reject, return normal success with no candidates even when other Concepts errored. This means no match among successfully validated evidence, **not** that every ANN hit was successfully evaluated. No error-rate/ANN threshold is added or changed.
- Nonempty validator workload with zero trusted decisions: `semantic_validator_unavailable`.
- Empty ANN workload: normal no-match without calling the validator.
- Explicit job-wide failure: `semantic_validator_unavailable`, even after earlier ACCEPT/REJECT decisions. This includes failed deferred accounting, unsupported/mismatched provider model, or persistent typed provider auth/permission/rate-limit/5xx/non-timeout connection failure. Only typed error metadata is examined; exception bodies are not returned/logged. An internal numeric `job_unavailable` marker keeps this separate from Concept ERROR counts.
- The existing single retry can recover a provider failure. An exhausted batch with a job-wide failure aborts remaining work; it does not add retries. A per-attempt timeout remains batch-local if other decisions are trustworthy. An exhausted validator sub-budget may retain earlier trustworthy decisions only while the outer request budget remains. No trusted decision, or expired outer request budget, stays unavailable.

The limits remain: at most12 Concepts, batch2, at most2 attempts/batch, per-attempt6 seconds, validator sub-budget18 seconds within the original outer27-second request deadline. Async transport cancellation/closure is unchanged.

## Safety and telemetry

Kill checks retain priority. ERROR evidence cannot acquire an accepted relation or owner. Social still applies candidate qualification/history/block/consent and fresh proposal-time readiness checks. A normal partial no-match is not counted as a typed-unavailable job; three consecutive typed-unavailable semantic jobs within15 minutes still engage the server kill. Bounded Concept error/retry counters remain visible; errors are not erased to obtain a successful empty result.

Tests cover6 REJECT+2 ERROR at different batch positions,2 ACCEPT+2 ERROR, zero valid decisions, recovered/persistent typed provider failures, accounting/model failure after both ACCEPT and REJECT prefixes, local/outer deadlines, late accepts, kill, empty ANN, Social normal no-match/error propagation, ERROR-only candidate exclusion, proposal final recheck, exact-first and continuous unavailable protection. All fixtures are synthetic with transport/Graph/Mongo stubs; no production requests or data writes are part of these tests.

This change does not authorize deployment, kill removal, all-user activation, bootstrap, backfill, a new monitor schedule or P1-B.
