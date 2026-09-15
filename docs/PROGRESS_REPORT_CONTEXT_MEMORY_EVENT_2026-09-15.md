# Progress report notes: Context, Memory, and Event-Driven Match

This is the current English speaker-note draft for the third project presentation.
Slides 7–10 intentionally remain the older agent-structure explanation for this
report, as requested. The final presentation should replace that downstream
architecture with the team's Pi description. These notes update only the Context,
Memory, and Event-Driven Match sections.

## Transition from Slide 10

> Besides improving how Ayue performs tasks, we also redesigned how it selects,
> stores, and recalls user information.

## Slide 11 — Context Engine

> Another major improvement was our Context Engine.
>
> The main idea is that context is not a database dump. For every turn, Ayue
> first checks the user's ownership, the room boundary, and the applicable
> consent rules. It then builds a bounded, privacy-safe context bundle containing
> only what the current response needs.
>
> The bundle can combine recent conversation, a validated summary of the same
> room, the user's recent situation, up to eight directional durable preferences,
> and safe match or calendar state. The HTTP adapter fetches a bounded 32-row
> window, while the final Pi context projection keeps at most 12 recent messages
> or 6,000 characters. Long conversations use a validated room summary to
> preserve continuity beyond that recent window.
>
> The Context Builder is read-only. It prepares a typed projection for the agent;
> it does not write memory or application state. A summary is used only when it
> passes the owner, room, policy, source, and evaluation checks.

### Slide 11 deployment wording

The internal compaction producer is still labelled `shadow` because it writes
through the safe shadow-run pipeline. This is not shadow-only consumption:
`CONTEXT_MODE=on`, the approved wildcard rollout, and per-summary validation now
allow eligible summaries to be injected into the public Pi context. If this is
mentioned in the presentation, call it **controlled all-account summary
injection**, not “shadow mode only.”

### Slide 11 accuracy note

The current slide labels the downstream boxes Planner, Sub-agents, and Synthesizer.
That is acceptable for this third-report snapshot because the teammate's Agent
Structure section is intentionally presented in its older form. In the final
report, keep the Context inputs and safety rules, but update the downstream label
to the deployed Pi runtime.

## Slide 12 — Memory Architecture

> On top of the Context Engine, we redesigned the memory architecture.
>
> Durable memory is not whatever the model happens to assume. It must come from
> a saved, user-owned message with validated evidence. The flow is: saved user
> message, typed extraction and evidence validation, then a canonical memory
> projection and a bounded context projection.
>
> Neo4j stores the owner-scoped preference and relationship graph used for
> reasoning. MongoDB stores the smaller read projection used for fast retrieval.
> Long-term preferences are separated from temporary intentions and from
> conversation continuity summaries. For example, “I prefer quiet places” may
> become a durable preference, while “I want to go hiking today” is temporary
> context rather than a permanent preference.
>
> Users can review Prefer and Avoid memories and can disable, restore, or correct
> them. Revision and compare-and-set checks prevent an older update from
> overwriting a newer user decision.

## Slide 13 — Product Demo

> This slide shows how the design appears in the product.
>
> On the left, the user can review their own Prefer and Avoid memories. On the
> right, Ayue uses the relevant preference projection to make a more suitable
> recommendation.
>
> The important point is that personalization does not require exposing raw
> database documents, internal IDs, or another person's private information.
> The user can see and control what Ayue remembers, while each response receives
> only the minimum safe information needed for that turn.

### Slide 13 evidence note

The screenshot demonstrates durable preference display and personalized output.
It is not, by itself, proof that a particular sentence came from conversation
summary injection. Use the separate live summary acceptance evidence for that
claim.

## Slide 14 — Event-Driven Match

> The next feature is Event-Driven Match. Previously, matching was mainly
> passive: a user had to ask Ayue to find someone. We extended this into a more
> proactive workflow based on shared public activities.
>
> In the current Kaohsiung pilot, a weekly worker discovers public events in the
> next 30 days across five categories. It validates the sources, creates the
> Event and Concept graph, analyzes relevance and avoidance, and performs a fair,
> batched opportunity scan.
>
> Neo4j identifies users who share a valid opportunity. MongoDB owns the proposal
> state, consent state, cooldowns, history, and the event snapshot shown to the
> user. The workflow is incremental: valid events are refreshed and expired
> events are cleaned up; the system does not clear and rebuild the whole
> inventory on every run.
>
> The worker is persistent and recoverable. It uses checkpoints, leases, bounded
> retries, and deterministic deduplication. Each batch evaluates up to 30 users
> and can create up to 3 proposals; that is a per-batch limit, not a promise that
> every user receives a proposal.
>
> Most importantly, finding an opportunity never accepts a match automatically.
> The system creates an opportunity, and the users make the final decision.

### Slide 14 verified implementation detail

The latest recorded cycle (Run 19) searched 63 candidates and wrote 10 validated
events. After reconciliation, 29 active events remained: 6 market, 6 music, 5
sports, 6 festival, and 6 food. Relevance readiness was complete with 84
`EVENT_RELEVANCE` links. Thirty-six users were scanned: 12 draft proposals were
created, 14 already had an activity proposal, and 10 had no suitable candidate;
failed users were 0. The run was marked `partial` because three source URLs were
dead, not because consent or proposal state failed.

Do not read these numbers as “everyone was matched” or “all sources are
available.” They are evidence of a completed, recoverable cycle with bounded
partial failure handling.

## Slide 15 — User-Facing Consent Flow

> This slide shows the user-facing sequence: Event Proposal Card, Pending State,
> and Accepted State.
>
> First, the user receives a card explaining the public event and the possible
> matching opportunity. If the first user accepts, the proposal moves to a
> pending state. That does not count as acceptance for the other person; the
> second user receives a separate invitation.
>
> Only after both users accept does the system create a new pair chat, or reuse
> the existing pair chat when the relationship already exists. The event snapshot
> and a safe event-related opening message are then available in that chat.
>
> Every state transition is checked against the canonical status and revision.
> Therefore, Event-Driven Match creates an opportunity, but the final decision
> always belongs to the users.

## Safe progress-report claim

> We implemented proactive, event-driven matching from public activities through
> validated graph opportunities and a consent-based proposal flow. The workflow
> is persistent, incremental, and recoverable, with bounded batches and explicit
> user decisions. Our latest cycle completed with bounded partial source
> failures, while preserving proposal state and consent safety.

## Claims to avoid

- Do not say that every event source is always available.
- Do not say that every user is guaranteed a match or a proposal.
- Do not say that one user's acceptance automatically accepts for the other user.
- Do not say Neo4j stores consent state; MongoDB owns the proposal state machine.
- Do not use the old Planner/DAG diagram as the final deployed public runtime; it
  is retained only for this interim third-report presentation.
