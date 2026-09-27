# Action-reference hardening — Phase 1 validation

Local validation: PASS under the differential gate (not an all-green-suite claim).
Exact clean-main base: `12b17c5746c17b8a0a089775e7219a95745aa93b`.
This branch includes the original full-source supplement `8b881cb` plus this hardening.
Production, DatingApp `ed6168734b09be709ec5a93ee6149e0f6cb5326d`, all activation flags and kill were untouched.
No merge, deployment, APK, P1-B, matching/semantic/embedding policy changes.

## Contract

- `key` is now a 48-character MAC-only reference (HMAC-SHA256, existing signing configuration with a distinct domain); it exposes no canonical key/Graph ID payload.
- Owner, current owner revision, exact relation identity, polarity and association/Concept state are bound. Validation is inside the existing owner write fence and transaction.
- Raw, forged, stale, disabled, retired, other-owner and polarity-changed references are rejected as public HTTP409 `stale_source`; no Mongo job/cache write occurs on rejection.
- Every successful correct/disable consumes the reference; same-identity correct advances revision without rewriting a shared Concept. Existing restore cannot resurrect a prior reference.
- Data-changing success commits a minimal bounded Graph projection marker with the mutation. Mongo projection starts only after success. The existing projection worker can recover a lost acknowledgement through service-signed internal scan/ack; it never replays the mutation or enables an embedding worker.
- The marker is one optional User property, not a new association schema/index/migration. Restore retains its existing recovery contract.
- Old raw-key edit/delete callers fail closed and must fetch full source. The approved client already echoes the source key; no client branch modification was made.

## Local test results

| Suite | Exact base | Head | Branch-only failures |
| --- | --- | --- | --- |
| Contracts |733 passed +232 subtests |735 passed +232 subtests |0 |
| Matchmaker |307 passed +17 subtests |329 passed +17 subtests |0 |
| Full Social |1873 passed /36 failed +63 subtests |1881 passed /34 failed +63 subtests |0 |
| Risk |231 passed /5 failed |231 passed /5 failed |0 |
| Focused Social lifecycle/action/API |covered by full base |128 passed |0 |
| Original eight reproducer scenarios, server-issued refs |previous5 PASS /3 FAIL |8 PASS |0 |

The original reproducer intents are retained: current PREFERS/AVOIDS correct,
other-owner correct/disable, superseded/disabled correct, and stale-polarity correct/disable.
The new22-case real-handler/real-MAC transactional adapter suite also covers active disable,
revision/properties/recreated association, invalid active state, tamper/raw key, shared owner,
restore ABA, same-text consumption, concurrent one-winner, rollback and64-item references.
Legacy identity/length unit tests isolate reference lookup so they continue testing their
original identity semantics; reference correctness is independently exercised without that seam.

## Real disposable Graph/Mongo integration

Nine scenario groups PASS on owned loopback-only Neo4j2026.08.1 and Mongo8.2.5 replica set.
Real handlers, HTTP routes, MACs, owner fences, transactions and Mongo projections were exercised.
Only authentication and provider startup are synthetic; no provider/model call or production I/O.

1. Current correct/disable succeeds, then replay rejects.
2. Disabled and restored same identity reject all old refs.
3. Polarity-changed correct/disable reject with exact Graph/Mongo before/after equality.
4. Revision change invalidates the old ref.
5. Other owner/tamper/raw keys reject; another owner's shared Concept association stays unchanged.
6. Concurrent stale/current/current requests produce200/409/409, one legal mutation.
7. Injected exception after Graph write rolls back mutation/revision/marker, no Mongo projection.
8. Lost successful Graph acknowledgement is recovered via committed marker, never action replay.
9. Full64-item source and edits/deletion beyond the12-item display boundary (positions13,30,63), with fresh refs after each success.

Every stale integration assertion compares owner Graph properties/edges/Concept properties
and all owner-scoped Mongo collections before/after. The fixture containers were stopped;
synthetic volumes and local receipts are retained. This is not production release validation.

## Environment and differential method

Both revisions use the same Python3.12.3 interpreter/venv per suite and the existing
`run_offline_tests.py` network/DNS/dotenv-denying harness. No requirements/workflow changes.
Social/Contracts: pytest9.1.1, httpx0.28.1, OpenAI1.30.1, Pydantic2.13.5, PyMongo4.7.2, Neo4j5.21.0.
Matchmaker: pytest9.1.1, httpx0.28.1, OpenAI2.35.1, Pydantic2.13.4, PyMongo4.18.2, Neo4j6.2.0.
Risk: pytest9.1.1, httpx0.28.1, OpenAI2.32.0, Pydantic2.12.5, PyMongo4.18.2; google-genai is absent in this existing venv on both revisions.
Common failing test names AND failure messages match exactly. No local failures were hidden,
skipped additionally or fixed out of scope. The existing harness exclusions are unchanged.
Canonical `start_all.sh` syntax and lifecycle Contracts passed; no production startup was performed.

Two Social cases failed only on the base run and passed on head; this is observed run variation,
not a claimed fix or evidence of a new branch failure:

- `tests.test_pi_feedback_regressions::test_safe_provider_sentence_is_published_before_completion`: AssertionError: assert 'Pi 這次未能完成有效回...的變更。請直接重試原需求。' == '你好，我先看懂你的問題。'      - 你好，我先看懂你的問題。   + Pi 這次未能完成有效回覆，也沒有建立或執行新的變更。請直接重試原需求。
- `tests.test_pi_feedback_regressions::test_unsafe_provider_stream_is_held_before_public_tokens`: AssertionError: assert 'Pi 這次未能完成有效回...的變更。請直接重試原需求。' == '你好，我是阿月。'      - 你好，我是阿月。   + Pi 這次未能完成有效回覆，也沒有建立或執行新的變更。請直接重試原需求。

## Remaining baseline failures (head)

### social

| Exact test name | Failure message |
| --- | --- |
| `tests.test_ayue_agent_stream.AyueAgentStreamTests::test_json_direct_chat_routes_authenticated_owner_to_public_pi` | fastapi.exceptions.HTTPException: 401: appwrite_jwt_required |
| `tests.test_ayue_agent_stream.AyueAgentStreamTests::test_public_stream_does_not_publish_tokens_without_opt_in` | fastapi.exceptions.HTTPException: 401: appwrite_jwt_required |
| `tests.test_ayue_agent_stream.AyueAgentStreamTests::test_public_stream_emits_only_safe_progress_and_compatible_final_response` | fastapi.exceptions.HTTPException: 401: appwrite_jwt_required |
| `tests.test_ayue_agent_stream.AyueAgentStreamTests::test_public_stream_publishes_bounded_token_fragments` | fastapi.exceptions.HTTPException: 401: appwrite_jwt_required |
| `tests.test_ayue_agent_stream.AyueAgentStreamTests::test_run_owned_background_tasks_finish_after_stream_response` | fastapi.exceptions.HTTPException: 401: appwrite_jwt_required |
| `tests.test_ayue_agent_stream.AyueAgentStreamTests::test_stream_hides_raw_exception_details` | fastapi.exceptions.HTTPException: 401: appwrite_jwt_required |
| `tests.test_ayue_agent_stream.AyueAgentStreamTests::test_unknown_progress_event_is_not_published` | fastapi.exceptions.HTTPException: 401: appwrite_jwt_required |
| `tests.test_chat_choice_routes.ChatChoiceRouteTests::test_private_json_choice_skips_owner_persistence` | fastapi.exceptions.HTTPException: 401: appwrite_jwt_required |
| `tests.test_chat_choice_routes.ChatChoiceRouteTests::test_public_json_choice_does_not_save_an_owner_message` | fastapi.exceptions.HTTPException: 401: appwrite_jwt_required |
| `tests.test_chat_router_characterization.ChatRouterCharacterizationTests::test_private_json_v2_saves_owner_once_then_delegates_without_runtime_fallthrough` | fastapi.exceptions.HTTPException: 401: appwrite_jwt_required |
| `tests.test_chat_router_characterization.ChatRouterCharacterizationTests::test_private_stream_keeps_the_ndjson_final_contract_after_extraction` | fastapi.exceptions.HTTPException: 401: appwrite_jwt_required |
| `tests.test_chat_router_characterization.ChatRouterCharacterizationTests::test_private_stream_requires_an_accepted_match` | AssertionError: 401 != 403 |
| `tests.test_google_calendar_agent_access::test_public_agent_route_passes_verified_owner_capability_to_the_turn` | fastapi.exceptions.HTTPException: 401: appwrite_jwt_required |
| `tests.test_pi_feedback_regressions::test_final_language_normalization_keeps_person_wording` | TypeError: _bind_interactions() missing 1 required keyword-only argument: 'batches' |
| `tests.test_pi_feedback_regressions::test_pi_connect_event_does_not_wait_for_the_first_model_response` | fastapi.exceptions.HTTPException: 401: appwrite_jwt_required |
| `tests.test_pi_feedback_regressions::test_pi_activity_search_schema_and_preparation_preserve_surfing` | TypeError: PiToolRuntime.__init__() missing 1 required keyword-only argument: 'operation_batch_collection' |
| `tests.test_pi_feedback_regressions::test_pi_event_interest_request_prepares_real_activity_search` | TypeError: PiToolRuntime.__init__() missing 1 required keyword-only argument: 'operation_batch_collection' |
| `tests.test_pi_feedback_regressions::test_pi_preference_search_is_canonical_and_confirmation_bound[\u5e6b\u6211\u627e\u559c\u6b61 K-pop \u7684\u4eba-K-pop]` | TypeError: PiToolRuntime.__init__() missing 1 required keyword-only argument: 'operation_batch_collection' |
| `tests.test_pi_feedback_regressions::test_pi_preference_search_is_canonical_and_confirmation_bound[\u5e6b\u6211\u627e\u559c\u6b61 Kpop \u7684\u4eba-K-pop]` | TypeError: PiToolRuntime.__init__() missing 1 required keyword-only argument: 'operation_batch_collection' |
| `tests.test_pi_feedback_regressions::test_active_semantic_fallback_is_disclosed_without_claiming_common_preference` | TypeError: PiToolRuntime.__init__() missing 1 required keyword-only argument: 'operation_batch_collection' |
| `tests.test_pi_feedback_regressions::test_explicit_preference_wording_overrides_a_misclassified_activity_kind` | TypeError: PiToolRuntime.__init__() missing 1 required keyword-only argument: 'operation_batch_collection' |
| `tests.test_pi_feedback_regressions::test_negative_preference_request_never_becomes_positive_graph_search[preference]` | TypeError: PiToolRuntime.__init__() missing 1 required keyword-only argument: 'operation_batch_collection' |
| `tests.test_pi_feedback_regressions::test_negative_preference_request_never_becomes_positive_graph_search[activity]` | TypeError: PiToolRuntime.__init__() missing 1 required keyword-only argument: 'operation_batch_collection' |
| `tests.test_pi_feedback_regressions::test_match_followup_uses_visible_context_and_explicit_recent_mode[\u5c0d-history0-arguments0-\u885d\u6d6a]` | TypeError: PiToolRuntime.__init__() missing 1 required keyword-only argument: 'operation_batch_collection' |
| `tests.test_pi_feedback_regressions::test_match_followup_uses_visible_context_and_explicit_recent_mode[\u6539\u7528\u8fd1\u671f\u60c5\u5883\u5c31\u597d-history1-arguments1-None]` | TypeError: PiToolRuntime.__init__() missing 1 required keyword-only argument: 'operation_batch_collection' |
| `tests.test_pi_feedback_regressions::test_match_unseen_topic_or_forged_authority_never_prepares` | TypeError: PiToolRuntime.__init__() missing 1 required keyword-only argument: 'operation_batch_collection' |
| `tests.test_pi_feedback_regressions::test_confirmed_surfing_search_reaches_executor_once` | TypeError: PiToolRuntime.__init__() missing 1 required keyword-only argument: 'operation_batch_collection' |
| `tests.test_pi_feedback_regressions::test_same_turn_place_result_reuses_private_coordinates_without_prompt_leak` | TypeError: PiToolRuntime.__init__() missing 1 required keyword-only argument: 'operation_batch_collection' |
| `tests.test_pi_feedback_regressions::test_invalid_private_place_coordinates_are_ignored` | TypeError: PiToolRuntime.__init__() missing 1 required keyword-only argument: 'operation_batch_collection' |
| `tests.test_pi_feedback_regressions::test_ambiguous_same_name_place_candidates_never_reuse_coordinates` | TypeError: PiToolRuntime.__init__() missing 1 required keyword-only argument: 'operation_batch_collection' |
| `tests.test_pi_feedback_regressions::test_public_pi_completes_route_and_contact_evidence_in_one_turn` | AssertionError: assert [] == ['places.sear...act_evidence']      Right contains 3 more items, first extra item: 'places.search_nearby'   Use -v to get more diff |
| `tests.test_stream_delivery_guarantees.StreamDeliveryGuaranteesTests::test_private_stream_delivers_tokens_and_no_buffer_headers` | fastapi.exceptions.HTTPException: 401: appwrite_jwt_required |
| `tests.test_stream_delivery_guarantees.StreamDeliveryGuaranteesTests::test_public_first_token_is_observable_before_final` | fastapi.exceptions.HTTPException: 401: appwrite_jwt_required |
| `tests.test_stream_delivery_guarantees.StreamDeliveryGuaranteesTests::test_public_queue_keeps_every_token_in_order` | fastapi.exceptions.HTTPException: 401: appwrite_jwt_required |

### risk

| Exact test name | Failure message |
| --- | --- |
| `tests.test_llm_adapters::test_default_provider_returns_gemini` | ImportError: cannot import name 'genai' from 'google' (unknown location) |
| `tests.test_llm_adapters::test_summary_provider_can_override_to_gemini` | ImportError: cannot import name 'genai' from 'google' (unknown location) |
| `tests.test_llm_adapters::test_gemini_nlp_budget_switches_to_fallback_without_retrying_primary` | ImportError: cannot import name 'genai' from 'google' (unknown location) |
| `tests.test_llm_adapters::test_gemini_adapter_multi_key_round_robin` | ImportError: cannot import name 'genai' from 'google' (unknown location) |
| `tests.test_llm_adapters::test_gemini_adapter_key_failover_on_429` | ImportError: cannot import name 'genai' from 'google' (unknown location) |

## Handoff boundary

Phase1 local differential PASS permits push/independent PR only. Remote Actions are a
separate result and must not be inferred from this report. Do not merge/deploy or start
Phase2 without the user's next approval. Production data/flags/workers/kill remain untouched.
