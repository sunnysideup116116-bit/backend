---
name: event-food-discovery
description: Discover and verify public Kaohsiung food events for a bounded future window.
version: 1
---

# Kaohsiung Food Event Discovery

The server supplies a fixed future window, region, and bounded untrusted search
results. Every retained field must be grounded in those results.

## Search Plan

- Prefer official tourism, organizer, venue, shopping district, and government
  event pages with explicit dates.
- Search for food festivals, tasting events, culinary fairs, limited public
  dining programs, and ingredient-themed celebrations.
- When a page lists multiple separately dated food events, preserve each one
  only if its own title, date, and venue are explicit.

## Eligibility

Keep only a dated public food-centered event in Kaohsiung with title, date,
venue, and source URL.

Exclude ordinary restaurant listings, permanent menus, discount articles,
shopping guides, food news, trade-only recruitment, generic night-market pages,
and festivals where food is incidental rather than a main public activity.

## Output

Use concrete cuisine, ingredient, tasting, or food-activity tags. Never infer
menu items, price, availability, quality, popularity, or dietary suitability.
