"""Preview-bound confirmation manager with one active action per user."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Any, Callable

from pydantic import BaseModel


INTERACTION_BUBBLE = "bubble_buttons_v1"
INTERACTION_LEGACY = "legacy_text"
SURFACE_PUBLIC = "public_ayue"
SURFACE_PRIVATE = "private_ayue"
ASSESSMENT_COMMIT_ACTION = "profile.commit_assessment"

# These writes were historically confirmed by sending a standalone text token.
# Match proposal / event-invitation decisions are intentionally absent: their
# existing mediator cards and text fallback must remain untouched.
BUBBLE_BUTTON_ACTIONS = frozenset({
    "calendar.submit_commands",
    "match.start_search",
    "profile.start_assessment",
    "relationship.start_date_coordination",
    ASSESSMENT_COMMIT_ACTION,
    "private.date.start_coordination",
})
LEGACY_CARD_ACTIONS = frozenset({
    "match.decide_active_proposal",
    "match.decide_active_event_invitation",
})


def interaction_mode_for_action(action_name: str) -> str:
    return (
        INTERACTION_BUBBLE
        if str(action_name or "") in BUBBLE_BUTTON_ACTIONS
        else INTERACTION_LEGACY
    )


def public_choice_projection(record: dict[str, Any]) -> dict[str, Any]:
    """Return the only confirmation fields allowed across the client boundary."""
    status = str(record.get("status") or "")
    selected = str(record.get("selected_choice") or "") or None
    state = {
        "prepared": "pending",
        "pending": "pending",
        "executing": "pending",
        "completed": "confirmed",
        "cancelled": "cancelled",
        "auto_cancelled": "auto_cancelled",
        "expired": "expired",
        "superseded": "superseded",
        "failed": "failed",
    }.get(status, "failed")
    return {
        "id": str(record.get("_id") or ""),
        "state": state,
        "selected": selected,
        "expires_at": float(record.get("expires_at", 0) or 0),
    }


def sync_choice_message_projection(
    message_collection: Any,
    *,
    room_id: str,
    projection: dict[str, Any] | None,
) -> bool:
    """Persist a resolved choice on its original assistant message."""
    choice_id = str((projection or {}).get("id") or "")
    if not room_id or not choice_id:
        return False
    if (
        os.getenv("AYUE_TEST_MODE", "").strip().lower() in {"1", "true", "on"}
        and message_collection.__class__.__module__.startswith("pymongo")
    ):
        return False
    try:
        result = message_collection.update_one(
            {
                "room_id": room_id,
                "metadata.choice_prompt.id": choice_id,
            },
            {"$set": {"metadata.choice_prompt": dict(projection or {})}},
        )
    except Exception:
        return False
    return getattr(result, "modified_count", 0) == 1


class ConfirmationRecord(BaseModel):
    confirmation_id: str
    user_id: str
    agent_name: str
    tool_name: str
    arguments: dict[str, Any]
    status: str = "prepared"
    created_at: float
    expires_at: float
    origin_run_id: str
    request_fingerprint: str
    preview_fingerprint: str = ""
    expected_persisted_fingerprint: str = ""
    presented_message_id: str | None = None
    presented_at: float | None = None
    room_id: str = ""
    surface: str = SURFACE_PUBLIC
    interaction_mode: str = INTERACTION_LEGACY
    selected_choice: str | None = None


class ConfirmationManager:
    """Owns one preview-bound pending confirmation per user with CAS execution."""

    def __init__(self, collection: Any) -> None:
        self._coll = collection

    def create_confirmation(
        self, *, user_id: str, agent_name: str, tool_name: str,
        arguments: dict[str, Any], ttl_seconds: int = 900,
        payload: dict[str, Any] | None = None,
        origin_run_id: str,
        preview: str,
        room_id: str = "",
        surface: str = SURFACE_PUBLIC,
        interaction_mode: str | None = None,
    ) -> str:
        if not origin_run_id.strip() or not preview.strip():
            raise ValueError("confirmation requires an origin run and visible preview")
        confirmation_id = uuid.uuid4().hex
        now = time.time()
        safe_payload = payload or {}
        request_fingerprint = self._request_fingerprint(
            tool_name=tool_name,
            arguments=arguments,
            payload=safe_payload,
            origin_run_id=origin_run_id,
        )
        preview_fingerprint = hashlib.sha256(preview.encode("utf-8")).hexdigest()
        mode = interaction_mode or interaction_mode_for_action(tool_name)
        if mode == INTERACTION_BUBBLE and not str(room_id or "").strip():
            raise ValueError("bubble confirmation requires a room_id")
        # Button-mode confirmations are room-scoped because the opaque choice
        # id removes the old ambiguity of a plain text "confirm". Legacy card
        # fallback remains globally single and is deliberately isolated.
        supersede_query: dict[str, Any] = {
            "user_id": user_id,
            "status": {"$in": ["prepared", "pending"]},
            "interaction_mode": mode,
        }
        if mode == INTERACTION_BUBBLE:
            supersede_query.update({
                "surface": surface,
                "room_id": room_id,
            })
        self._coll.update_many(
            supersede_query,
            {"$set": {
                "status": "superseded",
                "superseded_at": now,
                "resolution_reason": "replacement",
            }},
        )
        if mode == INTERACTION_LEGACY:
            self._coll.update_many(
                {
                    "user_id": user_id,
                    "tool_name": {"$in": list(LEGACY_CARD_ACTIONS)},
                    "status": {"$in": ["prepared", "pending"]},
                },
                {"$set": {
                    "status": "superseded",
                    "superseded_at": now,
                    "resolution_reason": "replacement",
                }},
            )
        self._coll.insert_one({
            "_id": confirmation_id,
            "user_id": user_id,
            "agent_name": agent_name,
            "tool_name": tool_name,
            "arguments": arguments,
            "payload": safe_payload,
            # Every bubble choice is inactive until the exact assistant preview
            # is persisted. Legacy card fallback retains its prior behavior.
            "status": "prepared" if mode == INTERACTION_BUBBLE else "pending",
            "created_at": now,
            "expires_at": now + ttl_seconds,
            "origin_run_id": origin_run_id,
            "request_fingerprint": request_fingerprint,
            # This is only the server preview source digest.  The public
            # preview fingerprint is assigned from persisted assistant text
            # by mark_presented after final normalization.
            "preview_source_fingerprint": preview_fingerprint,
            "preview_fingerprint": "" if mode == INTERACTION_BUBBLE else preview_fingerprint,
            "expected_persisted_fingerprint": "",
            "presented_message_id": None,
            "presented_at": None,
            "room_id": str(room_id or ""),
            "surface": str(surface or SURFACE_PUBLIC),
            "interaction_mode": mode,
            "selected_choice": None,
        })
        return confirmation_id

    def bind_final_preview(
        self, *, user_id: str, origin_run_id: str, final_content: str,
    ) -> bool:
        """Bind the normalized final reply before it is persisted."""
        content = str(final_content or "")
        if not origin_run_id.strip() or not content.strip():
            return False
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        try:
            result = self._coll.update_one(
                {
                    "user_id": user_id,
                    "origin_run_id": origin_run_id,
                    "status": "prepared",
                },
                {"$set": {"expected_persisted_fingerprint": digest}},
            )
        except Exception:
            return False
        return getattr(result, "modified_count", 0) == 1

    def mark_presented(
        self, *, user_id: str, origin_run_id: str, message_id: str,
        persisted_content: str,
    ) -> bool:
        """Activate a prepared confirmation only for saved final text."""
        content = str(persisted_content or "")
        message_key = str(message_id or "").strip()
        if not origin_run_id.strip() or not message_key or not content.strip():
            return False
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        now = time.time()
        try:
            result = self._coll.update_one(
                {
                    "user_id": user_id,
                    "origin_run_id": origin_run_id,
                    "status": "prepared",
                    "expected_persisted_fingerprint": digest,
                },
                {
                    "$set": {
                        "status": "pending",
                        "preview_fingerprint": digest,
                        "presented_message_id": message_key,
                        "presented_at": now,
                    }
                },
            )
        except Exception:
            return False
        return getattr(result, "modified_count", 0) == 1

    @staticmethod
    def _request_fingerprint(
        *, tool_name: str, arguments: dict[str, Any], payload: dict[str, Any], origin_run_id: str,
    ) -> str:
        canonical = json.dumps(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "payload": payload,
                "origin_run_id": origin_run_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def list_active(
        self,
        *,
        user_id: str,
        room_id: str | None = None,
        surface: str | None = None,
        interaction_mode: str | None = None,
        choice_id: str | None = None,
    ) -> list[dict[str, Any]]:
        now = time.time()
        query: dict[str, Any] = {
            "user_id": user_id,
            "status": "pending",
            "expires_at": {"$gt": now},
        }
        if room_id is not None:
            query["room_id"] = room_id
        if surface is not None:
            query["surface"] = surface
        if choice_id is not None:
            query["_id"] = choice_id
        try:
            cursor = self._coll.find(query)
            return [
                record for record in cursor
                if (
                    interaction_mode is None
                    or str(
                        record.get("interaction_mode")
                        or interaction_mode_for_action(str(record.get("tool_name") or ""))
                    )
                    == interaction_mode
                )
                and str(record.get("preview_fingerprint") or "")
                and (
                    str(record.get("interaction_mode") or INTERACTION_LEGACY)
                    != INTERACTION_BUBBLE
                    or (
                        str(record.get("presented_message_id") or "")
                        and record.get("presented_at") is not None
                    )
                )
            ]
        except Exception:
            # Confirmation lookup is safety-critical.  On storage failure, an
            # empty projection prevents both stale execution and planner leaks.
            return []

    def choice_for_run(
        self,
        *,
        user_id: str,
        room_id: str,
        surface: str,
        origin_run_id: str,
    ) -> dict[str, Any] | None:
        """Return the bubble choice created by this exact agent run."""
        try:
            records = list(self._coll.find({
                "user_id": user_id,
                "room_id": room_id,
                "surface": surface,
                "origin_run_id": origin_run_id,
                "interaction_mode": INTERACTION_BUBBLE,
                "status": {"$in": ["prepared", "pending"]},
            }))
        except Exception:
            return None
        if not records:
            return None
        records.sort(key=lambda rec: float(rec.get("created_at", 0) or 0), reverse=True)
        return public_choice_projection(records[0])

    def choice_projection(
        self,
        *,
        user_id: str,
        room_id: str,
        surface: str,
        choice_id: str,
    ) -> dict[str, Any] | None:
        try:
            records = list(self._coll.find({
                "_id": choice_id,
                "user_id": user_id,
                "room_id": room_id,
                "surface": surface,
                "interaction_mode": INTERACTION_BUBBLE,
            }))
        except Exception:
            return None
        if not records:
            return None
        record = records[0]
        if (
            str(record.get("status") or "") in {"prepared", "pending"}
            and float(record.get("expires_at", 0) or 0) <= time.time()
        ):
            self._coll.update_one(
                {"_id": choice_id, "user_id": user_id, "status": {"$in": ["prepared", "pending"]}},
                {"$set": {"status": "expired", "resolution_reason": "ttl"}},
            )
            record = {**record, "status": "expired"}
        return public_choice_projection(record)

    def record_for_choice(
        self,
        *,
        user_id: str,
        room_id: str,
        surface: str,
        choice_id: str,
        require_pending: bool = True,
    ) -> dict[str, Any] | None:
        query: dict[str, Any] = {
            "_id": choice_id,
            "user_id": user_id,
            "room_id": room_id,
            "surface": surface,
            "interaction_mode": INTERACTION_BUBBLE,
        }
        if require_pending:
            query.update({"status": "pending", "expires_at": {"$gt": time.time()}})
        try:
            records = list(self._coll.find(query))
        except Exception:
            return None
        return records[0] if records else None

    def resolve_for_continuation(
        self,
        *,
        user_id: str,
        room_id: str,
        surface: str,
    ) -> dict[str, Any] | None:
        """Cancel only the visible button choice in the current room."""
        active = self.list_active(
            user_id=user_id,
            room_id=room_id,
            surface=surface,
            interaction_mode=INTERACTION_BUBBLE,
        )
        if not active:
            return None
        active.sort(key=lambda rec: float(rec.get("created_at", 0) or 0), reverse=True)
        record = active[0]
        choice_id = str(record.get("_id") or "")
        result = self._coll.update_one(
            {"_id": choice_id, "user_id": user_id, "status": "pending"},
            {"$set": {
                "status": "auto_cancelled",
                "selected_choice": "cancel",
                "resolved_at": time.time(),
                "resolution_reason": "conversation_continued",
            }},
        )
        if getattr(result, "modified_count", 0) != 1:
            return None
        return public_choice_projection({
            **record,
            "status": "auto_cancelled",
            "selected_choice": "cancel",
        })

    def mark_superseded(
        self,
        *,
        user_id: str,
        room_id: str,
        surface: str,
        choice_id: str,
    ) -> dict[str, Any] | None:
        result = self._coll.update_one(
            {
                "_id": choice_id,
                "user_id": user_id,
                "room_id": room_id,
                "surface": surface,
                "interaction_mode": INTERACTION_BUBBLE,
                "status": "auto_cancelled",
            },
            {"$set": {
                "status": "superseded",
                "selected_choice": None,
                "resolved_at": time.time(),
                "resolution_reason": "replacement",
            }},
        )
        if getattr(result, "modified_count", 0) != 1:
            return None
        return self.choice_projection(
            user_id=user_id,
            room_id=room_id,
            surface=surface,
            choice_id=choice_id,
        )

    def cancel_choice(
        self,
        *,
        user_id: str,
        room_id: str,
        surface: str,
        choice_id: str,
    ) -> dict[str, Any] | None:
        result = self._coll.update_one(
            {
                "_id": choice_id,
                "user_id": user_id,
                "room_id": room_id,
                "surface": surface,
                "interaction_mode": INTERACTION_BUBBLE,
                "status": "pending",
            },
            {"$set": {
                "status": "cancelled",
                "selected_choice": "cancel",
                "resolved_at": time.time(),
                "resolution_reason": "button_cancel",
            }},
        )
        if getattr(result, "modified_count", 0) != 1:
            return self.choice_projection(
                user_id=user_id,
                room_id=room_id,
                surface=surface,
                choice_id=choice_id,
            )
        return self.choice_projection(
            user_id=user_id,
            room_id=room_id,
            surface=surface,
            choice_id=choice_id,
        )

    def cancel_legacy(self, *, user_id: str) -> int:
        result = self._coll.update_many(
            {
                "user_id": user_id,
                "interaction_mode": INTERACTION_LEGACY,
                "status": {"$in": ["prepared", "pending"]},
            },
            {"$set": {"status": "cancelled", "selected_choice": "cancel"}},
        )
        legacy_result = self._coll.update_many(
            {
                "user_id": user_id,
                "tool_name": {"$in": list(LEGACY_CARD_ACTIONS)},
                "status": {"$in": ["prepared", "pending"]},
            },
            {"$set": {"status": "cancelled", "selected_choice": "cancel"}},
        )
        return max(
            getattr(result, "modified_count", 0),
            getattr(legacy_result, "modified_count", 0),
        )

    def planner_projection(self, *, user_id: str) -> list[dict[str, Any]]:
        """Return bounded metadata only; never expose IDs, args, or payloads."""
        now = time.time()
        actives = self.list_active(user_id=user_id)
        if not actives:
            return []
        latest = max(actives, key=lambda rec: float(rec.get("created_at", 0) or 0))
        tool_name = str(latest.get("tool_name") or "")
        return [{
            "has_pending": True,
            "domain": tool_name.split(".", 1)[0] if "." in tool_name else "action",
            "expires_in_seconds": max(
                0, min(900, int(float(latest.get("expires_at", now) or now) - now)),
            ),
        }]

    def supersede_active(
        self, *, user_id: str, tool_name: str | None = None,
        reason: str = "replacement_attempt",
    ) -> dict[str, Any]:
        """CAS-supersede one non-executing confirmation.

        Confirmation and correction race on the same status predicate. If the
        confirmer wins the pending -> executing CAS, this method reports the
        observed state and never claims that an executing mutation was undone.
        """
        query: dict[str, Any] = {
            "user_id": user_id,
            # Include executing only to report the non-cancellable race
            # outcome; the CAS below still permits supersession solely from
            # prepared/pending states.
            "status": {"$in": ["prepared", "pending", "executing"]},
        }
        if tool_name:
            query["tool_name"] = tool_name
        try:
            candidates = list(self._coll.find(query))
        except Exception:
            return {"status": "storage_unavailable"}
        if not candidates:
            return {"status": "none"}
        candidates.sort(key=lambda rec: float(rec.get("created_at", 0) or 0), reverse=True)
        record = candidates[0]
        cid = record.get("_id")
        claim = self._coll.update_one(
            {
                "_id": cid,
                "user_id": user_id,
                "status": {"$in": ["prepared", "pending"]},
            },
            {"$set": {"status": "superseded", "superseded_at": time.time(), "superseded_reason": reason}},
        )
        if getattr(claim, "modified_count", 0) == 1:
            return {"status": "superseded", "confirmation_id": cid}
        try:
            latest = list(self._coll.find({"_id": cid, "user_id": user_id}))
        except Exception:
            latest = []
        current_status = str((latest[0] if latest else record).get("status") or "unknown")
        if current_status == "executing":
            return {"status": "already_executing", "confirmation_id": cid}
        if current_status in {"completed", "failed"}:
            return {"status": "already_completed", "confirmation_id": cid}
        return {"status": current_status, "confirmation_id": cid}

    def cancel_all(self, *, user_id: str) -> int:
        result = self._coll.update_many(
            {"user_id": user_id, "status": {"$in": ["prepared", "pending"]}},
            {"$set": {"status": "cancelled"}},
        )
        return getattr(result, "modified_count", 0)

    def execute_confirmed(
        self, *, user_id: str,
        executor: Callable[[str, dict[str, Any], str], Any],
        choice_id: str | None = None,
        room_id: str | None = None,
        surface: str | None = None,
        interaction_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        """Execute the one active, preview-bound confirmation for the user."""
        actives = self.list_active(
            user_id=user_id,
            choice_id=choice_id,
            room_id=room_id,
            surface=surface,
            interaction_mode=interaction_mode,
        )
        if not actives:
            return []
        actives.sort(key=lambda rec: rec.get("created_at", 0.0), reverse=True)
        rec = actives[0]
        cid = rec["_id"]
        tool_name = rec["tool_name"]
        arguments = rec["arguments"]
        payload = dict(rec.get("payload") or {})
        origin_run_id = str(rec.get("origin_run_id") or "")
        stored_fingerprint = str(rec.get("request_fingerprint") or "")
        preview_fingerprint = str(rec.get("preview_fingerprint") or "")
        presented_message_id = str(rec.get("presented_message_id") or "")
        presented_at = rec.get("presented_at")
        requires_persisted_message = (
            str(rec.get("interaction_mode") or INTERACTION_LEGACY)
            == INTERACTION_BUBBLE
        )
        expected_fingerprint = self._request_fingerprint(
            tool_name=tool_name,
            arguments=arguments,
            payload=payload,
            origin_run_id=origin_run_id,
        )
        if (
            not origin_run_id
            or not preview_fingerprint
            or (
                requires_persisted_message
                and (not presented_message_id or presented_at is None)
            )
            or not stored_fingerprint
            or stored_fingerprint != expected_fingerprint
        ):
            self._coll.update_one(
                {"_id": cid, "user_id": user_id, "status": "pending"},
                {"$set": {"status": "failed", "error_code": "confirmation_unbound"}},
            )
            return [{
                "confirmation_id": cid,
                "ok": False,
                "tool_name": tool_name,
                "error_code": "confirmation_unbound",
            }]
        # Atomically claim the confirmation (CAS pending -> executing).
        # If modified_count == 0 another worker already took it or it was
        # cancelled; skip to avoid double-execution under concurrency.
        claim = self._coll.update_one(
            {
                "_id": cid,
                "user_id": user_id,
                "status": "pending",
                "request_fingerprint": stored_fingerprint,
            },
            {"$set": {"status": "executing", "selected_choice": "confirm"}},
        )
        if getattr(claim, "modified_count", 0) == 0:
            return []
        results: list[dict[str, Any]] = []
        try:
            tool_result = executor(
                tool_name, arguments, user_id,
                {
                    **payload,
                    "_confirmation_id": cid,
                },
            )
        except Exception as exc:
            self._coll.update_one(
                {"_id": cid, "user_id": user_id, "status": "executing"},
                {"$set": {"status": "failed", "error_code": "executor_exception"}},
            )
            results.append({"confirmation_id": cid, "ok": False, "error_code": "executor_exception"})
            return results
        if isinstance(tool_result, tuple):
            ok = bool(tool_result[0])
            data = {"reply": str(tool_result[1] or "")} if len(tool_result) > 1 else {}
            error_code = tool_result[2] if len(tool_result) > 2 else None
        else:
            ok = bool(getattr(tool_result, "ok", False))
            data = getattr(tool_result, "data", {})
            error_code = getattr(tool_result, "error_code", None)
        new_status = "completed" if ok else "failed"
        self._coll.update_one(
            {
                "_id": cid,
                "user_id": user_id,
                "status": "executing",
                "request_fingerprint": stored_fingerprint,
            },
            {"$set": {
                "status": new_status,
                "result": data,
                "error_code": error_code,
                "selected_choice": "confirm",
                "resolved_at": time.time(),
            }},
        )
        results.append({
            "confirmation_id": cid,
            "ok": ok,
            "tool_name": tool_name,
            "data": data,
            "error_code": error_code,
        })
        return results
