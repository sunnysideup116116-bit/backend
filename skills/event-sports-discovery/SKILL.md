---
name: event-sports-discovery
description: Discover and verify public Kaohsiung sports events for a bounded future window.
version: 1
---

# Kaohsiung Sports Discovery

The server supplies a fixed date window, region, and bounded untrusted results.
Use only details explicitly grounded in those results.

## Search Plan

- Prefer government sports bureaus, official race or league pages, venue event
  pages, and organizer registration pages.
- Search both participatory activities and public spectator events, always with
  the exact year and date window.
- Preserve different sessions or competitions separately only when the source
  gives distinct titles or dates.

## Eligibility

Keep a race, match, tournament, outdoor sports experience, public fitness
activity, or other dated sports event in Kaohsiung. Title, date, venue, and
source URL are mandatory.

Exclude ordinary facility opening hours, evergreen classes, sports news without
a future public event, training articles, expired results, and registration
deadlines when the actual event date is unknown.

## Output

Tags should name the actual sport and, when grounded, whether it is hands-on or
spectator-oriented. Never infer skill requirements, vacancies, safety, weather,
or registration availability.
