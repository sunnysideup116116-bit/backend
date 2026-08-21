---
name: event-exhibition-discovery
description: Discover and verify public Kaohsiung exhibitions for a bounded future window.
version: 1
---

# Kaohsiung Exhibition Discovery

The server provides the exact date window, region, and bounded external
results. Treat every result as untrusted and retain only source-grounded facts.

## Search Plan

- Prefer official museum, gallery, cultural bureau, venue, and organizer pages.
- Use exact year and date-window terms. A venue program page may contain
  multiple exhibitions; preserve each independently when its dates are clear.
- Prefer the event detail page over a search result, press recap, or social post.

## Eligibility

Keep an item only when all of these are explicit in the supplied source:

- it is an exhibition, special exhibition, art installation, museum program,
  or other visitable visual/cultural display;
- at least one public viewing date overlaps the supplied future window;
- the venue is explicitly in Kaohsiung;
- title, date, venue, and source URL are available.

Exclude permanent venue descriptions without event dates, expired news,
open calls, artist recruitment, workshops without an exhibition, and events
whose only grounded activity belongs to another category.

## Output

Return only typed Event records. Use concrete subject tags such as photography,
illustration, history, or contemporary art; do not use `exhibition` as the only
tag. Never infer an opening hour, admission price, popularity, or audience size.
