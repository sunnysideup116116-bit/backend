"""Reference NDJSON decoding and public-event validation for mobile clients."""

from __future__ import annotations

import codecs
import json
from typing import Any


_PUBLIC_EVENT_KEYS = {
    "run_started": frozenset({"type", "agent_run_id"}),
    "stage": frozenset({"type", "agent_run_id", "stage", "text"}),
    "tool_started": frozenset({"type", "agent_run_id", "text"}),
    "tool_finished": frozenset({"type", "agent_run_id", "outcome", "duration_ms"}),
    "token": frozenset({"type", "agent_run_id", "text"}),
    "final": frozenset({"type", "response"}),
    "error": frozenset({"type", "agent_run_id", "reply"}),
}
_TERMINAL_TYPES = frozenset({"final", "error"})


class NdjsonEventDecoder:
    """Decode arbitrary UTF-8 byte chunks into complete NDJSON objects."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buffer = ""
        self._finished = False

    def feed(self, chunk: bytes, *, final: bool = False) -> list[dict[str, Any]]:
        if self._finished:
            raise ValueError("decoder is already finalized")
        self._buffer += self._decoder.decode(chunk, final=final)
        lines = self._buffer.split("\n")
        self._buffer = lines.pop()
        if final:
            self._finished = True
            if self._buffer.strip():
                lines.append(self._buffer)
            self._buffer = ""
        events: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("NDJSON event must be an object")
            validate_public_event(event)
            events.append(event)
        return events


def validate_public_event(event: dict[str, Any]) -> None:
    event_type = event.get("type")
    allowed = _PUBLIC_EVENT_KEYS.get(event_type)
    if allowed is None:
        raise ValueError("unknown public event type")
    unexpected = set(event) - allowed
    if unexpected:
        raise ValueError("public event contains non-public fields")
    if event_type != "final" and not isinstance(event.get("agent_run_id"), str):
        raise ValueError("event needs an agent run ID")
    if event_type in {"stage", "tool_started", "token"} and not isinstance(event.get("text"), str):
        raise ValueError("tool progress needs text")
    if event_type == "tool_finished":
        if event.get("outcome") not in {"ok", "error"}:
            raise ValueError("tool outcome is invalid")
        if not isinstance(event.get("duration_ms"), int) or event["duration_ms"] < 0:
            raise ValueError("tool duration is invalid")
    if event_type == "final":
        response = event.get("response")
        if not isinstance(response, dict) or not isinstance(response.get("reply"), str):
            raise ValueError("final event needs a reply response")
    if event_type == "error" and not isinstance(event.get("reply"), str):
        raise ValueError("error event needs bounded reply text")


def validate_public_sequence(events: list[dict[str, Any]]) -> None:
    for event in events:
        validate_public_event(event)
    terminals = [index for index, event in enumerate(events) if event["type"] in _TERMINAL_TYPES]
    if len(terminals) != 1:
        raise ValueError("stream must contain exactly one terminal event")
    if terminals[0] != len(events) - 1:
        raise ValueError("terminal event must be last")


def build_match_decision(
    *,
    user_id: str,
    match_id: str,
    action: str,
    expected_status: str,
    expected_revision: int | None = None,
    explicit_reasons: list[str] | None = None,
) -> dict[str, Any]:
    if expected_status not in {"draft", "pending"}:
        raise ValueError("only a visible live proposal is actionable")
    allowed_actions = {
        "draft": {"accept", "decline"},
        "pending": {"accept", "decline", "cancel"},
    }
    if action not in allowed_actions[expected_status]:
        raise ValueError("action is invalid for the visible proposal state")
    if not user_id or not match_id:
        raise ValueError("decision needs owner and match identity")
    if expected_revision is not None and expected_revision < 0:
        raise ValueError("revision cannot be negative")
    payload: dict[str, Any] = {
        "user_id": user_id,
        "match_id": match_id,
        "action": action,
        "expected_status": expected_status,
        "explicit_reasons": [
            str(reason).strip()[:140]
            for reason in (explicit_reasons or [])[:8]
            if str(reason).strip()
        ],
    }
    if expected_revision is not None:
        payload["expected_revision"] = expected_revision
    return payload


class MatchDecisionTapGuard:
    """Per-card in-flight guard mirrored by the website's Set implementation."""

    def __init__(self) -> None:
        self._in_flight: set[str] = set()

    def begin(self, match_id: str) -> bool:
        if not match_id or match_id in self._in_flight:
            return False
        self._in_flight.add(match_id)
        return True

    def finish(self, match_id: str) -> None:
        self._in_flight.discard(match_id)


def match_decision_error_action(status_code: int) -> str:
    return "refresh_without_replay" if status_code == 409 else "show_retry"
