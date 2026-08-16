---
name: recent-context
description: Extract a user's current short-term activity without storing durable preferences.
version: 4
---

# Recent Context

Use the owner's latest saved message as the only source of new facts and evidence. You may also receive one bounded typed active episode containing only previously validated owner fields; it is context for deciding continuity, never evidence for a new field. Return a patch for a short-term real-world activity. Never include durable likes/dislikes, people, account IDs, calendar data, invitation state, matching state, or a request to find a person.

Set `episode_relation` semantically:

- `continue`: the latest message adds to, corrects, or answers a follow-up about the supplied active episode.
- `new`: the latest message starts a different real-world activity or plan.
- `unrelated`: the message does not update that episode.

Do not require a time word for `continue`. Short answers such as a destination, activity, timing, or companion answer can continue the active episode when their meaning is clear. If the relationship is ambiguous, use `unrelated`; never merge by keyword alone.

Classify the message as real_world_update, match_operation, durable_preference, or other. For real_world_update, return only the explicitly evidenced fields among activity, destination, timing, companion_intent, and temporal_status. Each field must include operation (set or clear), value, confidence, subject=`owner`, and an exact evidence_span from the owner message. Do not write a free-text summary.

`temporal_status` is typed state, not a free-text summary:

- `past`: the owner says the activity already happened or recalls a completed recent activity.
- `current`: the owner says the activity is happening now or is an ongoing recent routine.
- `planned`: the owner explicitly wants, intends, plans, or is preparing to do it.

If the message does not establish one of these states, omit `temporal_status`. Never turn a completed activity into a future plan.
