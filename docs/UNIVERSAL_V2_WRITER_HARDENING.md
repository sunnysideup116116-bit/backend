# Universal-v2 future-writer hardening

Current release outcome (2026-09-28): backend PR #29 merged/deployed as
`709ffc9da2c6649ae55f27a182a9703d92694245`; canonical runtime restored and audited
without changing dirty Server/Pi source. Production restore/rebuild disposable
checks and cleanup passed; a whole-node hash STOP was read-only reviewed as only
five added existing-queue metadata fields, not an identity/vector/owner-edge
change. No mutation retry or data repair was performed. Current population,
flags and client merge are in [the architecture summary](PREFERENCE_SYSTEM_OVERVIEW.md).

Validated base: `66e7ad869b00624af25c05be83986fa0ad2240fa`.
Scope: restore and manual projection, plus explicitly approved recovery/old-CLI
guards. No matching, relation taxonomy, ANN, threshold, validator, embedding
policy, ordinary memory cap, client, signing, APK or P1-B change.

## Reachable writer inventory

| Entry | Durable identity authority |
| --- | --- |
| Chat extraction, profile/manual add, voice memory.add, explicit feedback | Existing validated full-text v2 memory apply → `write_preference_edges` |
| Registration/onboarding | Existing `registration_graph` verified v2 insert-only writer |
| Correct/edit | Existing owner action-reference fence; create/reuse verified v2 target |
| Restore | `preference_restore`: canonicalize legacy source or verify existing v2; old legacy stays inactive; preserve owner/polarity |
| Owner-confirmed bootstrap | Existing complete-set preview/consent/fence; verified v2 only |
| Bootstrap rollback | Reject legacy restoration before Mongo prepare and again under Graph fence; v2-only rollback retained |
| Manual projection rebuild | Explicit owner; existing active Graph v2 authority, no preference creation from Mongo payload |
| Historical migration CLI | `--apply` unconditionally rejected before dotenv/DB; readonly inventory retained |

The v1 canonicalizer still supports **read-only exact comparison**; it is not a
writer. General memories, events and expiring CURRENTLY_WANTS are not converted
to durable preferences. Privileged arbitrary database administration and synthetic
fixture setup are not product writer contracts.

Restore and CLI queue work transactionally only for positive verified PREFERS.
The existing missing-only worker/provider runs outside the Graph transaction.
Shared nodes and historical vectors are preserved. A rejected restore stages no
Mongo intent; committed Graph recovery markers cover lost acknowledgements without
replaying mutation. Legacy restore replay is rejected, not silently reactivated.

## Same-host clean-base differential

Python 3.12.3 / pytest 9.1.1. Each pair uses the identical interpreter/dependencies
and `scripts/run_offline_tests.py` (dotenv and external network disabled).
Social/Contracts use their pinned OpenAI 1.30.1 / Neo4j 5.21.0 overlay; Matchmaker
uses OpenAI 2.35.1 / Neo4j 6.2.0. HTTPX 0.28.1. Production environments unchanged.

| Suite | Exact base | Head | New failures |
| --- | --- | --- | --- |
| Contracts | 735 pass (+232 subtests) | 735 pass (+232 subtests) | 0 |
| Matchmaker | 329 pass (+17 subtests) | 353 pass (+17 subtests) | 0 |
| Social | 1879 pass / 36 fail | 1898 pass / 34 fail | 0 |
| Risk | 236 pass | 236 pass | 0 |
| Focused Social identity/writers | — | 215 pass | 0 |

All 34 common Social failure names and exact failure messages are identical.
Existing auth/Pi/stream fixture failures are not presented as a green full suite.
Two base-only stream outcomes are not claimed fixed by this patch:

- `tests.test_pi_feedback_regressions::test_safe_provider_sentence_is_published_before_completion`
- `tests.test_pi_feedback_regressions::test_unsafe_provider_stream_is_held_before_public_tokens`

JUnit and full exact failure-message evidence are retained in the private local
operator gate report. A mismatched-SDK preliminary run and sandbox-stalled runs
are excluded from the differential, not silently treated as pass.

## Real disposable integration

54 scenarios PASS using fresh ownership-verified loopback-only Neo4j 2026.08.1 and
Mongo 8.2.5 fixtures, actual Graph/Mongo transactions and private HMAC boundary.
No provider calls; owner auth is an explicit synthetic override, not production
authentication evidence. This is not an alternative service startup procedure.

Includes legacy PREFERS/AVOIDS restore → verified v2, polarity-correct queue,
retired-source replay rejection, failed normalization without writes, v2 restore,
legacy rollback rejection without Graph/Mongo side effects, v2-only rollback and
shared-node preservation, v2 manual projection and rejected legacy payload without
node creation. Offline tests also cover atomic rollback after target creation,
concurrent restore, target conflicts, projection capacity, and historical CLI
rejection before secret loading/connections.

## Release boundary

Deploy only after the differential/review/base gates, via the clean release
checkout and canonical `start_all.sh`. Keep current activation flags/kill policy;
no bulk migration. Production disposable smokes must use unique synthetic owner
and Concept locators and exact cleanup, with real-user data hashes preserved.

Population metrics must name their scope: the accepted migration concerns current
enabled accounts. At preflight, enabled owners had zero active legacy edges;
two legacy edges belonged to non-enabled accounts outside migration authority.
Do not silently repair them or claim the entire historical Graph has zero legacy.
