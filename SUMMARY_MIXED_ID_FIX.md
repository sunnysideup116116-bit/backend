# Mixed conversation message ID repair — 2026-09-15

## Root cause and scope

Ordinary Mongo messages use ObjectId; idempotent system notifications use `system-event:<64 lowercase hex>`. The old exact-batch reader converted every ID to ObjectId. A legitimate system notification therefore raised InvalidId and was mislabeled `source_unavailable`. Three retries led to a failed maintenance job, which ordinary turns deliberately do not auto-reopen. Existing valid summaries remained usable but stopped advancing.

Read-only reproduction found the same cause in all three failed `source_unavailable` jobs. The target account's batch contained ten ObjectIds and one system-event ID. The repaired reader loaded all eleven messages in each of the three batches without a model call or production write. All event contents remained excluded. A read-only inventory found 1,870 public message IDs matching supported formats; this is a snapshot, not a claim about future unknown types.

## Repair contract

- Shared ID decoder accepts the two actual supported formats; unknown formats fail closed as `source_invalid_id` rather than masquerading as a transient storage failure.
- Exact source loading retains room and sender checks. Missing/foreign messages still fail closed.
- Validated summary cursors can end on a system event. Same-timestamp database and in-memory history comparisons agree with BSON ordering (string before ObjectId), including Mongo's type-bracketed comparisons.
- System-event contents cannot enter summary prompts even if message-use metadata is accidentally marked ordinary. Excluded messages can advance processing coverage without becoming user memory.
- Worker blocks unsupported formats without three identical retries. Temporary source/storage errors retain bounded retries.
- Existing valid v5 summaries are retained; no source message or event is deleted/rekeyed. Model policy remains v5; the separately bound deployment fingerprint changes and requires renewed approval.
- Fingerprint now includes the ID decoder, order key, source loader, watermark query and baseline validator. The benchmark uses the same fingerprint function as approval, preventing duplicated fingerprint lists from drifting.

## Verification

- Full isolated Social regression: **1,364 passed + 63 subtests**, 35.33 seconds.
- Focused regression: **49 passed**.
- New mixed-ID tests: system event at head/middle/tail; system-event watermark reuse; excluded-only batch without model calls; equal-timestamp ordering for both ID types; missing/foreign/wrong-sender sources; malformed ID vs database failure; failed-job recovery followed by another compaction cycle; unsupported-ID blocked state.
- Initial new-test failures came from feeding the raw Mongo `_id` into a strict schema rather than using the real projection. Tests now use `_load_current_compaction`, and the complete suite was rerun. No runtime schema strictness was weakened.
- Real-model synthetic benchmark: **50/50**, ten scenario families, no reviews/unavailable/critical assertion failures, stored in `validation/summary-mixed-id-benchmark-2026-09-15.json`. In-memory database only; not inserted into live success metrics.
- Fingerprint: `7cda4ffc4e9c1ce83ecb9a39b8c7ff1c0620a7e171e773c7408b74ba116bd960`.
- `bash -n start_all.sh` and whitespace checks passed.

## Controlled deployment and recovery

Work is isolated from concurrent uncommitted Pi/voice work. Do not load unfinished teammate changes merely to restart the summary worker. Confirm the shared deployment checkout is ready, integrate without overwriting it, and restart **only through `Server/start_all.sh`**. Existing explicit approval intentionally becomes invalid for the new fingerprint until renewed; do not patch approval records by hand or add a demo bypass.

From the deployed Server root, after restart and health checks:

```bash
.local-venv/social/bin/python scripts/manage_summary_rollout.py approve-benchmark --report validation/summary-mixed-id-benchmark-2026-09-15.json
.local-venv/social/bin/python scripts/manage_summary_rollout.py approve-benchmark --report validation/summary-mixed-id-benchmark-2026-09-15.json --apply
.local-venv/social/bin/python scripts/recover_summary_mixed_ids.py
.local-venv/social/bin/python scripts/recover_summary_mixed_ids.py --apply
```

Recovery is dry-run first and selects only current-policy failed `source_unavailable` jobs whose next batch actually contains a valid system-event ID and can now be loaded in full. It does not approve or retry quality-review jobs, delete messages, or skip missing sources. Dry-run confirmed three eligible jobs. The operator must verify the running worker has the deployed fix before applying recovery.

After recovery, verify the target room's revision/watermark advances past the system event, pending count falls, and authenticated status reports usable summary. Verify a subsequent ordinary turn schedules further maintenance; preserve any genuine review/failure outcome rather than forcing it through. Complete a public Pi recall check with facts absent from the raw recent-message window. Approval and aggregate quality remain separate: 50 synthetic passes are not 50 live user samples.

**Deployment/recovery not yet performed at this implementation checkpoint.** The original jobs and approval record were left unchanged pending restart coordination with concurrent Pi/voice work. This document records a verified code repair, not completed all-account production acceptance.
