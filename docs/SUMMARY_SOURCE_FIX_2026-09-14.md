# Complete-source compaction and empty-summary guard

## Changes

Policy `conversation_compaction_policy_v5` removes per-message head truncation. Selection takes a contiguous prefix of complete reusable messages within the existing 9,000-character source budget. Generation and evaluation consume the same complete text. The worker rechecks the loaded messages before hashing or storing, so queued source growth cannot advance a watermark over an unprocessed tail.

A single reusable message larger than 9,000 normalized characters is deferred with `source_over_budget`. It is not skipped or partially marked covered. This conservatively blocks further compaction at that message until a future explicitly tracked chunking policy is implemented. It does not delete the raw conversation.

A generated summary with six empty arrays and nonempty reusable source raises a typed `empty_summary` failure. The existing retry mechanism permits one repair attempt. If both attempts fail, generation metadata is recorded but the evaluator is not invoked and the last approved summary/watermark is unchanged. Legitimate low-information chat may be conservatively deferred; the model is instructed not to invent content to fill a summary.

The evaluator prompt explicitly checks corrections, established facts and unresolved questions. This is additional guidance, not a guarantee that every nonempty semantic error is detected.

Older-policy summaries cannot be injected or used as recursive baselines. Their raw source remains available for regeneration. Current-policy passed summaries remain available during later generation/evaluation failures. Global rollout thresholds are unchanged.

## Verification

- Focused compaction/message-use/owner-refresh suite: **74 passed, 11 subtests passed**. Includes 13 new cases covering head/middle/tail corrections, complete batch boundaries, exact budget, raw-owner source, oversized deferral, empty retry/recovery, last-approved preservation, excluded-only advancement, and old-policy rejection.
- Full isolated Social suite: **1979 passed, 20 skipped, 15 failed, 148 subtests passed**. All 15 failed node IDs reproduced on unchanged `f081257` (targeted baseline: 70 passed, 15 failed); no unrelated tests or runtime code were edited to hide those failures. Full tests required sandbox-external execution with the same network-blocking offline runner.
- Real-model matrix: 10 synthetic scenarios, 20 successful provider calls. Strict literal checks passed 9/10; manual review confirmed 10/10 expected meanings, with one Simplified/Traditional Chinese spelling variation. This is a small controlled sample, not a production pass-rate claim. No production Mongo writes or rollout-sample inflation.
- The original failing 1473-character case now sends all 1473 characters to both stages. Actual generated continuity: `故事主角確定為白鷺，先前暫用的青松已作廢。` The evaluator passed it. Previously only 900 characters were supplied and the empty summary incorrectly passed.
- `bash -n start_all.sh` and whitespace checks passed.

## Delivery boundary

Implemented in an isolated DAG checkout on top of `f081257` plus the earlier documentation commit. Pi and the shared development checkout are unchanged. This fix has not been deployed to the running public service, so the earlier v4 demo HTTP acceptance must not be presented as v5 deployment acceptance.

Before deployment, integrate this shared compaction change with the intended runtime, retain the original `start_all.sh` entry point, then regenerate a v5 canary summary and repeat public-API recall with the answer outside recent history. Policy v5 has a separate readiness sample set; no global gate is weakened here. Old summaries temporarily stop being injected until they are regenerated under the new policy.
