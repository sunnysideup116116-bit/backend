---
name: recent-context
description: Extract a user's current short-term situation (recent event, explicit feeling, activity or plan) without storing durable preferences.
version: 5
---

# Recent Context

Use the owner's latest saved message as the only source of new facts and evidence. You may also receive one bounded typed active episode containing only previously validated owner fields; it is context for deciding continuity, never evidence for a new field. Return a patch for a short-term real-world situation: a recent event, a feeling the owner explicitly states about it, and/or an activity or plan. Never include durable likes/dislikes, people, account IDs, calendar data, invitation state, matching state, or a request to find a person. The server-owned `metadata.message_use` marker must be `ordinary`; calendar-operation, assessment, no-memory, and unmarked legacy messages are excluded before this skill is invoked.

Set `episode_relation` semantically:

- `continue`: the latest message adds to, corrects, or answers a follow-up about the supplied active episode.
- `new`: the latest message starts a different real-world situation or plan.
- `unrelated`: the message does not update that episode.

Do not require a time word for `continue`. Short answers such as a destination, activity, timing, or companion answer can continue the active episode when their meaning is clear. If the relationship is ambiguous, use `unrelated`; never merge by keyword alone.

Classify the message as real_world_update, match_operation, durable_preference, or other. For real_world_update, return only the explicitly evidenced fields among activity, destination, timing, companion_intent, temporal_status, event, and feeling. Each field must include operation (set or clear), value, confidence, subject=`owner`, and an exact evidence_span from the owner message. Do not write a free-text summary.

- `event`: a short Traditional-Chinese phrase for a recent real-world event the owner reports (for example, a meeting that ended badly). Use it when the owner reports what happened, even if there is no activity to do.
- `feeling`: only a feeling the owner explicitly states in this message (for example, annoyance or exhaustion). Prefer the owner's own wording, shortened to a brief emotion phrase; never infer mood, personality, mental state, or a permanent trait. A feeling is situational, not a durable preference, and must not be proposed as a memory item.
- `activity`, `destination`, `timing`, `companion_intent`, `temporal_status`: keep their existing meaning for activities and plans.

`temporal_status` is typed state, not a free-text summary:

- `past`: the owner says the activity already happened or recalls a completed recent activity.
- `current`: the owner says the activity is happening now or is an ongoing recent routine.
- `planned`: the owner explicitly wants, intends, plans, or is preparing to do it.

If the message does not establish one of these states, omit `temporal_status`. Never turn a completed activity into a future plan.
