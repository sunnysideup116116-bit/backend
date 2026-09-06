---
name: memory
description: Extract an owner's durable, non-sensitive social preferences.
version: 2
---

# Memory

Store only explicit, durable preferences belonging to the owner. Do not store
one-time plans, calendar items, invitation state, other people's attributes,
account IDs, or protected/sensitive attributes. The server-owned
`metadata.message_use` marker must be `ordinary`; calendar-operation,
assessment, no-memory, and unmarked legacy messages are excluded before this
skill is invoked. Return key, label_zh_tw,
stance, category, confidence, evidence_span, subject, and reason_code. The
subject must be `owner`; the evidence span must be an exact substring of the
owner's message. Use Traditional Chinese labels.
