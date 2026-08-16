---
name: basic-profile-assessment
version: v1
surface: public_ayue
---

# Basic Profile Assessment

Use only through `profile.start_assessment` with `kind=basic` after an explicit
user confirmation. The runtime owns session creation, revision checks, cancellation
and the final commit confirmation; this skill never writes `big_five` itself.

The assessment input is the current owner message plus the bounded typed draft.
Do not use conversation history, match state, tool results, internal IDs or
other users' data. A completed typed draft remains pending until the owner
explicitly confirms it; cancelling keeps the previously completed profile.
