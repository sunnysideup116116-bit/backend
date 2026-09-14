# Summary rollout operations acceptance — 2026-09-14

## Completed implementation

1. Reproducible synthetic v5 benchmark: 50 cases, 10 families, real generator and evaluator; in-memory database only. All 50 passed; no unavailable results, review results, excluded-string leaks or assertion-detected false acceptance. This is not 50 production samples.
2. Owner-authenticated, read-only summary status API and explicit rebuilding API; persistent jobs with deduplication, lease renewal/recovery, bounded retries and batch limits. Existing profile/source eligibility remains intact.
3. Explicit operator approval separated from live health, bound to policy, model, provider and algorithm fingerprint. Inactivity does not revoke approval. Sufficient evidence of degraded live quality pauses it; operator pause remains available.

## Verification

- Full Social regression: **1,347 passed, 63 subtests passed**, 35.58 seconds. Existing deprecation warnings remain.
- Focused final summary/compaction regression: **75 passed, 11 subtests passed**.
- `bash -n start_all.sh` and staged whitespace check passed.
- GitNexus pre-commit change analysis: 73 changed symbols, 24 affected symbols, 15 files; critical aggregate risk; `partial=false`, `truncated=false`, no empty symbol IDs. The global context gate is a high-impact path. Index process enumeration itself is bounded and is not proof of all possible runtime paths.
- Approval dry run with production configuration validated the report; **applied=false**.
- Report SHA-256: `22938259182522b0b40e8bac8130a523b2967a7431ad9fe649b8a7d34d11f4e0`.

## Read-only production inventory

Bounded scan covered all 88 enumerated profile-backed legacy/normal rooms, `truncated=false`:

| State | Rooms |
|---|---:|
| not_needed | 77 |
| rebuild_needed | 10 |
| ready | 1 |

No jobs were queued by this scan. These are rooms, not account counts. Short conversations are expected to use recent raw history rather than have a summary.

Current new approval state is `unapproved`; live v5 sample count is 2, pass rate 1.0, health `insufficient_data`. The original minimum 50 live samples has **not** been met.

## Deployment not yet performed

Implementation is isolated in branch `feature/summary-rollout-operations`, based on `cef5337`, with no Pi code edits. During final checks the shared Server acquired uncommitted changes in:

- `social/services/ayue_agent/pi/public_turn.py`
- `social/services/ayue_agent/pi/runtime.py`
- `social/tests/test_pi_feedback_regressions.py`

Those changes were not modified, committed, discarded or deployed by this work. Production was not restarted and no global approval or environment change was applied. The new HTTP endpoints and durable worker therefore have offline coverage, **not deployed end-to-end acceptance yet**.

Before declaring controlled all-account operation:

1. Confirm the current Pi work is ready for restart; integrate without replacing its changes, and run combined regression.
2. Apply the reviewed benchmark approval, use `AYUE_CONVERSATION_CONTEXT_USER_ALLOWLIST=*` without a demo bypass, retain context `on` and compaction generation `shadow`, and restart through `start_all.sh`.
3. Authenticate the demo account against the public API; verify summary status, ownership rejection, durable rebuilding and Pi recall with the relevant facts outside the recent-message window.
4. Review and enqueue the existing-room backlog, then inspect job outcomes and live health. Report pending/blocked rooms honestly; do not equate global eligibility with every room already containing a summary.

See [rollout runbook](../SUMMARY_ROLLOUT_OPERATIONS.md) for approval, pause and backfill commands.
