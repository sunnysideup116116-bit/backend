# Event V2 adapter — local validation

Base: `8ccfe09f688b92c105e961994e4d1da87ed480b7`.
Branch: `codex/event-v2-readiness-fix`.
Contract/data flow/migration impact: [Event V2 contract](EVENT_V2_ADAPTER_CONTRACT.md).
This is implementation/test readiness, not deployment or production acceptance.

```ini
EVENT_V2_ADAPTER_CONTRACT_READY = YES
EVENT_EXACT_DEFINED_AS_USER_TO_ACTIVITY = YES
CURRENTLY_WANTS_PRESERVED = YES
EVENT_AVOIDS_PRESERVED = YES
EVENT_TAGS_VIBES_SEPARATE_FROM_PREFERENCE_V2 = YES
EVENT_V2_COMPAT_FIX_READY = YES
LEGACY_EVENT_EMBEDDING_DEPENDENCY = 0
EVENT_GLOBAL_READINESS_ABORT_FIXED = YES
NORMAL_MATCHING_REGRESSION = 0
PRODUCTION_MUTATION = NONE
```

## Root cause → change

| Before | After |
| --- | --- |
| Weekly gate polls legacy missing-embedding endpoint until all related Concepts are ready | One Graph/V2 infrastructure probe; exact scan continues with pending vectors or unavailable semantic infrastructure |
| Event projection compares User and Event `Concept.embedding` | Read-time Event adapter: verified source exact, then compatible V2 positive ANN/validator; no legacy vector projection |
| Candidate discovery trusts cached EVENT_RELEVANCE/EVENT_AVOIDANCE links | Fresh owner→activity evidence, independent for both people; opaque source-bound final proof |
| Shared V2 preference requirement could wrongly narrow Event matching | Different preferences can independently match tags of one activity; exact gate is per owner/activity |
| Negative vectors may be missing | Exact negative blocks; unresolved negative is typed unavailable, never absence of conflict |

Event tags/vibes and current intent have independent query namespaces. Temporary
comparison keys reuse canonicalization without creating durable identities. Query
vectors use the existing frozen Social Gemini encoder over a signed private API,
768 dimensions/L2/runtime/source hash/encoder provenance. They are process-memory
artifacts only. No Event/preference migration, legacy backfill, index creation or
historical-vector overwrite is performed.

## Regression evidence

Same host, Python **3.12.3**, same dependency environment for each paired suite.
`scripts/run_offline_tests.py` disables dotenv, network/DNS and production secrets.
Sandboxed unchanged TestClient/async tests stalled; the final paired runs used the
same harness outside that filesystem sandbox, with network still blocked. Initial
wrong-venv and interrupted sandbox runs are not acceptance evidence.

| Suite | Exact base | Head | Branch-only failures |
| --- | --- | --- | --- |
| Matchmaker full | 365 PASS; 17 subtests PASS | 411 PASS; 17 subtests PASS | 0 |
| Social full | 1907 PASS / 36 FAIL; 63 subtests PASS | 1927 PASS / same 36 FAIL; 63 subtests PASS | 0 |
| Contracts | 735 PASS; 232 subtests PASS | 735 PASS; 232 subtests PASS | 0 |

Commands: `python scripts/run_offline_tests.py matchmaker`, `social`, `contracts`.
New Event coverage adds 46 Matchmaker and 20 Social cases (parameterized cases
included). Focused Event coverage exercises existing orchestration/delivery tests
as well as the adapter, signed transports, source expiry, stale proof and monitor.

Tests prove:

- Compatible V2 with legacy embedding null is ready.
- Different owner preferences can form exact relevance to one activity.
- Qualified exact bridge avoids ANN/validator, even with vectors pending and
  semantic/index unavailable.
- Exact on activity A does not suppress related relevance to activity B.
- Exact final proof survives derived-vector readiness changes; semantic proof
  binds its current vector/source/provenance and threshold.
- Incompatible source/runtime/provenance and conflicting identity keys fail closed.
- V2 index only; no legacy vector reads in active Event adapter paths.
- Pending candidate does not starve another exact candidate or abort weekly scan.
- Synthetic durable cycle processes both pending/exact owners: 2 processed,
  1 created, 1 failed after bounded retries, overall partial rather than false PASS.
- Unexpired CURRENTLY_WANTS remains positive; expiry invalidates request cache and
  final proof, including expiry during selection/recheck.
- AVOIDS never positive; exact negative blocks and unresolved negative is unavailable.
- Trusted REJECT + partial ERROR is normal no-match; only trusted ACCEPT is used.
- Zero-trusted/systemic validator failure remains typed unavailable; three consecutive
  typed failures in fifteen minutes use the existing shared kill policy.
- Missing signature/source hash, wrong owner, forged/stale receipt, new negative,
  source/polarity/revision/expiry changes reject before draft insertion.
- Final rejection releases quota, inserts no draft or notification, and weekly
  ownership is checked again after the final HTTP proof.
- Event query artifacts never create durable preference identity; encoder closes
  on failure and reuses compatible cached query vectors without re-encoding.
- Word rendering says activity relevance and possible interest; it does not claim
  shared/identical preference or that the counterparty agreed to attend.

Python compile, `bash -n start_all.sh` and diff whitespace checks PASS.
Ordinary matching / Identity V2 / validator / embedding source / startup script
are byte-identical to base. AST comparison of shared entry files changes only
`proactive_event_match`, `_refresh_semantic_event_links`, `find_event_matches`;
additional startup imports/routes initialize Event infrastructure.

## Social baseline differential

All **36 exact test identities and failure messages are equal** between base and
head. Canonical JSON message-map SHA256:
`2273c5916bb71eb88bdddc86e9b5a4852863e9a65f5d14c8cebb1e58d2c0bee3`.
No failure was removed or added. This is baseline confirmation, **not full Social
PASS or remote CI PASS**. Remote CI was not run in this phase.

16 failures with `fastapi.exceptions.HTTPException: 401: appwrite_jwt_required`:

- `tests.test_ayue_agent_stream.AyueAgentStreamTests::test_json_direct_chat_routes_authenticated_owner_to_public_pi`
- `tests.test_ayue_agent_stream.AyueAgentStreamTests::test_public_stream_does_not_publish_tokens_without_opt_in`
- `tests.test_ayue_agent_stream.AyueAgentStreamTests::test_public_stream_emits_only_safe_progress_and_compatible_final_response`
- `tests.test_ayue_agent_stream.AyueAgentStreamTests::test_public_stream_publishes_bounded_token_fragments`
- `tests.test_ayue_agent_stream.AyueAgentStreamTests::test_run_owned_background_tasks_finish_after_stream_response`
- `tests.test_ayue_agent_stream.AyueAgentStreamTests::test_stream_hides_raw_exception_details`
- `tests.test_ayue_agent_stream.AyueAgentStreamTests::test_unknown_progress_event_is_not_published`
- `tests.test_chat_choice_routes.ChatChoiceRouteTests::test_private_json_choice_skips_owner_persistence`
- `tests.test_chat_choice_routes.ChatChoiceRouteTests::test_public_json_choice_does_not_save_an_owner_message`
- `tests.test_chat_router_characterization.ChatRouterCharacterizationTests::test_private_json_v2_saves_owner_once_then_delegates_without_runtime_fallthrough`
- `tests.test_chat_router_characterization.ChatRouterCharacterizationTests::test_private_stream_keeps_the_ndjson_final_contract_after_extraction`
- `tests.test_google_calendar_agent_access::test_public_agent_route_passes_verified_owner_capability_to_the_turn`
- `tests.test_pi_feedback_regressions::test_pi_connect_event_does_not_wait_for_the_first_model_response`
- `tests.test_stream_delivery_guarantees.StreamDeliveryGuaranteesTests::test_private_stream_delivers_tokens_and_no_buffer_headers`
- `tests.test_stream_delivery_guarantees.StreamDeliveryGuaranteesTests::test_public_first_token_is_observable_before_final`
- `tests.test_stream_delivery_guarantees.StreamDeliveryGuaranteesTests::test_public_queue_keeps_every_token_in_order`

`tests.test_chat_router_characterization.ChatRouterCharacterizationTests::test_private_stream_requires_an_accepted_match`
fails with `AssertionError: 401 != 403`.

15 failures with `TypeError: PiToolRuntime.__init__() missing 1 required keyword-only argument: 'operation_batch_collection'`
(all in `tests.test_pi_feedback_regressions`):

- `test_active_semantic_fallback_is_disclosed_without_claiming_common_preference`
- `test_ambiguous_same_name_place_candidates_never_reuse_coordinates`
- `test_confirmed_surfing_search_reaches_executor_once`
- `test_explicit_preference_wording_overrides_a_misclassified_activity_kind`
- `test_invalid_private_place_coordinates_are_ignored`
- `test_match_followup_uses_visible_context_and_explicit_recent_mode[\u5c0d-history0-arguments0-\u885d\u6d6a]`
- `test_match_followup_uses_visible_context_and_explicit_recent_mode[\u6539\u7528\u8fd1\u671f\u60c5\u5883\u5c31\u597d-history1-arguments1-None]`
- `test_match_unseen_topic_or_forged_authority_never_prepares`
- `test_negative_preference_request_never_becomes_positive_graph_search[activity]`
- `test_negative_preference_request_never_becomes_positive_graph_search[preference]`
- `test_pi_activity_search_schema_and_preparation_preserve_surfing`
- `test_pi_event_interest_request_prepares_real_activity_search`
- `test_pi_preference_search_is_canonical_and_confirmation_bound[\u5e6b\u6211\u627e\u559c\u6b61 K-pop \u7684\u4eba-K-pop]`
- `test_pi_preference_search_is_canonical_and_confirmation_bound[\u5e6b\u6211\u627e\u559c\u6b61 Kpop \u7684\u4eba-K-pop]`
- `test_same_turn_place_result_reuses_private_coordinates_without_prompt_leak`

Other exact baseline failures in `tests.test_pi_feedback_regressions`:

- `test_final_language_normalization_keeps_person_wording`: `TypeError: _bind_interactions() missing 1 required keyword-only argument: 'batches'`.
- `test_public_pi_completes_route_and_contact_evidence_in_one_turn`: empty tool observations versus three expected place/contact tools.
- `test_safe_provider_sentence_is_published_before_completion`: Pi failure reply instead of `你好，我先看懂你的問題。`.
- `test_unsafe_provider_stream_is_held_before_public_tokens`: Pi failure reply instead of `你好，我是阿月。`.

JUnit/log evidence lives at `/tmp/event-v2-differential.OO7JW9/` on this host.

## Changed files and scope

- Contract/report: `EVENT_V2_ADAPTER_CONTRACT.md`, `EVENT_V2_VALIDATION.md`.
- Readiness/retrieval/proof: `matchmaker_agent/event_v2_{contract,adapter,api,vectors}.py`.
- Existing Event entrypoints only: `matchmaker_agent/agent_api.py`, `matchmaker_agent/matchmaker.py`.
- Event orchestration: `social/services/event_cycle_service.py`, `event_weekly_service.py`, `event_relevance_service.py`.
- Event encoding/audit/final proof: `social/services/event_query_vectors.py`, `event_semantic_monitor.py`, `event_v2_proposal.py`.
- Domain insertion/wiring: `social/services/event_opportunity_service.py`, `social/main.py`.
- Tests: `matchmaker_agent/test_event_v2_adapter.py`, `test_event_pipeline.py`, `test_context_projection.py`;
  `social/tests/test_event_v2_contract.py`, `test_event_opportunity_service.py`, `test_event_relevance_service.py`.
- Narrow documentation: `AYUE_V3_ARCHITECTURE.md`, `docs/EVENT_DRIVEN_MATCHMAKER_GUIDE.md`, `docs/architecture/09-runtime-interfaces.md`.

GitNexus binds this exact worktree. Its staged review reports 97 changed symbols,
711 affected processes and CRITICAL risk because the shared Matchmaker class is
touched. All 97 reported symbols were returned; no partial/truncated flags were
reported. Dynamic HTTP/global wiring is supplemented by direct source/AST checks
and signed endpoint tests. This graph result is not a claim that ordinary matching
changed; it remains a required review risk, alongside the tested source boundaries.

## Remaining release boundary

- No deployment/restart, production job, flag change or data mutation performed.
- No bulk migration/backfill, DatingApp/APK/dependency/P1-B changes.
- Unresolved semantic-negative constraints deliberately reduce availability for
  the affected owner/activity. This is an explicit limitation, not a conflict-free
  classification. A later semantic-negative policy would require a separate gate.
- Query caches/receipts are ephemeral; receipt miss after restart safely rejects.
- Real provider/Graph integration and canonical live four-service health are
  NOT_RUN in this phase. Local tests do not establish production reliability.
- Merge/deployment/production Event execution await the user's next approval.
