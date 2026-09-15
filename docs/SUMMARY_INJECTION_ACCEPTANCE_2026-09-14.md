# Conversation summary injection: canary acceptance (historical)

> **Superseded current status (2026-09-15):** The canary below was a historical Public V3/DAG acceptance. Summary v5 is now deployed on Public Pi with an approved controlled all-account rollout, mixed-ID support, bounded review repair, and 46/46 profile eligibility. See [global stability acceptance](../../artifacts/summary-global-stability-2026-09-15.md) and [current progress notes](PROGRESS_REPORT_CONTEXT_MEMORY_EVENT_2026-09-15.md). The historical measurements and claims below remain unchanged as evidence for that date only.

## Verified outcome

On September 14, 2026, conversation summary injection passed live acceptance for one explicitly enabled demo account on the deployed Public V3 DAG runtime. The test used real MongoDB persistence, real summary generation and evaluation, and the public streaming chat API.

This is a dated acceptance record, not a claim that all accounts or subsequent runtimes are validated. The deployed checkout included ongoing changes beyond commit `f081257`; that commit identifies the tracked base, not an exact reproducible snapshot of all deployed source. Pi was excluded from this acceptance.

## Method and evidence

1. Created a separate demo room containing 40 explicitly labelled synthetic user messages about a fictional story. These were a controlled fixture, not 40 real conversational turns.
2. Placed three distinctive facts in the oldest message: the story title **琥珀郵局**, protagonist **藍鷺**, and letter **紙月亮**. Later filler messages did not contain these answers.
3. Called the existing batch selector and compaction pipeline. A real model generated the summary and a real evaluator approved it. The existing writer persisted revision 1; no prewritten summary or fabricated pass result was inserted.
4. Verified that the three facts were absent from the current 12-message raw window and the demo profile, but present in the actual Planner and Synthesizer inputs.
5. Both real model paths recovered the facts. With injection disabled in a separate test context, the Synthesizer said the facts were unknown. Different-owner and different-room loader checks rejected access.
6. Ran the complete DAG Scheduler with streaming enabled: the summary reached the Planner, the answer recovered all three facts, and 18 stream fragments were observed.
7. Enabled the demo account in the deployment allowlist, restarted the complete stack through `start_all.sh ollama`, and checked all four fixed service ports plus the public health endpoint: HTTP 200.
8. Added 14 labelled synthetic filler messages in the same room so earlier successful answers were outside the recent window. Sent a fresh recall request through `https://service.misproject.us.ci/api/direct_chat/stream`.
9. The public API returned `agent_mode=v3`, with one Planner and one Synthesizer model call. The response correctly recovered the three facts and the unresolved question of how the envelope opens. The answer was persisted in the room; a subsequent read confirmed the story facts were still absent from the profile.

The API response included:

> 有，記錄裡有。故事暫名《琥珀郵局》，主角叫藍鷺，要找的那封信叫「紙月亮」。

The existing compaction regression suite also passed: **38 tests and 11 subtests**. Shell syntax validation for `start_all.sh` passed. No browser login or UI automation was performed; API acceptance and fixture checks are reported separately.

## Deployment semantics

`AYUE_CONVERSATION_COMPACTION_MODE=shadow` is the existing generation/evaluation mode. The current implementation accepts `off|shadow`; changing it to `on` disables that path rather than activating injection.

Summary consumption is separately controlled by `AYUE_CONVERSATION_CONTEXT_MODE=on` and `AYUE_CONVERSATION_CONTEXT_USER_ALLOWLIST`:

- An explicitly listed owner uses the existing canary path. Each summary must still pass ownership, room, policy and evaluation validation.
- `*` uses aggregate rollout readiness. It does not bypass the minimum sample count or quality gates.
- Both can coexist. This deployment retained `*` and added the verified demo owner. No global threshold was lowered.

The environment change is private deployment configuration and is not committed. Pulling this documentation does not activate an account. An operator must resolve the intended account to its canonical user ID, add that ID to the allowlist while preserving existing entries, and reload via `start_all.sh`.

Before acceptance, aggregate metadata showed 15 passes and one generation timeout; evaluation had not run for the timeout. The synthetic acceptance added one passing evaluation. That controlled case is not evidence of broad production quality or proof that global rollout readiness has passed.

## Presentation claim

> We have successfully enabled and verified conversation-summary injection for a designated test account through the live chat API. Validated summaries allow Ayue to recall information beyond the recent conversation window, while preserving user and room isolation.

Do not claim global deployment, perfect recall, or Pi support based on this record. Subsequent runtime migrations need their own acceptance. The 12-message/6,000-character budget describes the deployed code used in this test; older guides describe a different budget.
