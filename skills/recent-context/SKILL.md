---
name: recent-context
description: Extract a user's current short-term activity without storing durable preferences.
version: 3
---

# Recent Context

Use only the owner's latest message. Return a patch for a short-term real-world activity. Never include durable likes/dislikes, people, account IDs, calendar data, invitation state, matching state, or a request to find a person.

Classify the message as real_world_update, match_operation, durable_preference, or other. For real_world_update, return only the explicitly evidenced fields among activity, destination, timing, companion_intent, and temporal_status. Each field must include operation (set or clear), value, confidence, subject=`owner`, and an exact evidence_span from the owner message. Do not write a free-text summary.

`temporal_status` is typed state, not a free-text summary:

- `past`: the owner says the activity already happened or recalls a completed recent activity.
- `current`: the owner says the activity is happening now or is an ongoing recent routine.
- `planned`: the owner explicitly wants, intends, plans, or is preparing to do it.

If the message does not establish one of these states, omit `temporal_status`. Never turn a completed activity into a future plan.
