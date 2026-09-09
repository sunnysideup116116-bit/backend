"""Experimental personal evaluation adapter for Codex managed ChatGPT auth.

Only the official stdio protocol is used. Never inspect credentials or execute
model proposals: the application's existing Guard and executors own actions.
"""
from __future__ import annotations

import json
import atexit
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time
import threading
from contextlib import contextmanager

if __package__:
    from .gpt_settings import FAST_MODEL_OWNERS, LLM_OWNER_NAMES, load_gpt_settings
else:  # start_all.sh invokes this file directly with the Social interpreter.
    from gpt_settings import FAST_MODEL_OWNERS, LLM_OWNER_NAMES, load_gpt_settings

load_gpt_settings()


class CodexProviderError(RuntimeError):
    """Sanitized provider failure; never includes remote payloads or stderr."""


def selected_provider() -> str:
    provider = os.environ.get("AYUE_LLM_PROVIDER", "ollama").strip().lower()
    if provider not in {"ollama", "gpt"}:
        raise CodexProviderError("AYUE_LLM_PROVIDER must be ollama or gpt")
    return provider


def selected_model(*, fast: bool = False, owner: str | None = None) -> str:
    if owner is not None:
        if owner not in LLM_OWNER_NAMES:
            raise CodexProviderError(f"Unknown LLM owner: {owner}")
        owner_model = os.environ.get(f"AYUE_GPT_{owner.upper()}_MODEL", "").strip()
        if owner_model:
            return owner_model
    main = os.environ.get("AYUE_GPT_MODEL", "").strip()
    model = (os.environ.get("AYUE_GPT_FAST_MODEL", "").strip() or main) if fast else main
    if not model:
        raise CodexProviderError("Set AYUE_GPT_MODEL to an available Codex model")
    return model


def request_deadline(deadline: float | None = None) -> float:
    try:
        seconds = float(os.environ.get("AYUE_GPT_TIMEOUT_SECONDS", "120"))
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError
    except ValueError:
        raise CodexProviderError("AYUE_GPT_TIMEOUT_SECONDS must be positive and finite") from None
    own = time.monotonic() + seconds
    result = min(own, deadline) if deadline is not None else own
    if not math.isfinite(result) or result <= time.monotonic():
        raise TimeoutError("Codex request deadline exhausted")
    return result


class StdioRpc:
    """Bounded nonblocking JSON-lines RPC with one overall deadline."""

    def __init__(self, process, deadline: float):
        self.process = process
        self.deadline = deadline
        self.sequence = 0
        self.buffer = bytearray()
        self.pending = []
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stdin.fileno(), False)

    def ready(self, pipe, event):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex request deadline exhausted")
        with selectors.DefaultSelector() as selector:
            selector.register(pipe, event)
            if not selector.select(remaining):
                raise TimeoutError("Codex request deadline exhausted")

    def send(self, message):
        data = memoryview((json.dumps(message, ensure_ascii=False) + "\n").encode())
        if len(data) > 2_000_000:
            raise CodexProviderError("Codex request exceeds size limit")
        while data:
            self.ready(self.process.stdin, selectors.EVENT_WRITE)
            try:
                data = data[os.write(self.process.stdin.fileno(), data):]
            except BlockingIOError:
                continue
            except OSError:
                raise CodexProviderError("Codex transport closed") from None

    def receive(self):
        while b"\n" not in self.buffer:
            self.ready(self.process.stdout, selectors.EVENT_READ)
            try:
                chunk = os.read(self.process.stdout.fileno(), 65536)
            except BlockingIOError:
                continue
            if not chunk:
                raise CodexProviderError("Codex transport closed")
            self.buffer.extend(chunk)
            if len(self.buffer) > 2_000_000:
                raise CodexProviderError("Codex response exceeds size limit")
        line, _, rest = self.buffer.partition(b"\n")
        self.buffer = bytearray(rest)
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError
        except (ValueError, UnicodeError):
            raise CodexProviderError("Invalid Codex protocol frame") from None
        if "method" in message and "id" in message:
            self.send({"id": message["id"], "error": {
                "code": -32601, "message": "Client tools and approvals are disabled",
            }})
            raise CodexProviderError("Codex requested a forbidden client action")
        return message

    def call(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.send({"id": request_id, "method": method, "params": params})
        while True:
            message = self.receive()
            if "id" in message:
                if message["id"] != request_id or "error" in message:
                    raise CodexProviderError("Codex RPC failed")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise CodexProviderError("Invalid Codex RPC result")
                return result
            self.pending.append(message)
            if len(self.pending) > 1000:
                raise CodexProviderError("Excessive Codex notifications")

    def event(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Codex request deadline exhausted")
        return self.pending.pop(0) if self.pending else self.receive()


@contextmanager
def session(deadline):
    with _get_pool().lease(deadline) as worker:
        yield worker.rpc, worker.cwd


def _bounded_setting(name, default, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
        if not 1 <= value <= maximum:
            raise ValueError
        return value
    except ValueError:
        raise CodexProviderError(f"{name} must be between 1 and {maximum}") from None


class _Worker:
    """One owned process; caller holds the pool's exclusive lease."""

    def __init__(self):
        self.owner = os.getpid()
        self.lock = threading.Lock()
        self.process = self.rpc = self.directory = None
        self.cwd = None
        self.closed = False
        self.busy = False
        self.calls = 0

    def start(self, deadline):
        with self.lock:
            if self.closed:
                raise CodexProviderError("Codex worker is closed")
            if self.rpc is not None:
                self.rpc.deadline = deadline
                self.rpc.pending.clear()
                return
            self.directory = tempfile.TemporaryDirectory(prefix="ayue-codex-worker-")
            self.cwd = self.directory.name
            self.process = _launch_process(self.cwd, deadline)
            self.rpc = StdioRpc(self.process, deadline)
        self.rpc.call("initialize", {"clientInfo": {"name": "ayue_personal_eval", "version": "1.0"},
                                     "capabilities": {"experimentalApi": True}})
        self.rpc.send({"method": "initialized", "params": {}})

    def close(self):
        # Never signal a parent's process from a forked child or race two reapers.
        if self.owner != os.getpid():
            return
        with self.lock:
            if self.closed or self.owner != os.getpid():
                return
            self.closed = True
            process = self.process
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                process.stdin.close()
                process.stdout.close()
            if self.directory is not None:
                self.directory.cleanup()
            self.rpc = None


class _Pool:
    def __init__(self, size=2, max_requests=32):
        self.owner = os.getpid()
        self.identity = _runtime_identity()
        self.size = size
        self.max_requests = max_requests
        self.condition = threading.Condition()
        self.workers = set()
        self.closed = False

    @contextmanager
    def lease(self, deadline):
        if self.owner != os.getpid():
            raise CodexProviderError("Codex pool belongs to another process")
        with self.condition:
            while True:
                if self.closed or self.owner != os.getpid():
                    raise CodexProviderError("Codex pool is closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Codex request deadline exhausted in pool queue")
                worker = next((w for w in self.workers if not w.busy), None)
                if worker is None and len(self.workers) < self.size:
                    worker = _Worker()
                    self.workers.add(worker)
                if worker is not None:
                    worker.busy = True
                    break
                self.condition.wait(remaining)
        healthy = False
        try:
            worker.start(deadline)
            yield worker
            healthy = time.monotonic() < deadline
            if not healthy:
                raise TimeoutError("Codex request deadline exhausted")
        finally:
            worker.calls += 1
            with self.condition:
                discard = not healthy or self.closed or worker.calls >= self.max_requests
            # Keep the slot reserved until its process has actually been reaped.
            if discard:
                worker.close()
            with self.condition:
                if discard:
                    self.workers.discard(worker)
                worker.busy = False
                self.condition.notify_all()

    def close(self):
        if self.owner != os.getpid():
            return
        with self.condition:
            self.closed = True
            workers = list(self.workers)
            self.condition.notify_all()
        for worker in workers:
            worker.close()


_pool = None
_pool_lock = threading.Lock()
_shutdown = False


def _runtime_identity():
    return (os.environ.get("AYUE_CODEX_HOME", ""), os.environ.get("AYUE_CODEX_BIN", "codex"))


def _after_fork():
    global _pool, _pool_lock
    # Inherited locks may be held by threads that do not exist in the child.
    # Drop only the child's fd copies and detach temporary-directory finalizers;
    # the parent alone owns its processes and directories.
    if _pool is not None:
        for worker in _pool.workers:
            if worker.directory is not None:
                worker.directory._finalizer.detach()
            if worker.process is not None:
                worker.process.stdin.close()
                worker.process.stdout.close()
    _pool = None
    _pool_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _get_pool():
    global _pool
    with _pool_lock:
        if _shutdown:
            raise CodexProviderError("Codex provider is shut down")
        if _pool is None or _pool.owner != os.getpid():
            _pool = _Pool(_bounded_setting("AYUE_GPT_POOL_SIZE", 2, 4),
                          _bounded_setting("AYUE_GPT_WORKER_MAX_REQUESTS", 32, 128))
        if _pool.identity != _runtime_identity():
            raise CodexProviderError("Codex home or binary changed; restart Social to apply settings")
        return _pool


def shutdown():
    """Stop accepting work and reap owned workers; safe to call repeatedly."""
    global _shutdown
    with _pool_lock:
        _shutdown = True
        pool = _pool
    if pool is not None:
        pool.close()


atexit.register(shutdown)


def _launch_process(cwd, deadline):
    if deadline <= time.monotonic():
        raise TimeoutError("Codex request deadline exhausted")
    # A dedicated operator-managed home prevents importing desktop plugins,
    # project instructions and history. Authentication stays owned by Codex.
    auth_home = os.environ.get("AYUE_CODEX_HOME", "")
    if not auth_home or not Path(auth_home).is_absolute():
        raise CodexProviderError("Set AYUE_CODEX_HOME to a dedicated absolute Codex home")
    executable = os.environ.get("AYUE_CODEX_BIN", "codex")
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR") if key in os.environ}
    env["CODEX_HOME"] = auth_home
    command = [executable, "app-server", "--stdio"]
    for config in (
            'forced_login_method="chatgpt"', 'model_provider="openai"',
            'approval_policy="never"', 'sandbox_mode="read-only"',
            'web_search="disabled"', 'features.apps=false',
            'features.shell_tool=false', 'features.multi_agent=false',
            'features.memories=false', 'mcp_servers={}',
            'features.fast_mode=true',
    ):
        command.extend(["-c", config])
    try:
        return subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, cwd=cwd, env=env,
                                       start_new_session=True)
    except OSError:
        raise CodexProviderError("Cannot start Codex app-server") from None


def verify_account_and_models(rpc, models):
    requested_tier = os.environ.get("AYUE_GPT_SERVICE_TIER", "").strip()
    key = (frozenset(models), requested_tier)
    cached = rpc.__dict__.get("_verified")
    if cached and cached[0] == key and time.monotonic() < cached[1]:
        return dict(cached[2])
    rpc._verified = None
    ttl = _bounded_setting("AYUE_GPT_METADATA_TTL_SECONDS", 300, 3600)
    account = rpc.call("account/read", {"refreshToken": True}).get("account")
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        raise CodexProviderError("Managed ChatGPT login required; run Codex device login separately")
    remaining = set(models)
    requested_tier = os.environ.get("AYUE_GPT_SERVICE_TIER", "").strip()
    resolved_tiers = {}
    cursor = None
    seen = set()
    while True:
        page = rpc.call("model/list", {"cursor": cursor, "includeHidden": False})
        for item in page.get("data", []):
            model = item.get("model")
            if item.get("hidden", False) or model not in remaining:
                continue
            tier = None
            if requested_tier:
                # Installed protocol uses serviceTiers, with opaque catalog IDs.
                # "fast" is the operator alias for the catalog's Fast tier.
                matches = [entry["id"] for entry in item.get("serviceTiers", [])
                           if entry.get("id") == requested_tier or
                           (requested_tier == "fast" and
                            entry.get("name", "").lower() == "fast")]
                if len(set(matches)) != 1:
                    raise CodexProviderError("Selected GPT service tier is unavailable or ambiguous; no fallback allowed")
                tier = matches[0]
            resolved_tiers[model] = tier
            remaining.remove(model)
        cursor = page.get("nextCursor")
        if not cursor:
            break
        if cursor in seen:
            raise CodexProviderError("Invalid Codex model pagination")
        seen.add(cursor)
    if remaining:
        raise CodexProviderError("Selected GPT model is unavailable; no fallback allowed")
    # Refuse any configured MCP server, even if it currently has no tools.
    status = rpc.call("mcpServerStatus/list", {})
    if status.get("data") or status.get("nextCursor"):
        raise CodexProviderError("Dedicated Codex home must have no MCP servers")
    rpc._verified = (key, time.monotonic() + ttl, dict(resolved_tiers))
    return resolved_tiers


def preflight():
    try:
        from jsonschema import Draft202012Validator  # noqa: F401
    except ImportError:
        raise CodexProviderError("Install social/requirements.txt before GPT startup") from None
    models = {selected_model(), selected_model(fast=True)}
    models.update(
        selected_model(owner=owner, fast=owner in FAST_MODEL_OWNERS)
        for owner in LLM_OWNER_NAMES
    )
    with session(request_deadline()) as (rpc, _):
        verify_account_and_models(rpc, models)


def proposal_schema(tools):
    # Arguments are encoded JSON strings so optional fields, dynamic maps and
    # unions retain the application's original schema without making every
    # optional tool argument required by strict structured-output rules.
    call = {"type": "object", "additionalProperties": False,
            "properties": {"name": {"type": "string", "enum": [t["function"]["name"] for t in tools]},
                           "arguments_json": {"type": "string"}},
            "required": ["name", "arguments_json"]}
    properties = {"content": {"type": "string"}}
    if tools:
        properties["tool_calls"] = {"type": "array", "items": call}
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}


def generate(prompt, *, model, system_prompt=None, tools=None, json_output=False,
             deadline_monotonic=None, on_token=None):
    from jsonschema import Draft202012Validator

    started = time.monotonic()
    plain_text = not tools and not json_output
    streamed = ""
    streaming_item = None
    schema = proposal_schema(tools or [])
    deadline = request_deadline(deadline_monotonic)
    with session(deadline) as (rpc, cwd):
        tier = verify_account_and_models(rpc, [model])[model]
        tier_params = {"serviceTier": tier} if tier else {}
        thread = rpc.call("thread/start", {
            **tier_params,
            "model": model, "modelProvider": "openai", "allowProviderModelFallback": False,
            "ephemeral": True, "environments": [], "dynamicTools": [],
            "selectedCapabilityRoots": [], "runtimeWorkspaceRoots": [],
            "cwd": cwd, "sandbox": "read-only", "approvalPolicy": "never",
            "baseInstructions": "Generate only the requested response or tool proposals. Never execute actions or use tools. Tool proposals require the application's Guard before execution.",
            "developerInstructions": (system_prompt or "") +
                ("\nPut valid JSON in content." if json_output else "") +
                "\nAllowed proposal definitions (arguments_json must encode their parameters):\n" + json.dumps(tools or [], ensure_ascii=False),
        })
        if thread.get("model") != model or thread.get("modelProvider") != "openai":
            raise CodexProviderError("Codex changed requested model or provider")
        if tier and thread.get("serviceTier") != tier:
            raise CodexProviderError("Codex changed requested service tier; no fallback allowed")
        if thread.get("instructionSources") or thread.get("sandbox", {}).get("type") != "readOnly" or thread.get("sandbox", {}).get("networkAccess", False):
            raise CodexProviderError("Unexpected Codex instructions or permissions")
        thread_id = thread["thread"]["id"]
        turn = rpc.call("turn/start", {"threadId": thread_id,
                        **tier_params,
                        "input": [{"type": "text", "text": prompt, "text_elements": []}],
                        **({} if plain_text else {"outputSchema": schema})})
        turn_id = turn["turn"]["id"]
        final_text = None
        input_tokens = output_tokens = 0  # Existing metric contract: zero = unobserved.
        while True:
            event = rpc.event()
            method, params = event.get("method"), event.get("params", {})
            if params.get("threadId") != thread_id:
                continue
            if method == "error" and params.get("turnId") == turn_id and not params.get("willRetry", False):
                raise CodexProviderError("Codex turn failed")
            if method == "thread/tokenUsage/updated" and params.get("turnId") == turn_id:
                usage = params.get("tokenUsage", {}).get("last", {})
                input_tokens = int(usage.get("inputTokens") or 0)
                output_tokens = int(usage.get("outputTokens") or 0)
            if (plain_text and on_token and method == "item/agentMessage/delta"
                    and params.get("turnId") == turn_id
                    and streaming_item is not None and params.get("itemId") == streaming_item):
                delta = params.get("delta")
                if not isinstance(delta, str) or len(streamed) + len(delta) > 2_000_000:
                    raise CodexProviderError("Invalid Codex text delta")
                streamed += delta
                if delta:
                    on_token(delta)
            if method in {"item/started", "item/completed"} and params.get("turnId") == turn_id:
                item = params.get("item", {})
                if item.get("type") not in {"agentMessage", "userMessage", "reasoning"}:
                    raise CodexProviderError("Codex emitted a forbidden action")
                if (plain_text and method == "item/started" and item.get("type") == "agentMessage"
                        and item.get("phase") == "final_answer"):
                    if streaming_item is not None and item.get("id") != streaming_item:
                        raise CodexProviderError("Multiple Codex final messages")
                    streaming_item = item.get("id")
                if method == "item/completed" and item.get("type") == "agentMessage" and item.get("phase") in {None, "final_answer"}:
                    final_text = item.get("text")
            if method == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                if params["turn"].get("status") != "completed":
                    raise CodexProviderError("Codex turn did not complete successfully")
                break
        try:
            result = {"content": final_text} if plain_text else json.loads(final_text)
            Draft202012Validator(schema).validate(result)
            if not result["content"].startswith(streamed):
                raise ValueError
            calls = []
            definitions = {t["function"]["name"]: t["function"].get("parameters", {}) for t in tools or []}
            for call in result.get("tool_calls", []):
                arguments = json.loads(call["arguments_json"])
                if not isinstance(arguments, dict):
                    raise ValueError
                Draft202012Validator(definitions[call["name"]]).validate(arguments)
                calls.append({"name": call["name"], "arguments": arguments})
            if json_output:
                json.loads(result["content"])
        except Exception:
            raise CodexProviderError("Invalid Codex proposal output") from None
        released = rpc.call("thread/unsubscribe", {"threadId": thread_id})
        if released.get("status") not in {"unsubscribed", "notLoaded", "notSubscribed"}:
            raise CodexProviderError("Codex thread release failed")
        # Structured content stays buffered. Keep callbacks within the lease so
        # their failure or deadline expiry also discards the worker.
        if on_token and result["content"]:
            for offset in range(len(streamed), len(result["content"]), 120):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Codex request deadline exhausted")
                on_token(result["content"][offset:offset + 120])
        if time.monotonic() >= deadline:
            raise TimeoutError("Codex request deadline exhausted")
    return {"content": result["content"], "tool_calls": calls,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "duration_ms": round((time.monotonic() - started) * 1000)}


if __name__ == "__main__":
    try:
        preflight()
    except (CodexProviderError, TimeoutError) as exc:
        # Only these locally authored, sanitized messages may reach the console.
        raise SystemExit(f"GPT preflight failed: {exc}. No services were started.") from None
    print("GPT preflight passed (managed ChatGPT account and selected models).")
