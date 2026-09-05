# Ayue V3 Mobile Bootstrap and Identity Contract

## Identity ownership

- Appwrite Account `$id` is the only value Flutter sends as Ayue `user_id`.
- Appwrite owns authentication, account display fields, profile photo, and posts.
- MongoDB owns Ayue assessment sessions, completed personality data, recent context, durable memories, chat, match, relationship, and calendar state.
- Flutter must not generate a second Ayue identifier or substitute email/phone for `$id`.
- Registration asks about usual interests and leisure activities. Appwrite stores this explicit owner value in `user_profiles.interest`; the assessment adapter stores it in MongoDB `profiles.initial_interest`. It is not the user's `current_context` or a new recent-intent field.

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

Flutter's initial Big Five screen must not insert a canned question before asking for an interest-based question. When an interest exists and no conversation has been saved, it requests one concrete interest-based question through the existing assessment endpoint. While waiting, it shows a loading state, not a second question. An already saved opening or answer is restored even when the compatibility history endpoint returns an empty list; reopening must not issue another opening request. A failed first request offers an explicit retry. Without an interest, the screen asks one question about the user's usual activities.

Old unanswered UI placeholders may be hidden during read projection. A placeholder that was actually answered, and all owner messages, remain in history.

The removed `/api/profile/big-five/initialize` route must not be restored merely for Flutter compatibility.

## Idempotency and preservation

- Repeated initialize calls reuse an active session instead of treating control text as an answer.
- Starting a draft never overwrites an already completed `big_five` or `deep_profile`.
- A completed draft replaces its corresponding profile only after explicit commit confirmation.
- Cancellation preserves the previously completed profile.
- `initial_interest` is written only when the supplied value is meaningful and the stored field is absent/empty. Omission, an empty value, or compatibility placeholders do not overwrite an existing choice.
- Profile creation/upsert uses only `user_id`; the conditional interest update is not an upsert. Re-sending an interest must neither insert a second Mongo profile nor fail because the existing interest makes the conditional filter unmatched.

## Failure handling

- If Appwrite account creation succeeds but Ayue initialization fails, keep the Appwrite account and offer a retry with the same `$id`; do not create a second account.
- A network retry uses the same initialization payload.
- An `already_started` response is success-equivalent for navigation.
- Invalid state is a client contract error and should be surfaced during development, not retried indefinitely.

## Trust boundary

The demo backend currently trusts body/query `user_id`. This is acceptable only for the controlled integration environment. Production deployment must verify an Appwrite JWT/session at the HTTP boundary, derive the authenticated owner ID server-side, and authorize any `other_id`/`match_id` access against canonical relationship state.
