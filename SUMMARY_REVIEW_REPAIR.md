# Bounded-summary retention and review repair

## Defects addressed

The generation prompt did not list the actual per-field limits. The shared summary model's legacy normalizer truncates lists and strings; using that normalizer directly on new provider output could discard a seventh known fact before the evaluator saw the candidate. The same schema retry did not carry the concrete overflow reason, and a quality review caused the worker to repeat generation without feedback about the missing categories.

The lossy-normalization defect is reproduced deterministically and with entirely fictional model inputs. The exact rejected private candidate from the user's production run was not replayed: an attempted private-data model probe was denied by safety review and did not execute. These findings do not establish that silent truncation is the sole cause of every existing review result.

## Changes

- New provider output must contain the six fields, string lists within the existing field limits, and nonempty items no longer than 120 normalized characters. Unsafe or lossy-normalized output is rejected. Legacy persisted-record reads remain compatible.
- The prompt includes numeric limits. One schema retry receives a safe field/count error such as `known_continuity:actual=7,limit=6`; it must merge related facts, not drop the newest answer.
- A review gets at most one feedback-driven semantic repair, using the unchanged original prior summary and source messages. The repaired candidate is evaluated again. The original 0.8 confidence threshold, retention booleans and safety checks are unchanged.
- Explicitly answered questions may be removed from unresolved questions when their answer is preserved. Evaluation judges semantic coverage, not exact array positions or counts.
- Maximum per worker invocation: four generation calls and four evaluation calls, counting schema retries and semantic repair. Existing outer worker retries remain bounded. Persistent review still holds the watermark and requires operator attention; this is not an infinite-retry policy.
- Observability records whether semantic repair ran, initial issue codes, repair outcome and actual attempt counts. It stores no rejected candidate or raw transcript.
- Approval correctly counts an evaluation object with status `unavailable` as unavailable; the old rate limit is not weakened.
- `scripts/recover_summary_review.py` is owner-scoped and dry-run first. It only selects failed review jobs, checks the source batch, and requires matching approval before application. It never changes review to pass or resets another owner's work.

## Validation approach

The synthetic benchmark runs the real writer/evaluator pipeline with an in-memory database. It retains the existing 50 cases and adds 20 dense cases (six existing known facts, five open questions, answers/corrections/topic changes/excluded content), for 70 cases across 14 families. Exact expected anchors and excluded markers are checked independently of the evaluator. An explicitly answered character question must not remain unresolved.

The initial attempt had 66/70 passes and four generation-schema failures. It is retained as failed evidence, not a qualifying report. A second partial run was stopped after a callback-compatibility correction changed the fingerprint; it is not counted as completed. The final report must be validated against the final fingerprint before any approval is applied. Synthetic evaluations are never inserted into production metrics.

Unit coverage includes overflow and unsafe output rejection, exact retry feedback, semantic-repair feedback, re-evaluation, persistent-review rejection, bounded worst-case call counts, unchanged previous summary/watermark on failure, and unavailable-rate enforcement.

Final isolated regression: **1,376 passed + 63 subtests**, 34.76 seconds. Final fixed-corpus live synthetic benchmark: **70/70 passed**, no unavailable/review/critical failures, same corpus hash as the initial failed report. Final algorithm fingerprint is `728ed50d7f2e74f87a219d520b10655191341c0480afcdb4c38cc5e0074f0f2d`. Initial failure and final success reports are both retained in `validation/`. Startup shell syntax and whitespace checks passed. Owner-specific recovery dry-run found one eligible failed review job and performed no writes or model calls.

## Deployment and private-data boundary

This work is isolated from concurrent Pi/voice edits. No production files, approval records, jobs, or chats have been changed by this repair checkpoint. Production replay/recovery is pending explicit owner consent to send the necessary old summary and source messages to the existing Ollama / DeepSeek cloud model. Do not bypass that requirement via a worker, another script, or a different provider.

After consent, integration and restart through the sole `Server/start_all.sh` entry, validate and approve the final report, then dry-run and apply owner-specific recovery:

```bash
.local-venv/social/bin/python scripts/manage_summary_rollout.py approve-benchmark --report validation/summary-review-repair-final.json
.local-venv/social/bin/python scripts/manage_summary_rollout.py approve-benchmark --report validation/summary-review-repair-final.json --apply
.local-venv/social/bin/python scripts/recover_summary_review.py --owner <confirmed-owner>
.local-venv/social/bin/python scripts/recover_summary_review.py --owner <confirmed-owner> --apply
```

Verify multiple subsequent compaction cycles and live Pi recall outside the recent-message window. A passing synthetic suite is not proof that the user's production room has recovered or that all-account production quality has passed.
