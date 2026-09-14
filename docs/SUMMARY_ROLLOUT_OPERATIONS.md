# Summary v5: controlled global rollout and maintenance

## What is being approved

There are two distinct evidence sets:

- Live `conversation_compaction_shadow_runs` remains unchanged. Its minimum 50 samples and quality thresholds are still reported by the original readiness function.
- The independent synthetic benchmark contains 50 cases across 10 scenario families, five casts per family. Its report is stored in `validation/summary-v5-benchmark.json`, not inserted into live evaluation statistics. It checks expected facts and excluded strings in addition to the model evaluator; these bounded assertions are not a complete semantic proof.

A local operator may approve **controlled global trial** using a qualifying benchmark. Approval is stored separately in `conversation_summary_rollouts`, bound to the exact compaction policy, model, provider, algorithm fingerprint and report digest. It does not claim that the live 50-sample gate passed. This is an explicit deployment-policy change from an exclusively live-readiness-gated rollout.

The existing `CONTEXT_MODE=on` and `CONTEXT_USER_ALLOWLIST=*` are still required. Each room's summary must independently pass owner, room, policy and evaluation checks; approval does not inject invalid summaries or turn temporary conversation into durable preferences. New accounts use the same rule without listing individual IDs.

## Approval, health and pause

From Server root:

```bash
.local-venv/social/bin/python scripts/manage_summary_rollout.py status
.local-venv/social/bin/python scripts/manage_summary_rollout.py approve-benchmark --report docs/validation/summary-v5-benchmark.json
.local-venv/social/bin/python scripts/manage_summary_rollout.py approve-benchmark --report docs/validation/summary-v5-benchmark.json --apply
.local-venv/social/bin/python scripts/manage_summary_rollout.py pause --apply
```

Approval is operator-only; no public HTTP endpoint, model tool or user profile field can approve rollout. The report must be recent (within seven days), cover at least 50 unique cases and 10 families, match the running algorithm/model/provider, pass at least 95%, have at most 2% unavailable and 5% review, and contain no detected critical false acceptance/excluded-content leakage. The dry run validates the same conditions without writing.

An approved rollout survives absent/stale observations. Health reports `insufficient_data` or `stale` separately. Once live evidence meets the original sample minimum, degraded quality causes the maintenance worker to persist `paused`; quality recovery alone cannot silently reactivate it. Missing approval preserves legacy readiness behavior. Explicit pause overrides legacy readiness; canary IDs remain deliberate exceptions, so use `*` alone for uniform global control. Consumers cache their decision for up to 60 seconds. `CONTEXT_MODE=off` plus a normal restart disables all summary consumption.

Policy/model/provider/fingerprint changes invalidate approval. A rollout record or health-storage outage cannot authorize new consumption; the Context Builder remains read-only. There is no automatic assumption that every provider failure proves existing summaries are wrong.

## Owner-visible progress API

Both endpoints derive owner identity from an Appwrite Bearer JWT. They never accept a caller-supplied owner ID, return raw summary text, expose credentials, or accept rollout approval.

```http
GET /api/conversation/summary-status?room_id=<owned-room>
Authorization: Bearer <Appwrite JWT>

POST /api/conversation/summary-rebuild
Authorization: Bearer <Appwrite JWT>
Content-Type: application/json

{"room_id":"<owned-room>"}
```

GET is read-only. POST queues/retries work for the authenticated owner's normal Public AI room. Hub, proposal rooms and other owners' rooms are rejected. No new App screen is included in this change; the status API and operator CLI make progress inspectable without conflating it with ordinary chat responses.

Status includes `state`, `rebuild_state`, `summary_available`, `injection_enabled`, `pending_message_count`, `processed_batches`, `last_result_code`, and `updated_at`. States: `not_needed`, `ready`, `rebuild_needed`, `queued`, `running`, `retry_wait`, `blocked`, `failed`. A valid older current-policy summary can remain usable while a later batch retries. Short conversations need no summary and continue using raw recent history.

## Persistent rebuilding

Normal completed public turns enqueue maintenance into `conversation_summary_jobs`; jobs store only owner/room bindings and operational metadata. A single daemon worker, started/stopped by Social's lifecycle through the unchanged `start_all.sh`, polls every ten seconds. It uses atomic claims and a renewed lease, bounded retry (three attempts), and at most 50 batches per job. Repeated queue requests do not duplicate active jobs. Room ownership is rechecked at execution. Profile coverage runs before compaction using the existing source-eligibility rules. No-memory, calendar, assessment and unmarked legacy content are not newly reclassified as reusable.

Operator backfill is explicit and dry-run first:

```bash
.local-venv/social/bin/python scripts/manage_summary_rollout.py queue-existing --limit 200
.local-venv/social/bin/python scripts/manage_summary_rollout.py queue-existing --limit 200 --apply
```

This enumerates bounded profile/normal-room metadata and reports counts only. It does not delete old summaries or raw messages. V4 rooms regenerate from eligible source under v5; excluded-only batches advance coverage without a model call. A single message over 9,000 normalized characters blocks that room's compaction without skipping its tail; explicit retry can resume after the source issue is resolved. A 50-batch cap requires an explicit retry. `AYUE_SUMMARY_REBUILD_WORKER_ENABLED=off` stops new worker startup, leaving durable jobs recoverable.

## Reproducible verification

```bash
.local-venv/social/bin/python scripts/validate_summary_rollout.py --output /tmp/summary-benchmark.json
.local-venv/social/bin/python scripts/validate_summary_rollout.py --live --output /tmp/summary-benchmark.json
.local-venv/social/bin/python scripts/run_offline_tests.py social -k 'summary_operations or compaction'
```

The live benchmark requires mongomock/test dependencies and model access. It replaces the database module with an in-memory implementation before loading services, and forces the recorded Ollama provider. Two concurrent cases maximum; all final results and retries are retained. Fixture output is not written into live success counters. The accompanying report has 50/50 accepted cases; semantic coverage remains limited to this corpus.
