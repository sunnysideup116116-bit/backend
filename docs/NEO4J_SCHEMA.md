# Neo4j Core Schema

Event-driven 主動媒人的完整資料流、API、排程、demo 與除錯請見
[`EVENT_DRIVEN_MATCHMAKER_GUIDE.md`](./EVENT_DRIVEN_MATCHMAKER_GUIDE.md)。

Neo4j is a compact relationship projection. MongoDB remains the source of
truth for profiles, recent context, workflow state, and preference evidence.

## Core Nodes

| Node | Properties | Purpose |
| --- | --- | --- |
| `User` | `id` | Stable identity used to connect graph relations. |
| `Concept` | `key`, `label`, `kind`, `embedding`, `embedded_at` | Reusable semantic concept. The 768-dimensional vector is computed once and reused. |
| `Event` | `id`, `dedupe_key`, `schema_version`, `status`, `title`, `summary`, `category`, `region`, `venue`, `starts_at`, `ends_at`, `time_precision`, `session_starts`, `session_ends`, `session_precisions`, `session_count`, `expires_at`, `source_url`, `source_name`, `source_tier`, `first_seen_at`, `last_seen_at` | Time-limited, verified public activity. Multi-session times use parallel primitive arrays rather than extra nodes. |

`Agent` and `GlobalRule` are retained as technical learning nodes. They are
not user profile storage.

## Core Relationships

| Relationship | Properties | Meaning |
| --- | --- | --- |
| `(User)-[:PREFERS]->(Concept)` | none | Durable positive preference. |
| `(User)-[:AVOIDS]->(Concept)` | none | Durable dealbreaker or negative preference. |
| `(User)-[:HAS_TRAIT]->(Concept)` | none | Public trait projection, when available. |
| `(User)-[:CURRENTLY_WANTS]->(Concept)` | `expires_at` | Optional short-lived intent projection. |
| `(Event)-[:HAS_TAG]->(Concept)` | none | Event topic or activity. |
| `(Event)-[:HAS_VIBE]->(Concept)` | none | Event atmosphere. |
| `(User)-[:EVENT_RELEVANCE]->(Event)` | bounded semantic evidence, embedding model, update time | Rebuildable candidate-retrieval cache; not a declared user preference. |
| `(User)-[:EVENT_AVOIDANCE]->(Event)` | bounded semantic evidence, embedding model, update time | Rebuildable hard block derived from active avoid/dislike concepts. |
| `(Agent)-[:LEARNED_RULE]->(GlobalRule)` | `weight` | Aggregated global match rule. |

## MongoDB Ownership

| Mongo location | Owned data |
| --- | --- |
| `profiles` | Onboarding profile, Big Five, deep profile, raw recent context, context signals and embeddings. |
| `matches` | Draft/pending/accepted/declined state machine and explicit decline reasons. |
| `preference_facts` | Full preference metadata: stance, confidence, lifecycle, source, reason, evidence count and timestamps. |
| `profile_memory_outbox` | Retry queue only; it is not canonical memory storage. |
| `profile_memory_preview` | Up to 12 display-ready active preferences projected into a profile. |
| `context_graph_outbox` | Retry state for projecting structured recent intent to Neo4j. |

## Rules

1. Neo4j relationships do not duplicate confidence, reason, source, match ID,
   counters, display labels, or lifecycle flags.
2. Disabling a preference deletes its graph relationship and marks the Mongo
   fact inactive. Restoring it recreates the relationship.
3. Raw conversation text and the full recent-context document never enter
   Neo4j. A `CURRENTLY_WANTS` edge contains only `expires_at`.
4. Event signals reuse `Concept`; separate `Tag`, `Vibe`, and `Category` node
   types are not part of the production schema.
5. `HAS_PREFERENCE` and `Trait` are migration-only compatibility structures.
6. Demo reseeding may delete `User` and user-owned memory projections, but it
   must preserve `Event`, `GlobalRule`, `Agent`, and Concepts still linked to
   preserved nodes. A blanket `MATCH (n) DETACH DELETE n` is forbidden.
7. A match decline creates `AVOIDS` only when the owner explicitly selects
   `decline_reason_options` and confirms that the reasons may be recorded. The
   successful match CAS happens first; the bounded `/api/feedback` normalizer
   then reuses `/api/memory/apply`. Decline without recording, cancellation,
   empty reasons, stale decisions, and inferred counterparty traits never write
   preference edges.

## Event Ingestion V1

- The pilot region is Kaohsiung and the accepted time window is the next 30 days.
- Port 8000 is the only web-search owner and uses the bounded Tavily adapter.
- Discovery performs separate bounded searches for exhibitions, markets,
  music, sports, festivals, and food activities. Every category owns a
  versioned validation skill plus an official-source query and a broader
  fallback query. Each query contributes at most two candidates and each
  category contributes at most four, so an earlier category cannot consume the
  whole budget. The Kaohsiung twmarket category page remains a pinned market
  source.
- Validation is sent to the matchmaker one category at a time. Kimi may take
  longer on a category without blocking or competing with the next category;
  a weak response cannot erase valid events from another category.
- Discovery may extract at most twelve source pages, with a fair maximum of two
  pages per category. Each extraction request asks for that category's dates,
  venue, and defining evidence. Content is truncated before validation and is
  never persisted.
- Extraction queries include the exact active date window. Event category is
  inherited from the server-owned discovery batch and cannot be relabeled by
  model output.
- Port 9001 receives typed search summaries, validates them with the matchmaker
  model, and writes only verified public fields to Neo4j.
- An event is rejected when its title, venue, explicit calendar date, or safe
  source URL is missing. Unknown dates are never replaced with a synthetic TTL.
- Date/time extraction uses ISO 8601. When a source states a date but no time,
  `time_precision=date` prevents the UI or agent from presenting an invented
  hour.
- Search snippets and raw pages are not stored. Event expiry is derived from
  the verified end time, with a one-day cleanup grace period.
- `Event.dedupe_key` has a uniqueness constraint and is derived from the
  normalized title and start-date bucket. This keeps
  separate activities from one venue listing page distinct while merging the
  same event found through different sources. Re-discovery updates the same
  node and refreshes its typed properties and Concept links.
- `Event` is the canonical store for public event data. MongoDB will own future
  invitation/consent workflow state; that state must not be added to Event
  properties.
- New Concepts are embedded once by a bounded background worker and stored in
  Neo4j. Each batch contains at most 20 Concepts; quota responses pause the
  worker for the provider retry interval. Event ingestion never waits for this
  work. `activity` concepts compare only with Event tags; `interest` concepts
  may compare with tags or vibes. Partner traits and values are excluded.
- Embedding similarity is a configurable retrieval threshold, not a
  compatibility score. Positive retrieval creates `EVENT_RELEVANCE`. Only an
  explicitly typed activity dislike may create `EVENT_AVOIDANCE`; partner-trait
  and general-interest dislikes remain user-to-user dealbreakers and must not
  suppress unrelated Events. A qualifying activity avoidance always suppresses
  the positive link for the same User/Event pair.
- Each user retains at most three `EVENT_RELEVANCE` relationships. Recent
  context evidence ranks before durable interests, followed by maximum semantic
  similarity and the nearest Event start time. `EVENT_AVOIDANCE` is never
  truncated because every hard block must remain effective.
- Event opportunity queries require `EVENT_RELEVANCE` for both people, reject
  either person's `EVENT_AVOIDANCE`, and retain the symmetric exact
  user-to-user dealbreaker check.
- Results expose bounded matched concepts as evidence. The matchmaker chooses
  which less-certain party to ask first, and neither party is assumed to accept.
- Both Event semantic relationships are disposable projections. Neo4j cosine
  comparison refreshes them after each Concept batch; deleting an expired Event
  removes them. Concept vectors remain reusable; missing or invalid-dimension
  vectors are backfilled incrementally.
- Event proposal cards use the immutable Mongo Event snapshot and expose only
  title, date/time precision, venue, region, category, and a validated public
  source URL. Neo4j Event IDs, internal summaries, ranking evidence, and user
  identifiers never enter the public card payload. Existing actionable cards
  receive the same projection when their canonical state is hydrated.

## Event Opportunity Consent

- Neo4j only discovers an Event bridge. It never stores invitation consent.
- Port 8000 validates the internal result and creates a canonical Mongo proposal
  with `proposal_namespace=event_invitation`, `proposal_source=event_opportunity`,
  an unordered `participant_pair_key`, and an immutable Event snapshot.
- The less-certain party becomes the draft initiator and receives one anonymous
  mediator card. Accepting moves the existing lifecycle to `pending`, which
  causes the existing match action service to notify the second party.
- The existing `draft -> pending -> accepted` CAS transitions remain shared,
  while live-proposal blocking is isolated by namespace. A relationship match
  and Event invitation can therefore be live at the same time.
- If the pair already has a verified accepted relationship, accepting another
  Event invitation reuses the existing chat and does not create a second
  relationship anchor or replay first-match effects.
- `event_opportunity_key` uniquely identifies one Event and unordered user pair,
  preventing repeated scans from creating duplicate invitations.
- A successful Event discovery requests one opportunity scan. Port 8000 waits
  until pending Concept embeddings are drained, then scans a bounded, weekly
  rotated user set. One scan creates at most three proposals by default; users
  with a live Event invitation are excluded before the matchmaker generates
  hooks. An explicit Event-invitation decline suppresses only that unordered pair
  in the Event namespace for seven days; ordinary relationship declines remain
  independent. Cancellation does not trigger the cooldown and accepted pairs
  remain eligible.
- Automatic scan limits are configured by
  `EVENT_OPPORTUNITY_MAX_PROPOSALS_PER_SCAN`,
  `EVENT_OPPORTUNITY_MAX_USERS_PER_SCAN`, and
  `EVENT_PAIR_DECLINE_COOLDOWN_DAYS`. The same scan can be run manually through
  `POST /api/match/events/opportunities/scan`.

## Event Lifecycle

- Port 8000 `event_lifecycle_service.py` checks due Mongo Event proposals every
  five minutes. The internal port 9001 cleanup runs once per day; port 9001
  deletes only expired `Event` nodes and Neo4j removes their disposable Event
  relationships through `DETACH DELETE`.
- Mongo remains the proposal source of truth. Unresolved Event proposals whose
  `event_snapshot.starts_at` is due are atomically changed from `draft/pending`
  to `expired`, incrementing `proposal_revision`, appending `state_history`, and
  releasing `live_participants`.
- `accepted`, `declined`, and already `expired` matches are never modified.
  Accepted chats retain the immutable Event snapshot even after Neo4j removes
  the expired public Event.
- Manual testing uses `POST /api/match/events/lifecycle/run`. The two worker
  intervals and optional pre-start lead are configured through
  `EVENT_PROPOSAL_EXPIRY_INTERVAL_SECONDS`,
  `EVENT_GRAPH_CLEANUP_INTERVAL_SECONDS`, and
  `EVENT_PROPOSAL_EXPIRY_LEAD_SECONDS`.

## Migration

Dry-run:

```powershell
.\.project-venv\Scripts\python.exe scripts\migrate_neo4j_preferences.py
```

Apply after reviewing counts:

```powershell
.\.project-venv\Scripts\python.exe scripts\migrate_neo4j_preferences.py --apply
```

The apply order is Mongo backup, graph conversion, old relationship removal,
then verification.

## Recent Intent Projection

After Mongo atomically updates `recent_context_state`, the background profile
task projects only `activity` and `destination` into `CURRENTLY_WANTS`. It does
not make another LLM call. Every replacement first removes the user's previous
intent edges, and expired edges are cleaned before graph opportunity searches.

## Projection Recovery

After an accidental loss of user projection data, rebuild from canonical Mongo
without touching Events or learned rules:

```powershell
.\.project-venv\Scripts\python.exe scripts\rebuild_neo4j_projection.py
.\.project-venv\Scripts\python.exe scripts\rebuild_neo4j_projection.py --apply
```

The apply path uses one Neo4j transaction, refuses an empty Mongo profile set,
deletes only stale Users, and verifies the final User count before commit. It
rebuilds only Mongo-owned `PREFERS`, `AVOIDS`, and `CURRENTLY_WANTS` edges, so
current users keep their Event relevance and avoidance edges. Event discovery
and scoped Event cleanup never delete `User` nodes.
