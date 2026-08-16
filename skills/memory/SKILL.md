---
name: memory
description: Extract an owner's durable, non-sensitive social preferences.
version: 2
---

# Memory

Store only explicit, durable preferences belonging to the owner. Do not store
one-time plans, calendar items, invitation state, other people's attributes,
account IDs, or protected/sensitive attributes. Return key, label_zh_tw,
stance, category, confidence, evidence_span, subject, and reason_code. The
subject must be `owner`; the evidence span must be an exact substring of the
owner's message. Use Traditional Chinese labels.
