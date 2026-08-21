---
name: event-music-discovery
description: Discover and verify public Kaohsiung music events for a bounded future window.
version: 1
---

# Kaohsiung Music Discovery

The server supplies a fixed date window, region, and bounded untrusted search
results. Every output fact must be supported by those results.

## Search Plan

- Prefer official venue schedules, festival sites, ticketing detail pages, and
  organizer announcements with explicit performance dates.
- Search for concerts, live music, music festivals, orchestral programs, and
  other public performances using the exact year and date window.
- A program listing may contain several performances; keep them separate when
  each title and date are grounded.

## Eligibility

Keep only a public music performance inside the supplied window and in an
explicit Kaohsiung venue. Title, date, venue, and source URL are mandatory.

Exclude album/news articles, artist profiles, venue home pages without a dated
show, rehearsals, registration-only auditions, expired recaps, and events where
music is merely background to a stronger non-music category.

## Output

Use concrete genre or format tags such as jazz, rock, orchestra, indie music,
or outdoor concert. Never invent performers, set times, ticket availability,
or popularity.
