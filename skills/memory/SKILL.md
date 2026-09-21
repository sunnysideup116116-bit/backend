---
name: memory
description: Extract an owner's durable, non-sensitive social preferences.
version: 3
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

Each durable memory candidate represents exactly one atomic concept. When the
owner explicitly lists independent preferences, return one candidate per item
with item-level evidence spans. For example, `我喜歡 K-pop、J-pop、西洋音樂`
becomes three candidates. Do not split descriptive noun phrases: `適合讀書的安靜咖啡廳`
is one concept. Keys are proposals only; the server canonicalizes identity.
Respect the per-message candidate limit supplied in the extraction prompt; the
server also enforces its configurable limit and a hard resource ceiling.
