---
name: memory
description: Extract an owner's durable, non-sensitive social preferences.
version: 4
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

`label_zh_tw` is a full bounded semantic source, not a display summary. Preserve
qualifiers, exclusions, role, accessibility, and broad/narrow distinctions. The
server rejects text above its authoritative semantic bound instead of storing a
prefix. Evidence spans must be complete owner substrings within 160 characters;
do not truncate a longer proposed span. The server creates `semantic_text`, a
separate shortened `display_label`, the versioned canonical key, and integrity
metadata. Never generate identity from a shortened display label or a prior key.
