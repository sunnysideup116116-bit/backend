"""Server-owned contact selection shared by public Pi surfaces."""

from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from typing import Any


TTL_SECONDS = 15 * 60
PAGE_SIZE = 3
PENDING_STATUSES = frozenset({"prepared", "pending"})


class ContactSelectionManager:
    """Bind opaque UI actions to accepted-contact candidates outside prompts."""

    def __init__(self, collection: Any) -> None:
        self._coll = collection

    def ensure_indexes(self) -> None:
        self._coll.create_index(
            [("user_id", 1), ("room_id", 1), ("status", 1), ("created_at", -1)],
            name="contact_selection_active_room",
        )
        self._coll.create_index("expires_at", expireAfterSeconds=0, sparse=True)

    def create(
        self, *, user_id: str, room_id: str, origin_run_id: str,
        name_hint: str, candidates: list[dict[str, Any]],
        source_engine: str = "pi", tool_protocol_version: str = "pi_public.v1",
    ) -> dict[str, Any]:
        if not candidates:
            raise ValueError("contact selection requires candidates")
        now = time.time()
        self._coll.update_many(
            {"user_id": user_id, "room_id": room_id, "status": {"$in": list(PENDING_STATUSES)}},
            {"$set": {"status": "superseded", "resolved_at": now}},
        )
        selection_id = uuid.uuid4().hex
        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in candidates[:20]:
            other_id = str(item.get("other_id") or "").strip()
            display_name = str(item.get("display_name") or "").strip()[:120]
            if not other_id or not display_name or other_id in seen_ids:
                continue
            seen_ids.add(other_id)
            rows.append({
                "action_id": uuid.uuid4().hex,
                "other_id": other_id,
                "display_name": display_name,
                "match_kind": str(item.get("kind") or "similar")[:24],
            })
        if not rows:
            raise ValueError("contact selection has no valid candidates")
        document = {
            "_id": selection_id,
            "user_id": user_id,
            "room_id": room_id,
            "surface": "public_ayue",
            "origin_run_id": str(origin_run_id or "")[:80],
            "source_engine": "pi",
            "tool_protocol_version": str(tool_protocol_version or "legacy")[:80],
            "name_hint": str(name_hint or "").strip()[:120],
            "candidates": rows,
            "page": 0,
            "status": "prepared",
            "selected_action_id": None,
            "expected_persisted_fingerprint": "",
            "presented_message_id": None,
            "presented_at": None,
            "created_at": now,
            "expires_at": now + TTL_SECONDS,
        }
        self._coll.insert_one(document)
        return copy.deepcopy(document)

    @staticmethod
    def _presentation_digest(content: str, blocks: list[dict[str, Any]]) -> str:
        source = json.dumps(
            {"content": str(content or ""), "interaction_blocks_v1": blocks},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    def bind_final_preview(
        self, *, user_id: str, room_id: str, origin_run_id: str,
        final_content: str, interaction_blocks_v1: list[dict[str, Any]],
    ) -> bool:
        if not str(final_content or "").strip() or not interaction_blocks_v1:
            return False
        digest = self._presentation_digest(final_content, interaction_blocks_v1)
        result = self._coll.update_one(
            {"user_id": user_id, "room_id": room_id, "origin_run_id": origin_run_id, "status": "prepared"},
            {"$set": {"expected_persisted_fingerprint": digest}},
        )
        return getattr(result, "modified_count", 0) == 1

    def mark_presented(
        self, *, user_id: str, origin_run_id: str, message_id: str,
        persisted_content: str, interaction_blocks_v1: list[dict[str, Any]],
    ) -> bool:
        if not str(message_id or "").strip() or not str(persisted_content or "").strip():
            return False
        digest = self._presentation_digest(persisted_content, interaction_blocks_v1)
        result = self._coll.update_one(
            {
                "user_id": user_id, "origin_run_id": origin_run_id,
                "status": "prepared", "expected_persisted_fingerprint": digest,
            },
            {"$set": {
                "status": "pending", "presented_message_id": str(message_id),
                "presented_at": time.time(),
            }},
        )
        return getattr(result, "modified_count", 0) == 1

    def _rows(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            return [dict(item) for item in self._coll.find(query)]
        except Exception:
            return []

    def get(self, selection_id: str, *, user_id: str, room_id: str) -> dict[str, Any] | None:
        rows = self._rows({"_id": selection_id, "user_id": user_id, "room_id": room_id})
        return rows[0] if rows else None

    def selection_for_run(
        self, *, user_id: str, room_id: str, origin_run_id: str,
    ) -> dict[str, Any] | None:
        rows = self._rows({
            "user_id": user_id, "room_id": room_id,
            "origin_run_id": origin_run_id, "status": {"$in": list(PENDING_STATUSES)},
        })
        rows.sort(key=lambda row: float(row.get("created_at", 0) or 0), reverse=True)
        return rows[0] if rows else None

    def supersede_active(self, *, user_id: str, room_id: str) -> None:
        self._coll.update_many(
            {"user_id": user_id, "room_id": room_id, "status": {"$in": list(PENDING_STATUSES)}},
            {"$set": {"status": "superseded", "resolved_at": time.time()}},
        )

    def _expire(self, record: dict[str, Any]) -> dict[str, Any]:
        if (
            record.get("status") in PENDING_STATUSES
            and float(record.get("expires_at", 0) or 0) <= time.time()
        ):
            self._coll.update_one(
                {"_id": record.get("_id"), "status": {"$in": list(PENDING_STATUSES)}},
                {"$set": {"status": "expired", "resolved_at": time.time()}},
            )
            return {**record, "status": "expired"}
        return record

    def resolve(
        self, *, user_id: str, room_id: str, action_id: str, action: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Return the updated record and selected private contact id, if any."""
        rows = self._rows({"user_id": user_id, "room_id": room_id})
        record = next(
            (
                row for row in rows
                if str(row.get("_id") or "") == action_id
                or any(str(item.get("action_id") or "") == action_id for item in row.get("candidates") or [])
            ),
            None,
        )
        if record is None:
            return None, None
        record = self._expire(record)
        if record.get("status") in PENDING_STATUSES and record.get("source_engine") != "pi":
            self._coll.update_one(
                {"_id": record.get("_id"), "status": {"$in": list(PENDING_STATUSES)}},
                {"$set": {
                    "status": "expired",
                    "resolved_at": time.time(),
                    "resolution_reason": "dag_retired",
                }},
            )
            return {**record, "status": "expired", "resolution_reason": "dag_retired"}, None
        if (
            record.get("status") != "pending"
            or (
                record.get("source_engine") == "pi"
                and not record.get("presented_message_id")
            )
        ):
            return record, None
        selection_id = str(record.get("_id") or "")
        if action == "more" and action_id == selection_id:
            total_pages = max(1, (len(record.get("candidates") or []) + PAGE_SIZE - 1) // PAGE_SIZE)
            next_page = min(int(record.get("page", 0) or 0) + 1, total_pages - 1)
            updated = self._coll.update_one(
                {"_id": selection_id, "status": "pending"},
                {"$set": {"page": next_page}},
            )
            if getattr(updated, "modified_count", 0) != 1:
                return self.get(selection_id, user_id=user_id, room_id=room_id), None
            return {**record, "page": next_page}, None
        if action == "cancel" and action_id == selection_id:
            updated = self._coll.update_one(
                {"_id": selection_id, "status": "pending"},
                {"$set": {"status": "none_selected", "resolved_at": time.time()}},
            )
            if getattr(updated, "modified_count", 0) != 1:
                return self.get(selection_id, user_id=user_id, room_id=room_id), None
            return {**record, "status": "none_selected"}, None
        if action != "confirm":
            return record, None
        candidate = next(
            (item for item in record.get("candidates") or [] if str(item.get("action_id") or "") == action_id),
            None,
        )
        if candidate is None:
            return record, None
        updated = self._coll.update_one(
            {"_id": selection_id, "status": "pending"},
            {"$set": {
                "status": "selected", "selected_action_id": action_id,
                "resolved_at": time.time(),
            }},
        )
        if getattr(updated, "modified_count", 0) != 1:
            return self.get(selection_id, user_id=user_id, room_id=room_id), None
        return {**record, "status": "selected", "selected_action_id": action_id}, str(candidate.get("other_id") or "")


def public_contact_selection(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    candidates = list(record.get("candidates") or [])
    page = max(0, int(record.get("page", 0) or 0))
    start = page * PAGE_SIZE
    visible = candidates[start:start + PAGE_SIZE]
    selected_action = str(record.get("selected_action_id") or "")
    return {
        "id": str(record.get("_id") or ""),
        "state": "pending" if record.get("status") == "prepared" else str(record.get("status") or "failed"),
        "expires_at": float(record.get("expires_at", 0) or 0),
        "name_hint": str(record.get("name_hint") or "")[:120],
        "page": page,
        "has_more": start + PAGE_SIZE < len(candidates),
        "candidates": [
            {
                "action_id": str(item.get("action_id") or ""),
                "display_name": str(item.get("display_name") or "")[:120],
                "selected": str(item.get("action_id") or "") == selected_action,
            }
            for item in visible
            if str(item.get("action_id") or "") and str(item.get("display_name") or "")
        ],
    }


def contact_selection_block(record: dict[str, Any] | None) -> dict[str, Any] | None:
    projection = public_contact_selection(record)
    return {"type": "contact_selection", "selection": projection} if projection else None


def project_contact_selection_history(
    messages: list[dict[str, Any]], *, user_id: str, room_id: str, collection: Any,
) -> list[dict[str, Any]]:
    manager = ContactSelectionManager(collection)
    output: list[dict[str, Any]] = []
    for message in messages:
        metadata = message.get("metadata") if isinstance(message, dict) else None
        blocks = metadata.get("interaction_blocks_v1") if isinstance(metadata, dict) else None
        if not isinstance(blocks, list):
            output.append(message)
            continue
        projected: list[dict[str, Any]] = []
        changed = False
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "contact_selection":
                projected.append(block)
                continue
            selection = block.get("selection") or {}
            record = manager.get(str(selection.get("id") or ""), user_id=user_id, room_id=room_id)
            refreshed = contact_selection_block(manager._expire(record) if record else None)
            projected.append(refreshed or block)
            changed = changed or refreshed is not None
        output.append(
            {**message, "metadata": {**metadata, "interaction_blocks_v1": projected}}
            if changed else message
        )
    return output
