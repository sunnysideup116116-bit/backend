# Ayue V3 Risk Projection Contract

## Placement

Risk evaluation is mandatory server policy before persistence. It is not a Planner-selected tool and must not be added to the Public Ayue tool registry. Planner, Guard, Runtime, and Synthesizer retain the canonical V3 flow after the message passes the safety boundary.

```text
authenticated request
-> bounded risk request
-> delivery decision
-> exactly-once owner-message persistence
-> canonical Ayue V3 or pair-chat flow
```

The reference adapter remains at `tests/contracts/fixtures/ayue_v3_risk_adapter.py`. The live pair-chat boundary is implemented by `social/services/risk_policy_service.py` and is called by the non-Ayue branch of `routers/public_chat.py` before receiver-visible persistence. Public V3 and Private V2 orchestration are unchanged.

## Delivery policy

| Risk level | Persistence/delivery | Public priority |
|---|---|---|
| `safe` | allow once | coach |
| `observation` | allow once | coach |
| `warning` | allow once | risk |
| `restricted` | allow once | risk |
| `blocked` | do not persist as a receiver-visible message | risk |
| unavailable/invalid | allow once with `level=unavailable` (fail-open) | coach |

`restricted` is intentionally deliverable and must never be collapsed into `blocked`. If the risk service times out, cannot connect, returns 5xx, malformed JSON, or an unknown level, the pair message is persisted with `level=unavailable`. This is fail-open availability behavior, not a declaration that the message is safe; only an explicit `blocked` response prevents persistence.

## Bounded mobile projection

Flutter receives only:

```json
{
  "level": "restricted",
  "ui_priority": "risk",
  "delivery": "delivered"
}
```

Raw diagnosis, risk state, classifier evidence, prompts, relationship history, and intervention internals remain server-only. The projection may be attached to a JSON final response or `final.response` in NDJSON.

## Idempotency and history

- Each client send attempt supplies a server-scoped idempotency key.
- Re-evaluating the same key returns the cached decision and does not call the risk service again within the bounded adapter lifetime.
- A `MessagePersistencePermit` is single-use.
- Receiver history always filters `is_blocked != true`.
- Risk-backend audit storage may retain a blocked event under its own access controls; it must not become a delivered chat message.

## Wiring verification gate

Before changing the live adapter or persistence boundary:

1. Run GitNexus upstream impact analysis on the exact router/service symbol.
2. Warn before HIGH or CRITICAL edits.
3. Confirm owner-message persistence still occurs exactly once for both JSON and stream paths.
4. Confirm blocked content never reaches receiver history.
5. Add the reason and before/after behavior to `docs/ayue-v3-import-record.md`.
