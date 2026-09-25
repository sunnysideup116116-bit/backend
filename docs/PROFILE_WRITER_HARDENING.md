# Profile writer uniqueness boundary

Base: backend main `d90d100`. Implementation only; **not deployed**. No production
data repair, index creation, Graph constraint, feature enablement or P1-B change.

## Contract and owner

`services/profile_writer.py` owns **profile creation and confirmed E11000 recovery**.
The database enforces one `user_id` → one profile through a separately approved
`UNIQUE(user_id)` index. This helper does not create that index, guarantee uniqueness
without it, select among legacy duplicates, or fix existing data. Existing domain
services retain field ownership, authorization, confirmation, CAS and idempotency.

- `ensure_profile(collection, owner, defaults=None)`: `$setOnInsert` only. Existing
  values beat every initialization/default, including empty stubs. Defaults are
  not a request to fill/overwrite fields in an existing profile; registration's
  explicit field updates still use their original owning domain logic.
- `update_profile(collection, query, update, upsert=...)`: operator-only updates.
  Upsert can use **only** `{"user_id": owner}`. A conditional/CAS predicate must
  use `upsert=False`; a miss remains a miss, not evidence the profile is absent.
- Owner identity is nonempty string, no trimming/case folding/re-key. `_id` and
  `user_id` cannot be modified (`$setOnInsert.user_id` may equal the bound owner).
  Replacements/pipelines/identity rewrites are rejected before any database call.
- An explicit user edit can still change an allowed field under its existing
  domain policy. The helper does not reinterpret an intentional edit as a stub,
  change field precedence or silently discard updates.

## Recovery is bounded and evidence-based

Normal success path remains one `update_one`, same predicate/operators and return
value; no extra profile read, schema query, transaction or provider call.

Only `DuplicateKeyError(code=11000, keyPattern={user_id:1}, keyValue={user_id:owner})`
may recover. We do not parse private error text or infer identity from index names.

1. Read at most two `_id/user_id` rows. Exactly one same-owner winner is required;
   missing/ambiguous identity fails closed instead of picking a legacy duplicate.
2. Apply the unchanged operator payload once, bound to that winner's `_id` AND
   owner, **upsert=False**. A disappeared/recreated winner is a conflict, not a new
   insert. `$setOnInsert` stays insert-only and does nothing to the winner.
3. At most one recovery write. A second E11000 propagates. Other index errors,
   incomplete metadata, transport errors, timeouts and unknown outcomes never
   trigger helper retries. Existing event/message/operation IDs are unchanged.
4. Recovery read+write share one 2s PyMongo CSOT budget; an enclosing shorter CSOT
   deadline is not extended. Fast-path timeout behavior remains unchanged.
5. If the call entered within a Mongo transaction, propagate E11000 to its owning
   coordinator, even if the driver marks it aborted. Do not continue the aborted
   transaction or replay earlier Graph/domain effects. Bootstrap reconciliation
   remains the authority for Graph-committed/Mongo-pending operations.

No raw profile, error message, owner ID or payload is newly logged.

## All 27 online create/upsert call sites

This is an operation classification, not a new user-facing API. Runtime imports
the shared updater as `write_profile` to avoid router-name collisions.

| Domain/file | Functions (site count) | Classification and retained semantics |
| --- | --- | --- |
| assessment_session_service.py | start_assessment_session (1), handle_assessment_ui_message (1) | **create-if-absent / registration** → ensure_profile; subsequent conditional interest/session updates stay update-only |
| routers/system.py | update_profile (1), complete_onboarding (1) | **registration/profile bootstrap** → original explicit fields/projection and consent-complete state |
| ayue_agent/onboarding.py | ensure_public_ayue_onboarding (5), complete_public_ayue_onboarding (1) | **onboarding initialization** → version `$max`; message-level idempotency unchanged |
| routers/system.py | update_settings (1), update_profile_location (1), update_mediator_tone (1) | Existing settings/location/tone operator update, owner-only create-if-missing |
| routers/calendar.py | update_settings (1) | Calendar access preference; no calendar lifecycle change |
| routers/push.py | report_presence (1) | Foreground timestamp initialization, no notification dispatch change |
| routers/public_chat.py | _run_public_stream_turn (1), direct_chat (2) | Public activity timestamp initialization; auth/stream/model unchanged |
| routers/private_mediator.py | mediator_private_chat (1) | Private activity timestamp initialization; accepted-pair guard unchanged |
| memory_service.py | apply_profile_memory_proposals (1) | **memory initialization** → original notice `$push`; never redo Graph mutation on recovery |
| notification_service.py | update_notification_preference (1) | Original notification fields and array operators |
| mediator_event_service.py | queue_mediator_event (1) | **conditional creation**: without event_key owner-only upsert; with event_key dedupe predicate and upsert=False, so miss cannot create |
| routers/match.py | _set_match_search (1) | Original search status projection |
| match_search_job_service.py | enqueue_match_search (1) | Background search projection; job uniqueness/quota/consent unchanged |
| profile_task_service.py | queue_profile_skills (1) | Background process projection; original error/failure behavior preserved |
| proactive_followup_service.py | record_owner_activity (1) | Activity initialization; optional effect semantics preserved |
| ayue_agent/proactive_care.py | schedule_proactive_care (1) | Original care scheduling `$set/$unset`; no new scheduling policy |

Total: **2 insert-only ensures + 24 owner-only operator upserts + 1 guarded event
branch = 27 sites in 15 caller files**. Registration and background labels overlap
the operation categories; they are not additional call sites.

The prior 92-site inventory also included 63 non-creating mutations plus one
explicit destructive demo `insert_many` and one offline seed upsert. Update-only
CAS, bootstrap projection transactions, correction/retirement and ordinary
revision fences are left intact. Demo/offline seed/migration tools remain outside
online traffic and must be stopped during remediation; they are not deleted or
automatically invoked. The AST contract fails if an online creator bypasses this
shared boundary. There is no modification to the production 10 duplicate groups.

## Tests / limitations

See `tests/readiness/profile_writers/README.md` for isolated real replica-set
rehearsal and commands. Coverage includes real Mongo E11000 metadata, transaction
abort, lost acknowledgement, fresh creation vs guarded update, registration vs
memory, rich-vs-empty initialization, all27 wiring, and repeated high concurrency.

No local concurrency test proves production deployment/schema ready. Production
Mongo version8.0.32 and local8.2.5 are distinct; the local baseline exercises the
documented unique-index/transaction contract, not a claim of identical deployment.

## Production remediation runbook remains applicable

1. Review/merge this implementation; do NOT deploy or run production remediation
   merely because tests pass. Keep bootstrap/semantic/related-interest OFF.
2. Once separately approved, quiesce every profile creator and state writer,
   including read endpoints that refresh cache, plus old processes/manual tools.
   Deploy all shared-boundary callers as one clean release via start_all.sh in
   that controlled window. Do not resume online first-create traffic before the
   unique index exists: helper-only is not a substitute for DB uniqueness.
3. Re-run the private read-only forensic inventory and take fresh before images.
   Archive/retire only separately approved exact duplicate document IDs; preserve
   canonical fields and all owner-linked Appwrite/Graph/history. No repair script
   or private account data is included here.
4. Under the same stopped-write window, separately approve/create and verify the
   user_id unique index. Check missing/invalid owners, collation, duplicates,
   correct index schema and all caller paths before resume.
5. Separately approve the missing Neo4j operation uniqueness constraint and re-run
   bootstrap readiness. Only after that PASS and explicit authorization may
   temporary-account smoke run. Real cohort bootstrap remains a later decision.
6. Rollback is not blindly reinserting duplicates under a unique index. Existing
   private repair plan's archive/hash/revision guard and separately approved index
   rollback remain mandatory. No rollback to old creators while new traffic is
   allowed; no automatic index deletion or Graph/data migration.

This implements the code-hardening prerequisite, not the data/schema steps. The
public/private Pi architecture, durable Identity-v2, P0 matching, Event behavior,
semantic policy/threshold, providers and feature defaults are unchanged.

References: [MongoDB updateOne](https://www.mongodb.com/docs/manual/reference/method/db.collection.updateone/),
[PyMongo CSOT](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/connect/connection-options/csot/).
