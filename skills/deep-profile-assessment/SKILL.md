---
name: deep-profile-assessment
version: v1
surface: public_ayue
---

# Deep Profile Assessment

Use only through `profile.start_assessment` with `kind=deep` after an explicit
user confirmation. The runtime owns the session lifecycle and writes the completed
`deep_profile` only after a separate owner commit confirmation.

Each answer may use only the current owner message and the bounded typed draft.
Do not read or persist the completed profile, current context, conversation
history, raw prompts, assistant history, tool results, match state, or other
users' information in the session.
