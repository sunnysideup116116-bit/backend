# Ayue V3 Mobile Bootstrap and Identity Contract

## Identity ownership

- Appwrite Account `$id` is the only value Flutter sends as Ayue `user_id`.
- Appwrite owns authentication, the canonical account display fields, profile photo, and posts.
- MongoDB owns Ayue assessment sessions, completed personality data, recent context, durable memories, chat, match, relationship, and calendar state.
- The settings page mirrors explicitly edited public profile fields into MongoDB `profiles` for server-side fallback and matching projections. Appwrite remains the canonical profile store; the Mongo copy is not a second identity.
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
5. Later answers call the same endpoint with `initialize=false` or omitted and an optional `client_message_id` (1–160 characters). Flutter reuses that ID when retrying the same answer after a failed/lost response; a changed answer gets a new ID.

Flutter's initial Big Five screen must not insert a canned question before asking for an interest-based question. If no conversation has been saved, it retrieves the session's current question with `initialize=true`, never by submitting a hidden "please ask me" answer. Initialization uses a server-owned interest-based question (or a general leisure/planning question if no interest was supplied), needs no model call, and leaves `turn_count=0`. While waiting, the screen shows a loading state, not a second question. An already saved opening or answer is restored even when the compatibility history endpoint returns an empty list; reopening with that cache must not issue another opening request. A failed first request offers an explicit retry. Deep profiling uses the same initialization contract and does not append a second local follow-up question.

Old unanswered UI placeholders may be hidden during read projection. A placeholder that was actually answered, and all owner messages, remain in history.

## Existing-account profile edits

The settings page uses the same Appwrite `$id` for both writes:

1. Update the Appwrite `user_profiles` document with the edited fields.
2. Send the same allowlisted fields to `PATCH /api/profile`:

```http
PATCH /api/profile
Content-Type: application/json

{
  "user_id": "<Appwrite $id>",
  "name": "<display name>",
  "phone": "<optional phone>",
  "age": 28,
  "region": "高雄市",
  "photo_id": "<optional Appwrite file id>",
  "userinfo": "<optional self introduction>"
}
```

The endpoint upserts only the account key supplied by the current controlled
integration contract and accepts profile fields only. A `name` update also
refreshes the legacy Mongo `display_name` projection; an `interest` update
refreshes `initial_interest`, which is the matching-facing Mongo name for the
same registration value. Coarse location continues to use
`PATCH /api/profile/location`.

The removed `/api/profile/big-five/initialize` route must not be restored merely for Flutter compatibility.

## Idempotency and preservation

- Repeated initialize calls reuse an active session instead of treating control text as an answer.
- The session keeps its last reply and at most three bounded question/answer pairs (question ≤360 characters, answer ≤800), plus the interest explicitly supplied at this session's start. Assessment models receive only these own-session inputs and the typed draft; they do not inherit completed profiles, other chat rooms, Neo4j preferences, or general Mongo memory. Cancel/expire/commit removes these temporary dialogue fields.
- A duplicate successfully processed `client_message_id` returns the stored next question without another model call or revision increment. A repeated commit request with the same ID returns `already_committed`, not a new assessment.
- Starting a draft never overwrites an already completed `big_five` or `deep_profile`.
- A completed draft replaces its corresponding profile only after explicit commit confirmation.
- Cancellation preserves the previously completed profile.
- `initial_interest` is written only when the supplied value is meaningful and the stored field is absent/empty. Omission, an empty value, or compatibility placeholders do not overwrite an existing choice.
- Profile creation/upsert uses only `user_id`; the conditional interest update is not an upsert. Re-sending an interest must neither insert a second Mongo profile nor fail because the existing interest makes the conditional filter unmatched.

## Failure handling

- If Appwrite account creation succeeds but Ayue initialization fails, keep the Appwrite account and offer a retry with the same `$id`; do not create a second account.
- If an Appwrite profile edit succeeds but the Mongo mirror request fails, Appwrite remains canonical and the UI reports the failed save so the same edit can be retried; no second account or profile identifier is created.
- A network retry uses the same initialization payload.
- An `already_started` response is success-equivalent for navigation.
- Invalid state is a client contract error and should be surfaced during development, not retried indefinitely.
- Provider failures retain HTTP 200 for compatibility but return `status="error"`, `outcome="provider_error"`, a safe `error_code`, `retryable`, and the unchanged assessment state/revision. Clients must not treat this as a successful assistant turn. Flutter keeps the original answer editable, offers retry, and does not cache the error as dialogue.
- The assessment provider boundary validates JSON object shape, nonempty string reply, boolean completion and typed draft fields before any state change. Malformed/empty output and transient connection/server errors get at most one automatic retry within a shared 40-second transport budget; timeout, quota (429), auth and unavailable-model failures do not hot-retry. Flutter allows 65 seconds for the assessment HTTP response. Provider failure never advances the draft or turn count.
- `assessment_provider_failure` logs kind/model/code/attempt/elapsed time; `assessment_turn_failed` logs kind/code/revision. Neither includes raw answers, prompts, exception messages, or credentials. Existing `start_all.sh` log files are overwritten on restart, so missing historical entries cannot establish which provider error happened in an earlier deployment.

## Trust boundary

The demo backend currently trusts body/query `user_id`. This is acceptable only for the controlled integration environment. Production deployment must verify an Appwrite JWT/session at the HTTP boundary, derive the authenticated owner ID server-side, and authorize any `other_id`/`match_id` access against canonical relationship state.
