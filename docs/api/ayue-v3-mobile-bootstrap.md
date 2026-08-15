# Ayue V3 Mobile Bootstrap and Identity Contract

## Identity ownership

- Appwrite Account `$id` is the only value Flutter sends as Ayue `user_id`.
- Appwrite owns authentication, account display fields, profile photo, and posts.
- MongoDB owns Ayue assessment sessions, completed personality data, recent context, durable memories, chat, match, relationship, and calendar state.
- Flutter must not generate a second Ayue identifier or substitute email/phone for `$id`.

## New-account sequence

1. Create the Appwrite account and obtain `$id`.
2. Create/update the Appwrite public profile.
3. Initialize the canonical Ayue assessment with exactly one request:

```http
POST /api/chat
Content-Type: application/json

{
  "user_id": "<Appwrite $id>",
  "message": "",
  "state": "big_five",
  "initial_interest": "<optional explicit owner value>",
  "initialize": true
}
```

4. Navigate to the assessment UI using returned `assessment_state`, `assessment_kind`, and `assessment_revision`.
5. Later answers call the same endpoint with `initialize=false` or omitted.

The removed `/api/profile/big-five/initialize` route must not be restored merely for Flutter compatibility.

## Idempotency and preservation

- Repeated initialize calls reuse an active session instead of treating control text as an answer.
- Starting a draft never overwrites an already completed `big_five` or `deep_profile`.
- A completed draft replaces its corresponding profile only after explicit commit confirmation.
- Cancellation preserves the previously completed profile.
- `initial_interest` is written only when the supplied value is meaningful and the stored field is absent/empty. Omission, an empty value, or compatibility placeholders do not overwrite an existing choice.

## Failure handling

- If Appwrite account creation succeeds but Ayue initialization fails, keep the Appwrite account and offer a retry with the same `$id`; do not create a second account.
- A network retry uses the same initialization payload.
- An `already_started` response is success-equivalent for navigation.
- Invalid state is a client contract error and should be surfaced during development, not retried indefinitely.

## Trust boundary

The demo backend currently trusts body/query `user_id`. This is acceptable only for the controlled integration environment. Production deployment must verify an Appwrite JWT/session at the HTTP boundary, derive the authenticated owner ID server-side, and authorize any `other_id`/`match_id` access against canonical relationship state.
