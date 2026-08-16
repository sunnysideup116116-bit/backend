# Ayue V3 Backend Change Record

This file records only integration changes to the canonical Ayue V3 backend import: the reason for each change and the behavior before and after it.

## Source baseline

- Upstream repository: `https://github.com/chenjia0510/ayue_for_demo.git`
- Upstream branch: `main`
- Upstream commit: `dfe78136ad75a911c8b9fae95e1e9da0b97752cf`
- Canonical imported location: `Server/`
- Import date: 2026-08-12 (Asia/Taipei)

## Change 001: Preserve the new backend as an isolated canonical subtree

**Reason:** The existing Server repository contains the old `main_app`, matchmaker, risk backend, and AI Gen components. Importing the new backend over those paths would overwrite existing implementation and mix incompatible old and new logic.

**Before:**

- The new backend existed only at `/home/sunny/下載/ayue_for_demo-main`.
- The Server repository contained only the previous backend layout.
- The downloaded source had no usable Git history of its own.

**After:**

- The verified upstream source is preserved at `Server/`.
- Existing Server components remain unchanged in their original paths.
- New/old behavior conflicts will be resolved in favor of `Server/`.
- The bundled `social/frontend.html` and image assets remain the visual reference for Flutter integration.
- The local-only `social.env` and upstream `.git` directory were not imported.

**Canonical backend files modified:** None. The upstream files were imported without content changes.

## Change 002: Use isolated dependency environments

**Reason:** The new social backend and matchmaker pin incompatible FastAPI, Neo4j, OpenAI SDK, and related dependency versions. A shared virtual environment would silently replace one service's required versions with the other's.

**Before:**

- No runnable Python environment existed inside the imported subtree.
- The old Server virtual environment did not satisfy the complete new dependency set.

**After:**

- `ayue_for_demo/.local-venv/social` is reserved for `social/requirements.txt`.
- `ayue_for_demo/.local-venv/matchmaker` is reserved for `matchmaker_agent/requirements.txt`.
- Both directories remain ignored by the imported `.gitignore`.
- Neither upstream requirements file was changed.

**Canonical backend files modified:** None.

## Change 003: Split runtime configuration by service

**Reason:** The supplied `social.env` is not loaded automatically from the canonical service working directories, and the social and matchmaker services own different environment contracts. Copying the complete Server environment into both processes would expose unrelated credentials.

**Before:**

- The supplied secrets existed only in `/home/sunny/下載/ayue_for_demo-main/social.env`.
- Social expected a working-directory `.env`.
- Matchmaker explicitly expected `matchmaker_agent/.env`.
- Required matchmaker values were available only in the existing Server environment.

**After:**

- `scripts/provision_ayue_v3_env.sh` selects only canonical social keys into ignored `social/.env`.
- It selects only matchmaker LLM, Neo4j, and ranking keys into ignored `matchmaker_agent/.env`.
- Both generated files use permission mode `600`.
- Removed rollout flags are not migrated.
- `docs/ayue-v3-environment-matrix.md` documents names, ownership, defaults, and validation without secret values.

**Canonical backend files modified:** None. Both generated `.env` files are local ignored configuration.

## Change 004: Add non-destructive Linux service tooling

**Reason:** The canonical source provides Windows launchers, while the integration host is Linux. Existing ports may belong to running old services and must not be killed implicitly.

**Before:**

- No Linux launcher selected the two isolated Python environments.
- No unified read-only check covered Ayue Social, Matchmaker, and Risk services.

**After:**

- `scripts/run_ayue_social.sh` starts canonical Social from its required working directory and configurable port.
- `scripts/run_ayue_matchmaker.sh` starts canonical Matchmaker from its required working directory and configurable port.
- Both launchers refuse occupied ports without stopping any process.
- `scripts/check_ayue_services.sh` performs only health requests.
- `scripts/validate_ayue_v3_environment.py` validates required keys and optional service reachability without importing service modules or sending model prompts.

**Canonical backend files modified:** None.

## Change 005: Add mobile and visual integration contracts

**Reason:** Flutter must reproduce the bundled website's cards, dialogs, progress states, and guarded actions without coupling UI behavior to reply wording.

**Before:**

- The visual behavior existed only as HTML/CSS/JavaScript in the demo website.
- Flutter had no versioned contract for NDJSON events, final responses, match decisions, bootstrap identity, calendar/date coordination, relationship quiz, or feature availability.

**After:**

- `docs/ayue-v3-ui-contract.md` maps website states and actions to intended Flutter components.
- `docs/api/` contains versioned stream, response, match, bootstrap, calendar/relationship, safety, and capability contracts.
- `tests/contracts/fixtures/ayue_v3_contracts.py` provides a reference NDJSON decoder, public event validator, and match-card client guards.
- `tests/contracts/fixtures/ayue_v3_capabilities.py` derives safe place-card availability from configuration without exposing keys.
- Match revision remains optional to match the canonical website's current status-CAS behavior; clients echo revision only when supplied.
- Risk projection remains advertised as unavailable until it is wired into the canonical HTTP path.

**Canonical backend files modified:** None.

## Change 006: Add an unwired pre-persistence risk adapter

**Reason:** The canonical Ayue V3 source does not include the existing risk service, but risk enforcement must remain mandatory policy and must not become a Planner-selected tool. Wiring cannot proceed until GitNexus can analyze the exact canonical persistence symbols.

**Before:**

- No integration-safe adapter expressed `restricted` as deliverable and `blocked` as non-deliverable for the canonical V3 path.
- A retry contract did not prevent repeated risk evaluation or repeated persistence at the adapter boundary.

**After:**

- `tests/contracts/fixtures/ayue_v3_risk_adapter.py` defines bounded risk requests, an idempotency cache, explicit allow/block/local-guard policies, a single-use persistence permit, and a receiver-history filter.
- Public projection excludes raw diagnosis, risk state, classifier evidence, and prompts.
- The adapter is intentionally not imported by canonical routers and therefore does not change live backend behavior yet.

**Canonical backend files modified:** None.

## Change 007: Add a dedicated Flutter V3 API boundary

**Reason:** The Flutter client previously mixed legacy JSON calls, immediate match results, and removed initialization routes in one shared service. The new backend requires typed additive responses, durable match search, compare-and-set decisions, and canonical Big Five initialization.

**Before:**

- Big Five initialization called the removed `/api/profile/big-five/initialize` route.
- Public Ayue, match search, match decisions, Calendar, and relationship dates had no isolated V3 client boundary.
- Incoming invitation actions used the legacy accept and decline facades.

**After:**

- `DatingApp/lib/services/ayue_v3_api_service.dart` owns Public V3 streaming, durable match search, guarded decisions, Calendar, and relationship-date contracts.
- Big Five initialization uses `POST /api/chat` with `state=big_five` and `initialize=true` while retaining the Appwrite account ID as `user_id`.
- Incoming invitation actions use `/api/match/decision` with `expected_status=pending`; stale decisions refresh state and are never replayed automatically.

**Canonical backend files modified:** None.

## Change 008: Replace Public Ayue JSON sending with one NDJSON stream

**Reason:** Public V3 emits progress and typed presentation state through `/api/direct_chat/stream`. Calling the JSON and stream routes together would persist the owner message twice, while treating the stream as a complete body would lose incremental progress and disconnect handling.

**Before:**

- Flutter sent Public Ayue messages through `/api/direct_chat` and rendered only one `reply` string.
- A generic thinking indicator appeared immediately and did not reflect typed tool progress.
- Presentation blocks, place cards, sources, context confirmation, and Calendar-change state were not rendered.

**After:**

- One Public send opens one NDJSON request and accepts only the five allowlisted event types.
- The progress bubble appears after 250 ms, remains singular, updates from bounded `tool_started.text`, and is cleared on final, error, disconnect, or timeout.
- Final `messages` render as separate bubbles, with `reply` as fallback; typed presentation blocks, place cards, sources, context confirmation, and Calendar-change notices render as additive surfaces.

**Canonical backend files modified:** None.

## Change 009: Move stateful Flutter surfaces to canonical server state

**Reason:** Match, Calendar, shared-date, and Private Ayue interfaces must follow server-owned status, revision, ownership, and Public/Private runtime separation instead of deriving state from prose.

**Before:**

- Match search expected immediate candidates from `/api/match`.
- Match cards used compatibility accept and decline routes without an explicit expected status.
- Flutter had no canonical Calendar agenda or shared-date state card.
- Private Ayue used only the non-streaming compatibility response.

**After:**

- Match search uses the durable confirmed `/api/match/request` flow and displays the polled server progress state with a bounded cancel action.
- Match decisions include visible-card authority, prevent duplicate taps, and refresh without replay after HTTP 409.
- Flutter includes a typed Calendar agenda/editor, mediator-access toggle, revision-bound cancellation, and a shared-date confirmation card.
- Private Ayue remains isolated on `/api/mediator/private/stream` and does not reuse Public V3 context or state.

**Canonical backend files modified:** None.

## Change 010: Complete the remaining typed Flutter surfaces

**Reason:** Assessment exit, recent-context confirmation, accepted-contact mentions, relationship-quiz cancellation, canonical proactive frequency, and owner memory correction were still represented only partially or through removed V1 behavior.

**Before:**

- Public Ayue displayed assessment and context metadata only as passive message extras.
- Flutter retained the removed relationship-topic action and hid the quiz card immediately after the owner answered.
- Settings offered the obsolete `600` frequency and had no typed memory correction screen.

**After:**

- Public Ayue restores assessment state from history, sends typed cancel actions, performs bounded recent-context polling, and limits mentions to canonical accepted contacts.
- Relationship quiz keeps waiting/result state visible, supports canonical cancellation, and no longer calls the removed topic route.
- Settings use `none`, `60`, `3600`, or `86400`, and owner memories can be viewed and disabled by server key.

**Canonical backend files modified:** None.

## Change 011: Enforce pair-chat risk policy before persistence

**Reason:** The existing risk backend was not connected to the imported V3 pair-chat persistence path. Receiver-visible messages require a mandatory safety decision without making risk a Public V3 Planner tool or treating `restricted` as blocked.

**Before:**

- Pair-chat owner messages were stored before any risk decision.
- Retry attempts had no client-scoped idempotency key at this boundary.
- Message history did not explicitly exclude blocked documents.

**After:**

- The non-Ayue pair-chat branch evaluates risk before storage; `safe`, `observation`, `warning`, and `restricted` remain deliverable, while `blocked` or unavailable decisions are not persisted.
- Flutter supplies `client_message_id`; allowed owner messages use deterministic insert-once storage and return only a bounded risk projection.
- Receiver history excludes `is_blocked=true`; Public V3 and Private V2 flows remain outside this pair-chat gate.

**Canonical backend files modified:**

- `ayue_for_demo/social/models.py`
- `ayue_for_demo/social/routers/public_chat.py`
- `ayue_for_demo/social/routers/chat_messages.py`
- `ayue_for_demo/social/services/chat_service.py`
- `ayue_for_demo/social/services/risk_policy_service.py` (new)

## Change 012: Make the Calendar coordination index valid on deployed MongoDB

**Reason:** Live startup showed that MongoDB rejects an index combining `sparse=true` with `partialFilterExpression`, causing the complete Calendar index setup block to be skipped.

**Before:**

- The unique `coordination_id` index used both `sparse=True` and a `source_type=date` partial filter.
- Startup caught the Mongo error and continued without completing Calendar index setup.

**After:**

- The index remains unique and restricted to `source_type=date` documents through its partial filter.
- The incompatible redundant `sparse` option is absent, allowing Calendar index setup to complete.

**Canonical backend files modified:**

- `ayue_for_demo/social/services/calendar_service.py`

## Change 013: Render typed GIF messages and stabilize Flutter chat history

**Reason:** The canonical backend stores GIPHY reactions as `message_type=gif` with bounded `metadata.media`, while Flutter discarded those fields. The pair-chat screen also combined a reversed list with incremental `AnimatedList` bookkeeping, which could desynchronize after polling or access the first item of an empty server response.

**Before:**

- Flutter retained only sender and text fields from typed chat messages, so GIF reactions appeared as text-only messages or no visible media.
- Pair-chat history relied on backend order, ignored `message_id`, and updated an `AnimatedList` independently from the replacement message list.
- Empty or replaced history responses could leave the visible list stale.

**After:**

- Flutter retains `message_type` and a safe GIPHY media projection for both pair chat and Private Ayue, preferring the preview URL and rejecting non-HTTPS or non-GIPHY hosts.
- GIF messages render animated network images with caption, loading state, and failure placeholder.
- Pair-chat history uses `message_id`, normalizes newest-first ordering, reconciles local pending/failed messages with each server snapshot, and renders from one consistent list state.

**Canonical backend files modified:** None. All runtime changes are confined to `DatingApp/`.

## Change 014: Make pair-chat Risk resilient and align persistent audit state

**Reason:** The imported pair-chat gate treated Risk timeout or transport failure as a blocked delivery, while the deployed Appwrite schema did not match the current Risk history, temporal-feature, audit, and relationship code. The optional Guardrail classifier also shared the NLP retry budget and its failure was indistinguishable from a completed safe check.

**Before:**

- Pair chat waited three seconds by default and returned `not_delivered` for timeout, connection failure, 5xx, malformed responses, or unknown risk levels.
- Runtime configuration did not guarantee that the Social process received the Risk URL and timeout.
- Risk analysis writes and admin reads used `risk_analysis_logs`, while the authoritative collection ID is `risk_analysis_logs_`.
- Existing Appwrite collections retained the incompatible legacy attributes and indexes.
- Guardrail classifier failures retried with the NLP budget and returned the same observable shape as a successful safe check.
- The canonical launch flow did not start or health-check the local Guardrail classifier.

**After:**

- `RISK_TIMEOUT_SEC` is explicitly provisioned as 20 seconds, and 20 seconds is also the client fallback.
- Only an explicit `blocked` Risk response prevents persistence. Transport and response failures are delivered once with the bounded `level=unavailable` projection.
- Risk analysis write, reset, and admin audit paths consistently use `risk_analysis_logs_`.
- A non-destructive Appwrite migration creates a separate target DB from the authoritative dump, copies compatible documents, verifies attributes/indexes, and leaves the source DB unchanged. Runtime configuration switches only after that verification succeeds.
- Guardrail classifier defaults to one attempt and a three-second timeout. Skipped, empty, initialization, and request-failure paths expose `guardrail_degraded=true` without changing Risk scoring.
- Dedicated Guardrail, Risk, Matchmaker, Social, aggregate startup, and four-service health scripts define the canonical local launch flow.

**Canonical backend files/symbols modified:**

- `ayue_for_demo/social/services/risk_policy_service.py`: `PairMessageRiskGate.__init__`, `PairMessageRiskGate.evaluate`
- `main_app/services/risk_client.py`: timeout fallback and `is_blocked` delivery semantics
- `risk_backend/app/services/chat_log_service.py`: `ChatLogService.log_analysis_detail`
- `risk_backend/app/core/llm_adapters.py`: `call_with_retry`, `OpenAICompatAdapter`, `_make_openai_compat`, `get_guardrail_classifier_adapter`
- `risk_backend/app/core/guardrail_engine.py`: `GuardrailEngine.check`, `_check_via_openai_moderation`, `_check_via_classifier`
- `risk_backend/app/models/schemas.py`: `RiskDetectionResponse`
- `risk_backend/app/api/risk_detection.py`: `detect_risk`
- `main_app/routers/admin.py`: `get_audit_logs`
- `risk_backend/app/utils/reset_db.py`: `reset_collections`
- `risk_backend/db_setup/appwrite_schema_dump.json`
- `risk_backend/db_setup/migrate_appwrite_schema.py` (new)
- `tests/contracts/fixtures/ayue_v3_risk_adapter.py`
- `scripts/provision_ayue_v3_env.sh`, `run_ayue_risk.sh`, `run_ayue_guardrail.sh`, `start_ayue_services.sh`, `check_ayue_services.sh`, `validate_ayue_v3_environment.py`
- Risk projection, capability, environment, database setup, and startup documentation

**GitNexus impact and risk:** The pair-chat gate, audit ID, Guardrail execution, and capability changes were LOW risk. `OpenAICompatAdapter` was MEDIUM because NLP, summaries, background judgment, and Guardrail import it; existing Gemini, Ollama, and `extra_body` behavior was preserved. `RiskDetectionResponse` was HIGH because it is imported across the Risk core; the change is an additive boolean field with a default value.

**Rollback:** Restore the prior Risk client policy and configuration, point `APPWRITE_DB_ID` back to the preserved source DB, and stop the added Guardrail process. The migration performs no deletion or update against the source Appwrite database.

## Change 015: Bound pair-chat opening assistance and align shared-date surfaces

**Reason:** The accepted-pair branch generated and persisted a receiver-authored LLM reply after every user message. Flutter also discarded typed mediator-card metadata, so it reconstructed the shared date form inside Private Ayue and continued exposing a relationship-quiz UI that the canonical website no longer presents.

**Before:**

- Every accepted-pair message invoked the chat model and stored its output with the other participant's sender ID.
- The website appended the API `reply` even when no pair-chat assistance was needed.
- Flutter retained `message_type` but not the full bounded message metadata needed to render `date_coordination_*` cards.
- Private Ayue polled relationship-quiz and date state, displayed both cards locally, and offered the removed quiz action.
- The shared-date editor used undifferentiated text fields for date and time.

**After:**

- The backend generates one opening assist only when the first human pair message is present. Later messages remain person-to-person and skip the LLM reply path.
- Opening-assist messages carry `event_type=conversation_opening_assist`; an empty pair response is not appended by the website.
- Flutter preserves typed message metadata and renders shared date invitation/form/status cards directly in the common chat room.
- Private Ayue retains observation and date-coordination entry actions but no longer loads or renders the quiz or shared form.
- The shared-date editor uses the Calendar visual language, spacing, date picker, and start/end dropdowns.

**Canonical backend files/symbols modified:**

- `ayue_for_demo/social/routers/public_chat.py`: `direct_chat`
- `ayue_for_demo/social/frontend.html`: pair response rendering guard
- `docs/ayue-v3-ui-contract.md`
- Flutter runtime changes are confined to `DatingApp/lib/`.

**GitNexus impact and risk:** `direct_chat` is LOW risk with one direct upstream caller (`worker`) across the pair/public stream route. Flutter `ChatMessage` is LOW risk with three direct dependants and nine total import/copy dependants. The shared-chat card renderer and Private Ayue quick-action/polling changes are LOW risk with one direct page-build or init caller each.

**Rollback:** Restore the unconditional pair reply block, remove the typed Flutter shared-date card renderer, and restore the prior Private Ayue controls. No stored message, match, calendar, or Appwrite data is migrated or deleted by this change.

## Change 016: Complete Flutter shared-date and calendar actions

**Reason:** Flutter's shared-date labels and terminal card actions did not match the canonical website. The Calendar was only reachable from Public Ayue, and shared date events did not expose the backend's existing reschedule and reschedule-withdrawal routes.

**Before:**

- The shared editor labelled the activity as `活動`, with shortened date/time labels.
- A completed shared-date card had no direct Calendar action.
- The main Chat page had no Calendar entry point.
- Shared date events displayed no reschedule action, and pending proposals could not be withdrawn from Flutter.

**After:**

- The shared editor uses the website labels `想做什麼`, `日期 *`, `開始時間 *`, and `結束時間 *`.
- Completed shared-date cards expose `查看行事曆`, and the main Chat page exposes the same Calendar destination.
- Confirmed shared dates expose `提出改期`; pending reconfirmation events expose `撤回改期` and show the proposed schedule.
- Flutter calls the existing calendar reschedule endpoints with the server event ID, canonical date form, and current revision where required. A 409 refreshes the agenda before showing the conflict.

**Canonical backend files/symbols modified:** None. Runtime changes are confined to `DatingApp/`; the existing Calendar API is reused unchanged.

**GitNexus impact and risk:** `AyueCalendarEvent` is MEDIUM risk with seven direct and thirteen total upstream dependants; its new fields have backward-compatible defaults. Calendar card/editor, Chat list/header, and shared-room renderer changes are LOW risk, with at most three direct callers and one affected page-build flow.

**Rollback:** Remove the Flutter Calendar navigation callbacks and reschedule client methods, then restore the prior shared-date labels. No backend event, match, or Appwrite schema migration is introduced by this change.

## Change 017: Add owner-controlled coarse location to Flutter profile editing

**Reason:** The canonical website lets the owner maintain a city and district for nearby-information queries, while Flutter's personal profile editor only stored the separate public dating-region field in Appwrite.

**Before:**

- Flutter could not read or update the backend-owned `profile_location` projection.
- The personal profile editor exposed only the Appwrite `region` selector, which does not supply Ayue's nearby search context.

**After:**

- Flutter reads `user_location` from the canonical bootstrap response and updates it through `PATCH /api/profile/location`.
- The personal profile editor contains a clearly separated `所在地（僅用於附近資訊查詢）` area with city/county and district fields, each bounded to the backend's 20-character limit.
- A failed location load does not replace the stored location with empty values; the editor offers an explicit retry while allowing unrelated Appwrite profile fields to remain editable.

**Canonical backend files/symbols modified:** None. Runtime changes are confined to `DatingApp/` and reuse the existing profile-location contract unchanged.

**GitNexus impact and risk:** `AyueV3ApiService` is MEDIUM risk with fourteen direct and twenty total upstream dependants; only additive methods and a new data type were introduced. The profile editor state and save/render lifecycle are LOW risk, with one direct and fourteen total class dependants.

**Rollback:** Remove the Flutter location projection, API methods, and profile editor fields. No backend profile or Appwrite schema migration is introduced by this change.

## Future change entry format

```text
## Change NNN: Short title

Reason:

Before:

After:

Canonical backend files/symbols modified:

GitNexus impact and risk (required before canonical symbol changes):

Rollback:
```
