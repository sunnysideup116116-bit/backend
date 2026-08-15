# Ayue V3 Calendar and Relationship Mobile Contract

## Calendar

- List: `GET /api/calendar/events?user_id=&from=&to=&include_cancelled=`.
- Create personal event: `POST /api/calendar/events`.
- Update personal event: `PATCH /api/calendar/events/{event_id}` with optional `expected_revision`.
- Cancel event: `POST /api/calendar/events/{event_id}/cancel` with optional `expected_revision`.
- Request shared-date reschedule: `POST /api/calendar/events/{event_id}/reschedule`.
- Withdraw reschedule: `POST /api/calendar/events/{event_id}/reschedule/cancel`.
- Calendar access settings: `GET/PATCH /api/calendar/settings`.

Flutter echoes `event_id` and revisions obtained from typed state. It never extracts them from Ayue copy. Mutation conflicts refresh the event/coordination state before another user action. Personal event forms require title, date, start/end time, and timezone; optional location/notes remain bounded server fields.

## Date coordination

- Invite response: `POST /api/relationship/date/invite/respond`.
- Current state: `GET /api/relationship/date/state`.
- Update form: `POST /api/relationship/date/update` with `coordination_id` and `revision`.
- Confirm form: `POST /api/relationship/date/confirm` with `coordination_id` and `revision`.
- Cancel: `POST /api/relationship/date/cancel`.

The coordination projection is canonical. Cards remain inactive history after confirmation, cancellation, or supersession. `calendar_state_changed` invalidates open agenda caches, but does not itself describe the event state.

## Relationship quiz

- Read: `GET /api/relationship/fun/{other_id}`.
- Start: `POST /api/relationship/quiz/start`.
- Answer: `POST /api/relationship/quiz/answer` with typed answer mapping.
- Cancel: `POST /api/relationship/quiz/cancel`.

All quiz actions require an accepted canonical match. The removed `/api/relationship/topic` route has no Flutter fallback and its old button must be hidden rather than redirected to an unrelated operation.
