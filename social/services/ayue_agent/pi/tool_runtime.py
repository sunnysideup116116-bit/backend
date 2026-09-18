"""Pi-owned tool dispatch over shared deterministic domain services.

This module deliberately does not invoke a separate planner or task runner.
The Pi model owns intent; this boundary validates
schemas, injects server authority, executes reads, and prepares one confirmation.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

from services.ayue_agent.contracts import ToolCall
from services.ayue_agent.tool_registry import (
    ToolRisk,
    executor_arguments_for_turn,
    get_tool_spec,
    planner_arguments_allowed,
    tool_call_key,
)
from services.ayue_agent.tools import execute_tool
from services.ayue_agent.public_relationship_projection import (
    remember_relationship_reference,
)
from services.ayue_agent.shared.confirmation import (
    INTERACTION_BUBBLE,
    SURFACE_PUBLIC,
    ConfirmationManager,
)
from services.ayue_agent.shared.contact_selections import ContactSelectionManager
from services.ayue_agent.shared.operation_batches import OperationBatchManager
from services.ayue_agent.shared.guard import guard_proposal
from services.ayue_agent.shared.guard_contracts import GuardResultCode, ToolProposal
from services.ayue_agent.shared.write_actions import prepare_write_confirmation

MAX_DOMAIN_READS = 3
MAX_TOTAL_READS = 9


def _place_anchor_key(value: Any) -> str:
    return re.sub(r"[\s,，、·•()（）\-–—]+", "", str(value or "")).casefold()


class PiToolRuntime:
    def __init__(
        self,
        turn: Any,
        *,
        run_id: str,
        trace: dict[str, Any],
        confirmation_collection: Any,
        contact_selection_collection: Any,
        operation_batch_collection: Any,
        on_progress: Callable | None = None,
    ) -> None:
        self.turn = turn
        self.run_id = run_id
        self.trace = trace
        self.confirmations = ConfirmationManager(confirmation_collection)
        self.contact_selections = ContactSelectionManager(contact_selection_collection)
        self.operation_batches = OperationBatchManager(operation_batch_collection)
        self.on_progress = on_progress
        self.seen_keys: set[tuple[str, str]] = set()
        self.domain_reads: dict[str, int] = {}
        self.total_reads = 0
        self.write_prepared = False
        self.observations: list[dict[str, Any]] = []
        self.results: list[Any] = []
        self._lock = threading.Lock()

    def _progress(self, event_type: str, **payload: Any) -> None:
        self.trace.setdefault("event_sequence", []).append(event_type)
        if self.on_progress is None:
            return
        try:
            self.on_progress({"type": event_type, "agent_run_id": self.run_id, **payload})
        except Exception:
            pass

    def _project(self, *, name: str, status: str, result: dict[str, Any] | None = None,
                 error_code: str | None = None, skip_reason: str | None = None) -> dict[str, Any]:
        item = {
            "task_id": f"pi_{name.split('.')[0]}",
            "status": status,
            "tool": name,
            "result": result or {},
            "error_code": error_code,
            "skip_reason": skip_reason,
        }
        self.observations.append(item)
        return item

    def project(self, **kwargs: Any) -> dict[str, Any]:
        """Domain-facing result envelope."""
        return self._project(**kwargs)

    def _private_place_anchor(self, anchor: Any) -> dict[str, Any] | None:
        requested = _place_anchor_key(anchor)
        if not requested:
            return None
        matches: list[dict[str, Any]] = []
        for stored in self.results:
            private_data = stored.get("private_data") if isinstance(stored, dict) else None
            for candidate in (private_data or {}).get("place_anchor_candidates") or []:
                if not isinstance(candidate, dict):
                    continue
                name = str(candidate.get("name") or "").strip()
                address = str(candidate.get("address_summary") or "").strip()
                forms = {
                    _place_anchor_key(name),
                    _place_anchor_key(address),
                    _place_anchor_key(f"{name}{address}"),
                    _place_anchor_key(f"{address}{name}"),
                }
                if requested in forms:
                    matches.append(candidate)
        unique: dict[tuple[str, str, float, float], dict[str, Any]] = {}
        for item in matches:
            try:
                latitude = float(item["latitude"])
                longitude = float(item["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
                continue
            identity = (
                str(item.get("provider") or ""),
                str(item.get("place_id") or item.get("map_url") or ""),
                latitude,
                longitude,
            )
            unique[identity] = item
        if len(unique) != 1:
            return None
        candidate = next(iter(unique.values()))
        label = " ".join(
            value for value in (
                str(candidate.get("name") or "").strip(),
                str(candidate.get("address_summary") or "").strip(),
            ) if value
        )[:160]
        return {
            "requested_anchor": str(anchor or "").strip(),
            "label": label,
            "latitude": float(candidate["latitude"]),
            "longitude": float(candidate["longitude"]),
        }

    def _create_confirmation(self, *, agent_name: str, tool_name: str,
                             arguments: dict[str, Any], payload: dict[str, Any],
                             preview: str, idempotency_key: str | None = None) -> str:
        if self.write_prepared:
            raise ValueError("pi_write_budget_exhausted")
        self.write_prepared = True
        tagged_payload = {
            **payload,
            "source_engine": payload.get("source_engine") or "pi",
            "tool_protocol_version": payload.get("tool_protocol_version") or "pi_public.v1",
        }
        batch_id = str(getattr(self.turn, "_operation_batch_id", "") or "")
        item_id = str(getattr(self.turn, "_operation_item_id", "") or "")
        batch_revision = int(getattr(self.turn, "_operation_batch_revision", 0) or 0)
        if batch_id and item_id:
            tagged_payload["_operation_item_id"] = item_id
            idempotency_key = idempotency_key or (
                f"operation-batch:{batch_id}:{item_id}:{batch_revision}:{self.run_id}"
            )
        return self.confirmations.create_confirmation(
            user_id=self.turn.user_id,
            room_id=self.turn.room_id,
            surface=SURFACE_PUBLIC,
            agent_name=agent_name,
            tool_name=tool_name,
            arguments=arguments,
            payload=tagged_payload,
            origin_run_id=self.run_id,
            preview=preview,
            interaction_mode=INTERACTION_BUBBLE,
            idempotency_key=idempotency_key,
        )

    def create_confirmation(self, **kwargs: Any) -> str:
        return self._create_confirmation(**kwargs)

    def _read(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = get_tool_spec(name)
        if spec is None or spec.risk is not ToolRisk.READ:
            return self._project(name=name, status="failed", error_code="tool_not_allowed")
        if not planner_arguments_allowed(spec, arguments):
            return self._project(name=name, status="failed", error_code="tool_schema_invalid")
        domain = name.split(".", 1)[0]
        if self.domain_reads.get(domain, 0) >= MAX_DOMAIN_READS or self.total_reads >= MAX_TOTAL_READS:
            return self._project(name=name, status="failed", error_code="public_read_budget_exhausted")
        proposal = ToolProposal(tool_name=name, arguments=arguments)
        decision = guard_proposal(
            proposal,
            agent_name=domain,
            seen_keys=self.seen_keys,
            step_count=self.domain_reads.get(domain, 0),
            max_reads=MAX_DOMAIN_READS,
        )
        self.trace.setdefault("guard_results", []).append(decision.code.value)
        if not decision.ok:
            return self._project(
                name=name,
                status="failed",
                error_code=(
                    "duplicate_call" if decision.code is GuardResultCode.DUPLICATE_CALL
                    else decision.code.value
                ),
            )
        mentioned_ids = list(getattr(self.turn, "_mentioned_ids", []) or [])
        try:
            safe_args = executor_arguments_for_turn(spec, mentioned_ids, arguments)
        except Exception:
            return self._project(name=name, status="failed", error_code="executor_args_invalid")
        trusted_place_anchor = (
            self._private_place_anchor(safe_args.get("anchor"))
            if name == "places.search_nearby" else None
        )
        if name == "web.extract":
            from .web_tools import extraction_urls_allowed
            urls = [str(url) for url in (arguments.get("urls") or [])]
            if not extraction_urls_allowed(self.turn, self.observations, urls):
                return self._project(name=name, status="failed", error_code="web_extract_url_not_bound")
        key = tool_call_key(spec, safe_args)
        with self._lock:
            if key in self.seen_keys:
                return self._project(name=name, status="failed", error_code="duplicate_call")
            self.seen_keys.add(key)
            self.domain_reads[domain] = self.domain_reads.get(domain, 0) + 1
            self.total_reads += 1
        started = time.perf_counter()
        self._progress("tool_started", text=spec.progress_text, tool_name=name)
        raw_ctx = self.turn._raw_ctx
        previous_private_anchor = getattr(raw_ctx, "_pi_trusted_place_anchor", None)
        if trusted_place_anchor is not None:
            object.__setattr__(raw_ctx, "_pi_trusted_place_anchor", trusted_place_anchor)
        try:
            result = execute_tool(
                ToolCall(name=name, arguments=safe_args),
                raw_ctx,
                clock=self.turn.clock,
            )
        except Exception:
            self._progress("tool_finished", outcome="error", tool_name=name,
                           duration_ms=round((time.perf_counter() - started) * 1000))
            self.trace.setdefault("tool_results", []).append({"tool": name, "ok": False, "code": "tool_exception"})
            return self._project(name=name, status="failed", error_code="tool_exception")
        finally:
            object.__setattr__(raw_ctx, "_pi_trusted_place_anchor", previous_private_anchor)
        duration_ms = round((time.perf_counter() - started) * 1000)
        self._progress("tool_finished", outcome="ok" if result.ok else "error",
                       tool_name=name, duration_ms=duration_ms)
        self.trace.setdefault("tool_results", []).append({
            "tool": name,
            "ok": bool(result.ok),
            "code": result.error_code,
        })
        if result.ok and result.private_data:
            self.results.append({"tool": name, "private_data": dict(result.private_data)})
            self.results = self.results[-8:]
            relationship_ref = result.private_data.get("relationship_contact_reference")
            if isinstance(relationship_ref, dict):
                other_id = str(relationship_ref.get("other_id") or "")
                if other_id:
                    object.__setattr__(
                        raw_ctx,
                        "_pi_recent_relationship_contact_id",
                        other_id,
                    )
                    try:
                        remember_relationship_reference(
                            self.turn.user_id,
                            self.turn.room_id,
                            other_id,
                            safe_label=str(relationship_ref.get("safe_label") or "對方"),
                        )
                    except Exception:
                        pass
        return self._project(
            name=name,
            status="ok" if result.ok else "failed",
            result=result.data,
            error_code=result.error_code,
        )

    def read(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._read(name, arguments)

    def _write(self, name: str, arguments: dict[str, Any], *, prepare: Callable | None = None) -> dict[str, Any]:
        spec = get_tool_spec(name)
        if spec is None or spec.risk is not ToolRisk.WRITE or not planner_arguments_allowed(spec, arguments):
            return self._project(name=name, status="failed", error_code="tool_schema_invalid")
        decision = guard_proposal(
            ToolProposal(tool_name=name, arguments=arguments),
            agent_name=name.split(".", 1)[0],
            seen_keys=self.seen_keys,
            step_count=0,
            max_reads=MAX_DOMAIN_READS,
        )
        self.trace.setdefault("guard_results", []).append(decision.code.value)
        if decision.code is not GuardResultCode.WRITE_REQUIRES_CONFIRMATION:
            return self._project(name=name, status="failed", error_code="guard_rejected")
        payload, preview = (
            prepare() if prepare is not None
            else prepare_write_confirmation(name, arguments, self.turn._raw_ctx, self.turn)
        )
        if payload is None:
            return self._project(
                name=name,
                status="failed",
                result={"clarification": {"message": preview or "需要更多資訊。", "operation_prepared": False}},
                error_code="preflight_rejected",
            )
        if payload.get("action") == "relationship.select_date_invitation_contact":
            data = dict(payload.get("data") or {})
            try:
                selection = self.contact_selections.create(
                    user_id=self.turn.user_id,
                    room_id=self.turn.room_id,
                    origin_run_id=self.run_id,
                    name_hint=str(data.get("name_hint") or ""),
                    candidates=list(data.get("candidates") or []),
                    source_engine="pi",
                    tool_protocol_version="pi_public.v1",
                )
            except Exception:
                return self._project(name=name, status="failed", error_code="contact_selection_unavailable")
            return self._project(name=name, status="ok", result={
                "contact_selection": True,
                "selection_state": str(selection.get("status") or "pending"),
                "operation_prepared": False,
            })
        try:
            self._create_confirmation(
                agent_name=name.split(".", 1)[0],
                tool_name=name,
                arguments=dict(payload.get("arguments") or {}),
                payload=dict(payload.get("data") or {}),
                preview=str(preview or "請確認是否執行這次操作。"),
            )
        except Exception:
            return self._project(name=name, status="failed", error_code="confirmation_unavailable")
        return self._project(name=name, status="ok", result={
            "pending_confirmation": True,
            "tool_name": name,
            "preview": str(preview or "請確認是否執行這次操作。"),
        })

    def write(self, name: str, arguments: dict[str, Any], *, prepare: Callable | None = None) -> dict[str, Any]:
        # Only domain handlers may supply a preparation callback; never model data.
        return self._write(name, arguments, prepare=prepare)

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # Lazy import keeps the registry authoritative without a module cycle:
        # registry -> domain -> runtime protocol, runtime -> registry dispatch.
        from .registry import dispatch
        return dispatch(self, name, arguments)
