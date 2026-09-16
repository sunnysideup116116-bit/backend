"""Small Private Pi bridge client.

The Public runtime keeps its own client and is intentionally untouched by this
module.  Both clients speak the existing generic ``pi_agent/bridge.mjs`` pipe
protocol, while Private owns its own context and tool callbacks.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from typing import Any, Callable

from .settings import PI_ROOT, pi_available


def provider_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Pi Agent messages to the provider's native chat shape."""
    result: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}
    for message in messages:
        role = message.get("role")
        content = message.get("content", [])
        text = content if isinstance(content, str) else "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
        if role in {"user", "assistant"}:
            value: dict[str, Any] = {"role": role, "content": text}
            calls = (
                [
                    item for item in content
                    if isinstance(item, dict) and item.get("type") == "toolCall"
                ]
                if isinstance(content, list) else []
            )
            if calls:
                value["tool_calls"] = [
                    {
                        "id": str(call.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(call.get("name") or ""),
                            "arguments": call.get("arguments") or {},
                        },
                    }
                    for call in calls
                ]
                tool_names.update({
                    str(call.get("id") or ""): str(call.get("name") or "")
                    for call in calls
                })
            result.append(value)
        elif role == "toolResult":
            call_id = str(message.get("toolCallId") or "")
            if call_id not in tool_names:
                raise ValueError("unbound_private_tool_result")
            result.append({
                "role": "tool",
                "tool_call_id": call_id,
                "tool_name": tool_names[call_id],
                "content": text,
            })
        else:
            raise ValueError("unexpected_private_pi_message_role")
    return result


def run_bridge(
    initial: dict[str, Any],
    model_call: Callable,
    tool_call: Callable,
    *,
    final_validate: Callable[[str, bool], dict[str, Any]] | None = None,
    timeout: float = 90,
    max_model_calls: int = 6,
    max_tool_calls: int = 8,
) -> dict[str, Any]:
    """Run one Pi Agent process and service its model/tool requests."""
    if not pi_available():
        raise RuntimeError("pi_runtime_unavailable")
    node = shutil.which("node")
    if not node:
        raise RuntimeError("pi_node_unavailable")
    deadline = time.monotonic() + max(1.0, float(timeout))
    process = subprocess.Popen(
        [node, str(PI_ROOT / "bridge.mjs")],
        cwd=PI_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env={"PATH": os.defpath, "LANG": "C.UTF-8", "NODE_NO_WARNINGS": "1"},
    )
    incoming: queue.Queue[str | None] = queue.Queue(maxsize=32)

    def read_output() -> None:
        try:
            assert process.stdout is not None
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

    reader = threading.Thread(target=read_output, name="ayue-private-pi-pipe", daemon=True)
    reader.start()

    def send(payload: dict[str, Any]) -> None:
        if process.stdin is None:
            raise RuntimeError("private_pi_stdin_closed")
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
                    data.get("messages") or [],
                    deadline,
                    [str(name) for name in (data.get("toolNames") or [])],
                )
            elif kind == "tool":
                tool_count += 1
                if tool_count > max_tool_calls:
                    raise RuntimeError("pi_tool_budget_exhausted")
                result = tool_call(
                    str(data.get("name") or ""),
                    data.get("arguments") or {},
                )
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
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:
            pass
        reader.join(timeout=2)
        try:
            if process.stdout is not None:
                process.stdout.close()
        except Exception:
            pass
