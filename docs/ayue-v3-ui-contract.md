# Ayue V3 Website-to-Flutter UI Contract

`Server/social/frontend.html` is the executable visual reference. Flutter may adapt spacing and platform controls, but must preserve the same states, actions, concurrency rules, privacy boundaries, and fallback behavior.

## Universal rules

- Flutter must not infer match, assessment, calendar, context, safety, or delivery state from Ayue reply copy.
- Typed response fields and canonical state endpoints are authoritative.
- Unknown additive fields are ignored and retained when practical; missing optional fields use the documented fallback.
- One user action sends one HTTP request. JSON and stream chat endpoints are never called together for the same message.
- Cards retain their typed metadata. Internal prompt text, raw tool arguments/results, private memory, and model-only IDs never become visible UI.
- Every async action disables only its own control, remains idempotent from the user's perspective, and exposes a retry only when retry is safe.

## Surface mapping

| Surface | Trigger and transport | Required typed input | Loading state | Success state | Error/dismissal | Flutter component |
|---|---|---|---|---|---|---|
| Public Ayue stream | Send to `POST /api/direct_chat/stream`; parse UTF-8 NDJSON | `user_id`, `contact_id=ai_assistant`, `message`; optional mention IDs, `mentions_inline`, `assessment_action` | After 250 ms show one Ayue progress bubble; update it from safe `tool_started.text` | Remove progress; render `final.response` exactly once | Remove progress on `error`, EOF, timeout, cancellation, route disposal; show bounded retry message | `AyueChatController`, one ephemeral `AyueProgressBubble` |
| Public Ayue presentation | `final.response` | `reply`; optional `messages`, `presentation_blocks`, `sources`, `place_cards` | No second typing indicator after terminal event | Render ordered messages and blocks; cards appear after their associated text | If structured content is absent/invalid, render escaped Markdown/plain text from `reply` | Typed message list plus block renderers |
| Assessment controls | History/final fields `assessment_state`, `assessment_kind`, `assessment_revision` | State `active` or `awaiting_commit` | Disable exit while stream request is active | Show kind, current state, and server copy; exit sends typed `assessment_action=cancel` | Hide when state is absent/terminal; failed cancel restores prior control state | Persistent assessment status bar |
| Recent-context confirmation | `context_confirmation_needed`; background status at `GET /api/profile/recent-context/status` | `profile_process_run_key`, current revision | No visible process bubble; bounded polling at 0.5/1/2/4/8 seconds | Refresh displayed context only when process is completed/updated and revision increases | Stop on timeout, superseded, unavailable, contact change, or exhausted attempts | Inline confirmation notice plus silent controller poll |
| Match search progress | `POST /api/match/request`, then `GET /api/match/status` | `search.status`, `step`, optional reason | One in-flow progress card, polling about once per second; allow collapse/expand | Remove progress when terminal; render proposal/status projection | Stop polling after repeated failures; retry is explicit; `POST /api/match/cancel` cancels active search | Collapsible `MatchSearchProgressCard` |
| Match proposal decision | Message metadata/status card and `POST /api/match/decision` | `match_id`, `action`, `expected_status`, optional `expected_revision`, `explicit_reasons` | Disable this card's actions; reject double taps | Render new canonical status/stage; open chat only after accepted state says it is available | HTTP 409 refreshes canonical state and does not replay; decline dialog dismiss leaves card unchanged | `MatchProposalCard` plus `DeclineReasonSheet` |
| Calendar | `GET/POST/PATCH /api/calendar/events`, cancel/reschedule routes and settings | Typed event ID, revision/confirmation fields required by endpoint | Modal/screen-specific spinner; disable edited event only | Refresh agenda after mutation and after `calendar_state_changed` | Preserve editor draft on retryable error; close action never mutates | `CalendarScreen`, `CalendarEditorSheet`, event action menu |
| Date coordination | Shared-room `mediator_card` metadata plus relationship date update/confirm/cancel APIs | Match/date coordination state and canonical identifiers | Disable current action; show server-owned pending state | Render invitation, editable form, waiting, confirmed, cancelled, and calendar-link states inside the shared room | Refresh shared messages after conflict; terminal cards remain visible but inactive | Calendar-style typed `DateCoordinationCard` variants |
| Private mediator | JSON or `POST /api/mediator/private/stream` for the private surface only | Owner ID, other ID, message | Private progress/typing state isolated from Public Ayue | Render private advice and the supported observation/date-coordination actions only | Closing panel cancels UI work; must not auto-submit a handoff into Public Ayue | `MediatorPrivateSheet` with its own controller; no relationship-quiz UI |
| Notifications and contacts | `GET /api/notifications`, `GET /api/contacts`, proactive delivery | Public projection only | Non-blocking refresh indicator | Add accepted contacts and privacy-safe invitation cards | Failed polling does not interrupt the active chat | Notification inbox and contacts controller |
| Settings and memories | `/api/settings`, mediator/model settings, `/api/profile/memories` and action route | Typed enum/value plus owner ID | Disable only changed row/action | Replace row from server result or refresh | Restore previous local selection on failure | Settings sections and memory management list |
| Profile location | `GET /api/init` (`user_location`) and `PATCH /api/profile/location` | Owner ID plus coarse city and district only | Load/retry state is isolated inside profile editing | Replace city/district from the server projection after save | Never overwrite an unread location with empty values; exact address is never requested | `ProfileEditPage` coarse-location section |
| Pair-chat risk | Non-Ayue `POST /api/direct_chat` before persistence | Accepted pair plus `client_message_id`; bounded risk projection only | Keep one local pending bubble | Delivered and unavailable levels remain visible; warning/restricted may show a boundary hint | Only blocked stays local failed and never enters receiver history | Existing chat bubble with bounded shield/error state |

## Public Ayue stream

Allowed public event types are `run_started`, `tool_started`, `tool_finished`, `final`, and `error`. Flutter must decode arbitrary byte chunks with a stateful UTF-8 decoder, buffer partial lines, ignore blank lines, and parse every complete line independently.

Only one progress bubble may exist. `run_started` uses the default thinking copy; `tool_started` may replace it with the bounded server text. `tool_finished` updates diagnostics only and does not create a second bubble. A terminal event, EOF, timeout, navigation, or exception always clears the bubble.

The website uses a 120-second request timeout. Flutter should use the same initial timeout unless mobile lifecycle requirements demand earlier user cancellation.

## Presentation blocks and place cards

- `messages` is an ordered list and may contain up to the server-defined bound; do not collapse it into one untyped string.
- `presentation_blocks` selects structured renderers. Unknown block kinds fall back to their safe text projection.
- `sources` show title/domain/link only after URL validation.
- `place_cards` are optional and feature-gated. Render name, category/address/distance/rating/hours/photo/map fields only when present and valid.
- When place cards are disabled, incomplete, or unsupported, the text/Markdown answer remains complete.
- Remote image failure shows a neutral placeholder and never removes the surrounding message.

## Assessment controls

`active` means answers continue in chat. `awaiting_commit` means a completed draft exists but has not replaced the prior profile. Exiting uses the narrow typed cancel action; Flutter must not translate arbitrary button labels into agent commands. A completed profile is updated only after the separate server confirmation flow.

## Recent-context confirmation

The confirmation notice is driven by `context_confirmation_needed`, not keyword matching. Background extraction is visually silent. A new context replaces local display only when the returned revision is greater than the baseline captured for that run.

## Match search progress

The website maps durable search steps into a single collapsible in-chat card. Flutter should preserve the ordering and server descriptions without exposing internal candidate data. `no_suitable_candidate`, cancelled, failed, and completed are distinct terminal outcomes.

## Match proposal decision

Lifecycle:

```text
draft -> pending | declined | expired
pending -> accepted | declined | expired
```

The initiator's pending cancellation maps to action `cancel`; receiver rejection maps to `decline`. A 409 response means the visible card is stale: read current state/status, update the card, and never automatically replay the previous decision. Accepted/declined/expired history remains visible and permanently inactive.

## Calendar

Flutter exposes the Calendar from both Public Ayue and the main Chat page. It includes personal/shared access indication, create form, cancel, shared-date reschedule proposal, reschedule withdrawal, and settings. A completed shared-date card links directly to the Calendar. Mutations follow backend confirmation/CAS/idempotency rules; a 409 refreshes the agenda, and `calendar_state_changed=true` invalidates any open calendar cache.

## Date coordination

Date cards are driven by typed `mediator_card` coordination metadata rather than chat copy and render in the shared pair room, never inside Private Ayue. Their editor follows the Calendar form language: date picker, start/end dropdowns, and spaced activity/location/budget/notes fields. Both users may see different allowed actions. Confirmed dates link to Calendar; cancelled or superseded cards remain as inactive history.

## Private mediator

Private Ayue remains V2 and must use a separate controller/state namespace. A handoff can prefill Public Ayue, but must not auto-submit. Public mentions and private relationship context must not cross surfaces accidentally. The relationship-quiz control and card are not part of the current website surface and must remain absent from Flutter.

## Notifications and contacts

Only privacy-safe public projections are displayed. An accepted match creates a contact only after canonical acceptance evidence. Proactive polling failures are silent and never replace the current chat with an error screen.

## Settings and memories

`proactive_frequency` accepts `none`, `60`, `3600`, or `86400`. Memory actions use server keys and typed actions; Flutter does not expose raw stored documents. Runtime model controls appear only when the capability/config response authorizes them.
