"""Account-gated Pi loop with Pi-owned Calendar tools.

Pi owns intent and tool iteration.  Python owns model credentials, bounded
context, guards, confirmations, and writes.  No Planner or Synthesizer is
called from this module.
"""
from __future__ import annotations

import json
import os
import queue
from dataclasses import dataclass
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable

from services.ai_service import generate_chat_completion_with_tools
from services.ayue_agent.pi.settings import PI_ROOT, pi_available
from services.ayue_agent.contracts import AgentResult
from services.ayue_agent.shared.confirmation_layout import confirmation_layout
from services.ayue_agent.shared.model_context_text import strip_model_time_annotations
from services.ayue_agent.shared.interaction_history import strip_historical_card_copy
from .prompts import POLICY, PRESENTATION_POLICY
from .registry import PI_TOOL_NAMES, tool_schemas as registry_tool_schemas
from .reply import failed_reply, pending_preview, pending_reply, validate_pi_reply_result
from .tool_runtime import PiToolRuntime


TOOLS = PI_TOOL_NAMES
INITIAL_TOOLS = PI_TOOL_NAMES


def initial_tools_for_message(message: str) -> frozenset[str]:
    """Compatibility seam: language never changes the authorized tool set."""
    del message
    return INITIAL_TOOLS


@dataclass
class PiMetrics:
    llm_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class _ValidatedReplyStream:
    """Publish only complete provider sentences that pass the public boundary.

    Tool-call arguments and incomplete prose remain buffered. The authoritative
    final event can still correct a previously safe prefix when final validation
    repairs a later sentence.
    """

    def __init__(
        self,
        on_token: Callable[[str], None],
        observations: list[dict],
    ) -> None:
        self._on_token = on_token
        self._observations = observations
        self._raw = ""
        self._published = ""
        self._blocked = False

    @property
    def published(self) -> str:
        return self._published

    def push(self, fragment: str) -> None:
        if self._blocked or not fragment:
            return
        self._raw += str(fragment)
        boundaries = list(re.finditer(r"[\u3002\uff01\uff1f!?]|\n", self._raw))
        if not boundaries:
            return
        self._publish(self._raw[:boundaries[-1].end()])

    def accept(self, text: str) -> bool:
        """Flush the validated remainder when it still extends the live prefix."""
        if self._blocked:
            return False
        canonical = str(text or "")
        if self._published and not canonical.startswith(self._published):
            self._blocked = True
            return False
        delta = canonical[len(self._published):]
        if delta:
            self._on_token(delta)
            self._published = canonical
        return True

    def reject(self) -> None:
        self._blocked = True

    def _publish(self, raw_prefix: str) -> None:
        validation = validate_pi_reply_result(raw_prefix, self._observations)
        canonical = validation.reply
        if not canonical:
            self._blocked = True
            return
        if self._published and not canonical.startswith(self._published):
            self._blocked = True
            return
        delta = canonical[len(self._published):]
        if delta:
            self._on_token(delta)
            self._published = canonical


def pi_context(turn: Any) -> dict:
    historical_interactions: list[dict[str, Any]] = []
    recent_messages = [
        {
            "role": str(item.get("role") or ""),
            "content": (
                strip_historical_card_copy(strip_model_time_annotations(item.get("content")))
                if item.get("role") == "assistant"
                else str(item.get("content") or "").strip()
            ),
            "sent_at": str(item.get("sent_at") or "unknown")[:40],
            "timezone": str(item.get("timezone") or turn.clock.timezone)[:64],
            "truncated": bool(item.get("truncated")),
            "sources": [
                {
                    "title": str(source.get("title") or "公開來源")[:160],
                    "url": str(source.get("url") or "")[:1000],
                }
                for source in (item.get("sources") or [])[:5]
                if isinstance(source, dict) and str(source.get("url") or "").strip()
            ],
        }
        for item in (turn.recent_messages or [])
        if isinstance(item, dict)
        and str(item.get("role") or "") in {"user", "assistant"}
        and str(item.get("content") or "").strip()
    ]
    recent_messages = [item for item in recent_messages if item["content"]]
    for index, item in enumerate(turn.recent_messages or []):
        if not isinstance(item, dict) or not isinstance(item.get("historical_interaction"), dict):
            continue
        interaction = dict(item["historical_interaction"])
        interaction["message_index"] = index
        historical_interactions.append(interaction)
    if (
        recent_messages
        and recent_messages[-1]["role"] == "user"
        and recent_messages[-1]["content"] == str(turn.message or "").strip()
    ):
        recent_messages.pop()
    result = {
        "message": str(turn.message or "")[:1200],
        "recent_messages": recent_messages,
        "historical_interactions": historical_interactions[-8:],
        "history_projection_status": getattr(turn, "history_projection_status", "complete"),
        "conversation_continuity": (
            turn.conversation_continuity.model_dump()
            if getattr(turn, "conversation_continuity", None) is not None
            and hasattr(turn.conversation_continuity, "model_dump")
            else getattr(turn, "conversation_continuity", None)
        ),
        "clock": turn.clock.model_dump(),
        "calendar_recent_mutation": getattr(turn, "calendar_recent_mutation", None),
        "cancelled_confirmation": getattr(turn, "_cancelled_confirmation_context", None),
        "mentioned_contacts": getattr(turn, "mentioned_contacts", None),
        "mentioned_contact_overflow": getattr(turn, "mentioned_contact_overflow", False),
        "date_coordination_summary": getattr(turn, "date_coordination_summary", None),
        "operation_batch": getattr(turn, "_operation_batch_context", None),
    }
    return {key: value for key, value in result.items() if value is not None}


def tool_schemas() -> list[dict]:
    return registry_tool_schemas()


def _schema_field_paths(schema: dict[str, Any]) -> set[str]:
    output: set[str] = set()

    def visit(node: Any, prefix: str = "") -> None:
        if not isinstance(node, dict):
            return
        for key, child in (node.get("properties") or {}).items():
            path = f"{prefix}.{key}" if prefix else str(key)
            output.add(path)
            visit(child, path)
        items = node.get("items")
        if isinstance(items, dict):
            visit(items, f"{prefix}[]")
        for branch in (node.get("anyOf") or node.get("oneOf") or []):
            visit(branch, prefix)

    visit(schema)
    return output


def _argument_fields(
    value: Any, prefix: str = "", *, limit: int = 24,
    allowed_paths: set[str] | None = None,
) -> list[str]:
    fields: list[str] = []

    def visit(item: Any, path: str) -> None:
        if len(fields) >= limit:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                next_path = f"{path}.{key}" if path else str(key)
                fields.append(next_path[:120] if allowed_paths is None or next_path in allowed_paths else "<unknown>")
                visit(child, next_path)
        elif isinstance(item, list) and item:
            visit(item[0], f"{path}[]")

    visit(value, prefix)
    return fields[:limit]


def run_bridge(
    initial: dict,
    model_call: Callable,
    tool_call: Callable,
    *,
    final_validate: Callable[[str, bool], dict[str, Any]] | None = None,
    timeout: float = 90,
    max_model_calls: int = 6,
    max_tool_calls: int = 8,
) -> dict:
    if not pi_available():
        raise RuntimeError("pi_runtime_unavailable")
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        [shutil.which("node"), str(PI_ROOT / "bridge.mjs")],
        cwd=PI_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", bufsize=1,
        env={"PATH": os.defpath, "LANG": "C.UTF-8", "NODE_NO_WARNINGS": "1"},
    )
    incoming: queue.Queue = queue.Queue(maxsize=16)

    def read_output():
        try:
            for line in iter(lambda: process.stdout.readline(1_000_001), ""):
                if len(line) > 1_000_000:
                    break
                incoming.put(line, timeout=1)
        except Exception:
            pass
        finally:
            try:
                incoming.put(None, timeout=1)
            except queue.Full:
                pass

    reader = threading.Thread(target=read_output, name="ayue-pi-pipe", daemon=True)
    reader.start()

    def send(payload):
        process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        process.stdin.flush()

    try:
        send({"type": "start", **initial})
        model_count = 0
        tool_count = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("pi_deadline_exceeded")
            try:
                line = incoming.get(timeout=remaining)
            except queue.Empty as exc:
                raise RuntimeError("pi_deadline_exceeded") from exc
            if line is None:
                raise RuntimeError("pi_process_exited")
            data = json.loads(line)
            kind = data.get("type")
            if kind == "done":
                return data
            if kind == "model":
                model_count += 1
                if model_count > max_model_calls:
                    raise RuntimeError("pi_model_budget_exhausted")
                result = model_call(
                    data.get("messages") or [], deadline,
                    [str(name) for name in (data.get("toolNames") or [])],
                )
            elif kind == "tool":
                tool_count += 1
                if tool_count > max_tool_calls:
                    raise RuntimeError("pi_tool_budget_exhausted")
                result = tool_call(str(data.get("name") or ""), data.get("arguments") or {})
            elif kind == "validate_final" and final_validate is not None:
                result = final_validate(
                    str(data.get("text") or ""),
                    bool(data.get("repairAttempted")),
                )
            else:
                raise RuntimeError("pi_protocol_invalid")
            send({"type": "response", "id": data.get("id"), "result": result})
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        process.stdin.close()
        reader.join(timeout=2)
        process.stdout.close()


def _presentation(preview: str, *, deadline: float) -> tuple[list[str], list[dict], dict[str, int]]:
    started = time.perf_counter()
    completion = generate_chat_completion_with_tools(
        "", [],
        system_prompt=PRESENTATION_POLICY,
        temperature=0,
        max_tokens=500,
        deadline_monotonic=deadline,
        model_owner="pi",
        on_token=lambda _fragment: None,
        conversation_messages=[{
            "role": "user",
            "content": json.dumps({"verified_pending_preview": preview}, ensure_ascii=False),
        }],
    )
    model_text = "" if completion.tool_calls else completion.content
    layout = confirmation_layout(model_text, fallback_preview=preview)
    return layout.messages, layout.interaction_blocks_v1, {
        "called": 1,
        "input_tokens": completion.input_tokens,
        "output_tokens": completion.output_tokens,
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "fallback": int(layout.used_fallback),
    }


def _public_sources(observations: list[dict]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in observations:
        if item.get("status") != "ok" or item.get("tool") not in {"web.search", "web.extract"}:
            continue
        result = item.get("result") or {}
        candidates = list(result.get("results") or result.get("sources") or [])
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            url = str(candidate.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            output.append({
                "title": str(candidate.get("title") or candidate.get("name") or "公開來源")[:160],
                "url": url,
            })
            if len(output) >= 5:
                return output
    return output


def _tool_recovery_prompt(name: str, observations: list[dict]) -> str:
    if not name.startswith("places."):
        return ""
    place_failures = [
        str(item.get("error_code") or "")
        for item in observations
        if isinstance(item, dict)
        and str(item.get("tool") or "").startswith("places.")
        and item.get("status") == "failed"
    ]
    if (
        place_failures.count("location_not_found") < 2
        and "public_read_budget_exhausted" not in place_failures
    ):
        return ""
    return (
        "地點工具已連續無法解析後續搜尋中心。請停止呼叫工具，使用本回合已成功的地點結果自然回答；"
        "清楚區分已完成與未完成部分，不得把第一階段候選冒充成完整結果，也不得宣稱已執行任何變更。"
    )


def run_pi_turn(
    turn: Any,
    *,
    run_id: str,
    trace: dict,
    confirmation_collection: Any,
    contact_selection_collection: Any,
    operation_batch_collection: Any,
    on_progress=None,
    on_token: Callable[[str], None] | None = None,
    debug_enabled=False,
) -> AgentResult:
    del debug_enabled
    request_started = time.monotonic()
    budget_class = "standard"
    model_limit = 6
    tool_limit = 8
    turn_deadline = request_started + 90.0
    hard_deadline = request_started + 120.0
    hard_model_limit = 10
    hard_tool_limit = 16
    tool_dispatch_count = 0
    terminal = False
    used_calendar = False
    forced_failure: str | None = None
    python_schema_failures: dict[str, int] = {}
    metrics = PiMetrics()
    schemas = tool_schemas()
    initial_tool_names = initial_tools_for_message(turn.message)
    schema_fields = {
        schema["name"]: _schema_field_paths(schema.get("parameters") or {})
        for schema in schemas
    }
    failure = None
    tool_runtime = PiToolRuntime(
        turn,
        run_id=run_id,
        trace=trace,
        confirmation_collection=confirmation_collection,
        contact_selection_collection=contact_selection_collection,
        operation_batch_collection=operation_batch_collection,
        on_progress=on_progress,
    )
    observations = tool_runtime.observations
    trace["agent_runtime"] = "pi"
    trace["execution_mode"] = "pi"
    trace["reply_owner"] = "pi"
    trace["pi_calendar_protocol"] = "pi_calendar.v1"
    trace["pi_budget_class"] = budget_class
    trace["pi_model_budget"] = model_limit
    trace["pi_tool_budget"] = tool_limit
    active_reply_stream: _ValidatedReplyStream | None = None
    live_stream_abandoned = False

    def model_call(messages, deadline, active_tool_names=None):
        nonlocal active_reply_stream, live_stream_abandoned
        metrics.llm_call_count += 1
        if metrics.llm_call_count > model_limit - 1 or time.monotonic() >= turn_deadline:
            return {"error": "pi_model_budget_exhausted"}
        allowed = set(initial_tool_names if active_tool_names is None else active_tool_names)
        active_schemas = [schema for schema in schemas if schema["name"] in allowed]
        started = time.perf_counter()
        diagnostic = {"stage": "decision", "call": metrics.llm_call_count, "tool_count": len(active_schemas)}
        trace.setdefault("pi_model_diagnostics", []).append(diagnostic)
        reply_stream = (
            _ValidatedReplyStream(on_token, observations)
            if on_token is not None and not live_stream_abandoned
            else None
        )
        active_reply_stream = reply_stream
        try:
            completion = generate_chat_completion_with_tools(
                "", [{"type": "function", "function": schema} for schema in active_schemas],
                system_prompt=POLICY, temperature=0, max_tokens=4096,
                deadline_monotonic=min(deadline, turn_deadline), model_owner="pi",
                # Keep the provider in streaming mode for observable TTFT and
                # publish only sentence prefixes accepted by the public reply
                # boundary. Incomplete or unsafe text never crosses this seam.
                on_token=(reply_stream.push if reply_stream else lambda _fragment: None),
                conversation_messages=provider_messages(messages),
            )
        except Exception as exc:
            code = "pi_provider_timeout" if "timeout" in type(exc).__name__.lower() or isinstance(exc, TimeoutError) else "pi_provider_error"
            diagnostic.update(code=code, error_type=type(exc).__name__, duration_ms=round((time.perf_counter() - started) * 1000))
            if reply_stream and reply_stream.published:
                live_stream_abandoned = True
            return {"error": code}
        if completion.tool_calls:
            if reply_stream:
                if reply_stream.published:
                    live_stream_abandoned = True
                reply_stream.reject()
            active_reply_stream = None
        diagnostic.update(code=None, duration_ms=round((time.perf_counter() - started) * 1000))
        metrics.input_tokens += completion.input_tokens
        metrics.output_tokens += completion.output_tokens
        return {"content": completion.content, "tool_calls": completion.tool_calls,
                "input_tokens": completion.input_tokens, "output_tokens": completion.output_tokens}

    def tool_call(name, arguments):
        nonlocal terminal, used_calendar, forced_failure
        nonlocal budget_class, model_limit, tool_limit, turn_deadline, tool_dispatch_count
        started = time.perf_counter()
        tool_dispatch_count += 1
        if budget_class == "standard" and (
            name.startswith("web.") or name == "workflow.queue_operations"
        ):
            budget_class = "complex"
            model_limit = hard_model_limit
            tool_limit = hard_tool_limit
            turn_deadline = hard_deadline
            trace["pi_budget_upgraded_by"] = "research" if name.startswith("web.") else "workflow"
        if tool_dispatch_count > tool_limit or time.monotonic() >= turn_deadline:
            forced_failure = "pi_tool_budget_exhausted"
            return {"observations": [{"status": "failed", "code": forced_failure}], "stop": True}
        diagnostic = {
            "tool": name[:100],
            "stage": "python_dispatch",
            "argument_fields": _argument_fields(
                arguments,
                allowed_paths=schema_fields.get(name, set()),
            ),
        }
        trace.setdefault("pi_tool_diagnostics", []).append(diagnostic)
        if terminal:
            diagnostic["outcome"] = "stopped"
            diagnostic["code"] = "confirmation_pending"
            return {"observations": [{"status": "stopped", "code": "confirmation_pending"}], "stop": True}
        if name not in TOOLS:
            diagnostic["outcome"] = "failed"
            diagnostic["code"] = "tool_not_allowed"
            return {"observations": [{"status": "failed", "code": "tool_not_allowed"}]}
        domain = name.split(".")[0]
        used_calendar = used_calendar or domain == "calendar"
        try:
            projected = [tool_runtime.dispatch(name, arguments)]
        except Exception:
            count = python_schema_failures.get(name, 0) + 1
            python_schema_failures[name] = count
            diagnostic["outcome"] = "failed"
            diagnostic["code"] = "tool_schema_invalid"
            diagnostic["attempt"] = count
            if count >= 2:
                forced_failure = "pi_tool_schema_invalid"
            return {
                "observations": [{
                    "status": "failed",
                    "code": "tool_schema_invalid",
                    "hint": "依工具 schema 修正參數；這不是使用者缺少欄位。",
                }],
                "stop": count >= 2,
            }
        for item in projected:
            observation = item.get("result") or {}
            if observation.get("pending_confirmation") or observation.get("contact_selection"):
                terminal = True
        schema_error = next(
            (
                str(item.get("error_code") or "")
                for item in projected
                if str(item.get("error_code") or "")
                in {"tool_schema_invalid", "executor_args_invalid", "schema_invalid"}
            ),
            "",
        )
        if schema_error:
            count = python_schema_failures.get(name, 0) + 1
            python_schema_failures[name] = count
            diagnostic["attempt"] = count
            if count >= 2:
                forced_failure = "pi_tool_schema_invalid"
        diagnostic["duration_ms"] = round((time.perf_counter() - started) * 1000)
        diagnostic["outcome"] = "ok" if any(item["status"] == "ok" for item in projected) else "failed"
        diagnostic["code"] = next((item["error_code"] for item in projected if item["error_code"]), None)
        trace.setdefault("pi_steps", []).append({
            "tool": name,
            "outcomes": [{"status": item["status"], "error_code": item["error_code"]} for item in projected],
        })
        response = {
            "observations": projected,
            "stop": terminal or bool(schema_error and python_schema_failures[name] >= 2),
        }
        recovery_prompt = _tool_recovery_prompt(name, observations)
        if recovery_prompt and not terminal:
            response["disableTools"] = True
            response["recoveryPrompt"] = recovery_prompt
        if budget_class == "complex":
            response["upgradeBudget"] = True
        return response

    def final_validate(text: str, repair_attempted: bool) -> dict[str, Any]:
        nonlocal active_reply_stream, live_stream_abandoned
        validation = validate_pi_reply_result(text, observations)
        if validation.reply:
            if active_reply_stream is not None and not live_stream_abandoned:
                if not active_reply_stream.accept(validation.reply):
                    live_stream_abandoned = True
            return {"accept": True, "text": validation.reply}
        if active_reply_stream is not None:
            if active_reply_stream.published:
                live_stream_abandoned = True
            active_reply_stream.reject()
        code = validation.code or "pi_reply_invalid"
        trace.setdefault("pi_reply_validation", []).append({
            "code": code,
            "stage": validation.stage,
            "public_reason": validation.public_reason,
            "repair_attempted": bool(repair_attempted),
        })
        if validation.repairable and not repair_attempted:
            return {
                "accept": False,
                "disableTools": True,
                "repairPrompt": (
                    f"上一個答案未通過發布驗證（原因：{code}）。"
                    "只能使用對話中既有且已驗證的工具結果重寫自然語言答案；這一輪不能呼叫任何工具。"
                    "需要操作但尚未準備時，只說明仍需重新處理，不得輸出 [[confirmation]]、仿卡、URL、內部識別碼或完成宣稱。"
                ),
            }
        return {"accept": False, "error": code}

    try:
        context = pi_context(turn)
        history = []
        for item in context.pop("recent_messages", []):
            history_text = item["content"]
            if item.get("sources"):
                history_text += "\n\n[此則已發布訊息的來源 metadata]\n" + json.dumps(
                    item["sources"], ensure_ascii=False,
                )
            history_item = {
                "role": item["role"],
                "content": [{"type": "text", "text": history_text}],
                "timestamp": 0,
            }
            if item.get("sent_at") not in {None, "", "unknown"}:
                history_item["messageTime"] = {
                    "sent_at": item["sent_at"],
                    "timezone": item.get("timezone", "Asia/Taipei"),
                }
            history.append(history_item)
        bridge_result = run_bridge(
            {"systemPrompt": POLICY, "prompt": json.dumps(context, ensure_ascii=False), "history": history,
            "tools": schemas, "initialToolNames": sorted(initial_tool_names),
             "maxRounds": 5, "maxToolCalls": 8,
             "validateFinal": True}, model_call, tool_call,
            final_validate=final_validate,
            timeout=max(1.0, hard_deadline - time.monotonic()),
            max_model_calls=hard_model_limit - 1,
            max_tool_calls=hard_tool_limit,
        )
        failure = forced_failure or bridge_result.get("error")
        node_diagnostics = [
            {
                "tool": str(item.get("tool") or "")[:100],
                "stage": "node_validation",
                "code": str(item.get("code") or "")[:80],
                "attempt": int(item.get("attempt") or 0),
                "argument_fields": [str(field)[:120] for field in (item.get("argumentFields") or [])[:24]],
            }
            for item in (bridge_result.get("toolFailures") or [])[:4]
            if isinstance(item, dict)
        ]
        if node_diagnostics:
            trace.setdefault("pi_tool_diagnostics", []).extend(node_diagnostics)
        if bridge_result.get("budget_exhausted") and not terminal:
            failure = failure or "pi_step_limit"
        trace["pi_protocol_repair_attempted"] = bool(bridge_result.get("repair_attempted"))
    except Exception as exc:
        trace["pi_failure_type"] = type(exc).__name__
        print(f"[PI_RUNTIME] failure={type(exc).__name__}")
        failure = "pi_deadline_exceeded" if str(exc) == "pi_deadline_exceeded" else "pi_runtime_failed"
        bridge_result = {}

    presentation_metrics: dict[str, int] | None = None
    presentation_attempted = False
    messages: list[str] = []
    interaction_blocks: list[dict] = []
    preview = pending_preview(observations) if terminal else None
    if preview:
        failure = None
        trace["reply_owner"] = "pi_presentation"
        try:
            if turn_deadline - time.monotonic() <= 1.0:
                raise TimeoutError("presentation_budget_exhausted")
            presentation_attempted = True
            messages, interaction_blocks, presentation_metrics = _presentation(
                preview,
                deadline=turn_deadline,
            )
        except Exception as exc:
            trace["pi_presentation_failure"] = type(exc).__name__
            layout = confirmation_layout("", fallback_preview=preview)
            messages = layout.messages
            interaction_blocks = layout.interaction_blocks_v1
            presentation_metrics = {
                "called": int(presentation_attempted),
                "input_tokens": 0,
                "output_tokens": 0,
                "duration_ms": 0,
                "fallback": 1,
            }
        reply = "\n\n".join(messages)
    elif terminal:
        reply = pending_reply(observations) or "請先完成畫面上的選擇，這次還沒有執行變更。"
        messages = [reply]
    elif failure:
        reply = failed_reply(failure, observations=observations, has_pending_interaction=terminal)
        messages = [reply]
    else:
        last_validation = validate_pi_reply_result(
            str(bridge_result.get("finalText") or ""), observations,
        )
        reply = last_validation.reply
        if not reply:
            failure = last_validation.code or "pi_reply_invalid"
            trace.setdefault("pi_reply_validation", []).append({
                "code": failure,
                "stage": last_validation.stage,
                "public_reason": last_validation.public_reason,
                "repair_attempted": bool(bridge_result.get("repair_attempted")),
            })
            reply = failed_reply(failure, observations=observations)
        messages = [reply]

    trace["pi_decision_llm_calls"] = metrics.llm_call_count
    trace["pi_budget_class"] = budget_class
    trace["pi_model_budget"] = model_limit
    trace["pi_tool_budget"] = tool_limit
    presentation_calls = int((presentation_metrics or {}).get("called", 0))
    trace["pi_presentation_llm_calls"] = presentation_calls
    trace["llm_call_count"] = metrics.llm_call_count + presentation_calls
    llm_metrics = [{
        "owner": "pi_decision",
        "llm_call_count": metrics.llm_call_count,
        "input_tokens": metrics.input_tokens,
        "output_tokens": metrics.output_tokens,
    }]
    if presentation_metrics:
        llm_metrics.append({
            "owner": "pi_presentation",
            "llm_call_count": presentation_calls,
            **{key: value for key, value in presentation_metrics.items() if key != "called"},
        })
    return AgentResult(
        handled=True, reply=reply, messages=messages,
        interaction_blocks_v1=interaction_blocks,
        presentation_class="transaction" if terminal else "fallback" if failure else "conversation",
        agent_run_id=run_id, agent_mode="pi",
        conversation_intent="pi", fallback_reason=failure,
        profile_write_allowed=not tool_runtime.write_prepared,
        profile_write_reason=(
            "calendar_operation" if used_calendar
            else "confirmation" if tool_runtime.write_prepared
            else "casual"
        ),
        # A fallback is not claim-bound to a specific web observation. Do not
        # publish a potentially unrelated source card beside generic recovery
        # prose; successful model replies retain their verified web sources.
        sources=[] if failure else _public_sources(observations),
        place_presentation_required=False,
        llm_call_metrics=llm_metrics,
    )


def provider_messages(messages: list[dict]) -> list[dict]:
    result = []
    tool_names = {}
    for message in messages:
        role = message.get("role")
        content = message.get("content", [])
        text = content if isinstance(content, str) else "".join(
            str(item.get("text") or "") for item in content if item.get("type") == "text"
        )
        if role in {"user", "assistant"}:
            value = {"role": role, "content": text}
            calls = [item for item in content if isinstance(item, dict) and item.get("type") == "toolCall"] if isinstance(content, list) else []
            if calls:
                value["tool_calls"] = [{"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": call.get("arguments") or {},
                }} for call in calls]
                tool_names.update({call["id"]: call["name"] for call in calls})
            result.append(value)
        elif role == "toolResult":
            call_id = message.get("toolCallId")
            if call_id not in tool_names:
                raise ValueError("unbound_tool_result")
            result.append({"role": "tool", "tool_call_id": call_id,
                           "tool_name": tool_names[call_id], "content": text})
        else:
            raise ValueError("unexpected_pi_message_role")
    return result
