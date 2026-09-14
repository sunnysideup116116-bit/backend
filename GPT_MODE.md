# Experimental GPT mode (personal evaluation)

`./start_all.sh` and `./start_all.sh ollama` retain Ollama. Existing main/fast
Ollama configuration, including `deepseek-v4-flash:cloud`, is unchanged.
`./start_all.sh gpt` selects official Codex app-server managed ChatGPT OAuth.
`./start_all.sh --help` exits without touching services.

This is experimental **personal evaluation**, not a claim that a personal
ChatGPT quota authorizes or supports a shared production service. Account/model
access, quotas, commercial suitability and service guarantees need separate
evaluation. Official protocol documentation:
[Codex App Server](https://learn.chatgpt.com/docs/app-server).

## Operator setup

Install `social/requirements.txt` in the existing Social environment (adds
`jsonschema>=4.23,<5`). Use the installed Codex CLI; protocol fields were checked
against `codex-cli 0.153.4` with `app-server generate-json-schema --experimental`.
The adapter requires experimental environment-access controls; older builds are
not supported. Recheck protocol compatibility when upgrading Codex.

Choose a dedicated absolute directory for Codex-managed login/config, outside
the repository. Do not reuse the desktop Codex home, install plugins or configure
MCP servers in this home. Put settings in **Server/.env**. Standalone preflight
and Social share `social/services/gpt_settings.py`, loaded before Social's
general dotenv handling. It imports only the GPT keys below; explicit process
environment values (including empty values) win over Server/.env. GPT settings
belong in Server/.env, not social/.env. `AYUE_SKIP_DOTENV=1` disables this loader.
No shell sourcing, variable interpolation, credential extraction or dotenv
value logging occurs. Changing dotenv requires restarting the process.

```bash
AYUE_CODEX_HOME=/home/sunny/.local/share/ayue-codex
AYUE_CODEX_BIN=/usr/lib/chatgpt/resources/codex
AYUE_GPT_MODEL=gpt-5.6-luna
# Optional; empty/unset uses AYUE_GPT_MODEL.
AYUE_GPT_FAST_MODEL=gpt-5.6-luna
AYUE_GPT_SERVICE_TIER=fast
AYUE_GPT_TIMEOUT_SECONDS=120
```

Public V3 exposes eight optional owner-specific model overrides for each
provider. Leave a value empty or unset to preserve the original tier routing.
The fast-tier owners are Planner, Places, Match, Relationship and Profile;
Calendar, Web and Synthesizer use the main tier.

```bash
# The same eight suffixes are available under AYUE_GPT_* and AYUE_OLLAMA_*.
AYUE_GPT_PLANNER_MODEL=
AYUE_GPT_CALENDAR_MODEL=
AYUE_GPT_PLACES_MODEL=
AYUE_GPT_MATCH_MODEL=
AYUE_GPT_RELATIONSHIP_MODEL=
AYUE_GPT_PROFILE_MODEL=
AYUE_GPT_WEB_MODEL=
AYUE_GPT_SYNTHESIZER_MODEL=

AYUE_OLLAMA_PLANNER_MODEL=
AYUE_OLLAMA_CALENDAR_MODEL=
AYUE_OLLAMA_PLACES_MODEL=
AYUE_OLLAMA_MATCH_MODEL=
AYUE_OLLAMA_RELATIONSHIP_MODEL=
AYUE_OLLAMA_PROFILE_MODEL=
AYUE_OLLAMA_WEB_MODEL=
AYUE_OLLAMA_SYNTHESIZER_MODEL=
```

Ollama keeps its existing process-wide runtime override and per-call explicit
model precedence. GPT owner models are also verified against the Codex model
catalog during `./start_all.sh gpt` preflight. Changing these values requires a
restart.

Complete managed login separately, under the same OS user and dedicated home:

```bash
env CODEX_HOME=/home/sunny/.local/share/ayue-codex /usr/lib/chatgpt/resources/codex login --device-auth
```

Follow the device-code instructions yourself. The official app-server equivalent
is `account/login/start` with `type: "chatgptDeviceCode"`. Startup does not start
login or open a browser. Never extract/copy OAuth tokens or use an API key as a
fallback. Only Codex manages credential storage and refresh.

Then start the complete stack with `./start_all.sh gpt`. Before port cleanup,
preflight checks the dependency, managed ChatGPT account type, paginated model
catalog for both selected tiers, and absence of MCP servers. It sends no prompt
and starts no turn. Missing login, unavailable models, timeout or protocol errors
abort startup before existing ports are touched. Catalog presence is not a
guarantee of sufficient quota for a later generation.

## Scope and behavior

Only shared `social/services/ai_service.py` completion calls change provider:
Public V3 planner/sub-agents/synthesizer, Private V2 consumers, profile/summary
and Social helper calls. Risk, Matchmaker's own LLM, embeddings and Guardrail
keep their existing providers. Social helper functions named `match_candidates`
or match explanation still switch because they use shared Social completion.
Ports stay Social 8000, Risk 8001, Matchmaker 9001, Guardrail 8081. Public URL
remains `https://service.misproject.us.ci/`.

GPT tier fallback uses `AYUE_GPT_MODEL` / `AYUE_GPT_FAST_MODEL`, after an optional
owner-specific model. Existing
Ollama explicit model and runtime model/thinking overrides do not select GPT
models; the legacy settings UI remains Ollama-oriented. Effective-model telemetry
uses the selected GPT tier. There is no provider/model fallback.

Fast mode is separate from fast-model routing and reasoning effort. The
[official speed documentation](https://learn.chatgpt.com/docs/agent-configuration/speed)
describes `service_tier="fast"` and `features.fast_mode=true` for Codex config.
The installed 0.153.4 app-server schema instead exposes `model/list.serviceTiers`
(not `supportedServiceTiers`) with catalog IDs. Luna's locally cached catalog
and the operator's live catalog check identify `{id: "priority", name: "Fast"}`.
The adapter enables `features.fast_mode=true`, resolves the dotenv alias `fast`
to that catalog ID during metadata verification, and sends `serviceTier="priority"`
on both thread/start and turn/start. A literal catalog ID is also accepted.
Missing/ambiguous tier support, unavailable models, or a changed thread tier
fail explicitly; there is no silent downgrade. Both selected models are checked
during preflight. Empty/unset service tier leaves the Codex default untouched.

Reasoning remains **default (no override)**; Luna's observed catalog default is
`medium`. Benchmark labels should record `model=gpt-5.6-luna`,
`service_tier=priority (Fast)`, `reasoning=default (catalog: medium)`.
The adapter does not force low reasoning. Catalog data can change; cached
support alone does not guarantee a future request's account access or quota.

Social lazily owns a bounded pool of persistent stdio app-server processes:
two workers by default, each leased exclusively to one request. Every generation
starts a fresh ephemeral thread in its worker's empty temporary directory.
No room/user thread is resumed or shared. Only
the caller's existing sliced prompt enters that thread. `environments: []`, empty
dynamic tools/capability/workspace roots, read-only sandbox, no approvals and
disabled web search/apps/shell/memory constrain Codex. Unexpected client tool or
approval requests fail closed. Child environment is allowlisted and excludes
API keys. App-server stderr and remote error payloads are never returned/logged.
Model-provider transport necessarily uses network; agent environment network
access is disabled. Codex itself retains access to its managed auth home.

Successful calls use `thread/unsubscribe` (installed schema statuses:
`unsubscribed`, `notLoaded`, `notSubscribed`). Unsubscription can retain a loaded
thread for 30 minutes; it is not immediate unloading. Each worker is therefore
terminated and replaced after 32 leases, bounding retained contexts during
sustained traffic. No thread is archived or resumed. Any request, protocol,
validation, callback or cleanup error discards the worker without retrying the
generation. Social shutdown and `atexit` reap only owned process groups; forked
children reset inherited pool locks and cannot signal the parent's workers.

Each worker caches one successful account/model/tier/MCP verification for 300
seconds. A different model set or service tier replaces that entry; failure
invalidates it. Account changes are rechecked on expiry. Changing Codex home or
binary at runtime fails explicitly and requires a Social restart. Optional
**process environment** tuning (these keys are not loaded from Server/.env):
`AYUE_GPT_POOL_SIZE` 1–4 (default 2), `AYUE_GPT_WORKER_MAX_REQUESTS` 1–128
(default 32), `AYUE_GPT_METADATA_TTL_SECONDS` 1–3600 (default 300).

For tools or JSON output, `turn/start.outputSchema` constrains the final envelope to `content` and (when
tools exist) `tool_calls[{name, arguments_json}]`. Arguments use a JSON string
to preserve optional fields/dynamic maps without rewriting the original tool
schema's required fields. The adapter validates the envelope, known tool name,
decoded argument object and original JSON schema. These remain **proposals**:
the existing application Guard, confirmation and executors retain authority.
Tool definitions are prompt data, never registered as executable Codex tools.

Tools and JSON output **buffer until successful completion and validation**,
then deliver chunks of at most 120 characters. Plain text without tools omits
`outputSchema` and streams deltas only from a matching thread/turn/item explicitly
marked `final_answer`. Commentary and reasoning are never streamed. Items without
an explicit final phase remain buffered; the final response supplies any unsent
suffix without duplicating deltas. A later failure can leave partial plain text
already delivered, and propagates without retry. `thread/tokenUsage/updated` supplies
observed token counts; the existing numeric metric contract uses zero for
**unknown/unobserved**, not proof of zero usage. TTFT/TPS remain unobserved (zero).
Duration includes queueing, any app-server startup/auth/catalog overhead and cleanup. The installed
`turn/start` has no temperature or output-token-limit fields: existing
`temperature`, `max_tokens`, and Ollama thinking parameters are not applied in
GPT mode. No hard output-token cap is promised; transport size/deadline limits
still apply. A caller deadline covers pool queueing, generation, validation,
unsubscribe and callbacks; expired/failed calls terminate and reap that worker's
process group. Reaping may require up to a one-second grace period after the
request deadline. Synchronous callbacks cannot be preempted, but expiry is
checked after callback return.

## Verification limits

Pool/streaming follow-up: 78 targeted offline tests passed across provider,
pool, GPT settings, AI prompt/stream compatibility and startup contracts;
`bash -n start_all.sh` and `git diff --check` passed. Tests cover exclusive
parallel workers, queue deadlines, TTL, recycling, fresh threads, release errors,
shutdown before first use, fork locks, runtime identity and callback failure.
Latency measurements and before/after interpretation are maintained separately
in `GPT_POOL_BENCHMARK.md`; process reuse alone does not establish a speedup.

Offline tests mock all Codex process launches; pipe tests use local file
descriptors only. Startup tests use an isolated script copy and fake preflight.
No real prompt, account login, service restart or production data access is part
of automated verification. Live model quality, quota, latency and a successful
authenticated generation remain operator acceptance checks.

Verified in this workspace: jsonschema 4.26.0 installed in Social's venv;
186 targeted Social tests plus 3 subtests passed; 16 startup/launcher tests
passed; Python compilation, `bash -n start_all.sh` and `git diff --check` passed.
The targeted Social command excludes five unrelated files requiring the missing
test-only `mongomock` dependency; this is not a full-suite pass:

```bash
AYUE_LLM_PROVIDER=ollama .local-venv/social/bin/python scripts/run_offline_tests.py social \
  -k 'codex_chat_provider or ai_service_prompt_roles or ai_service_stream_metrics or v3_planner_deadline or v3_guard or v3_sub_agents or v3_scheduler or public_ai_room_scope' \
  --ignore=tests/test_chat_read_efficiency.py \
  --ignore=tests/test_event_delivery_adapter.py \
  --ignore=tests/test_event_scheduled_once.py \
  --ignore=tests/test_event_weekly_progress.py \
  --ignore=tests/test_owner_memory_refresh.py
AYUE_LLM_PROVIDER=ollama .local-venv/social/bin/python scripts/run_offline_tests.py contracts \
  -k 'start_all or ayue_launch'
```

A separate real **unauthenticated** temporary-home check passed `initialize`,
`account/read` (account absent), `model/list` and `mcpServerStatus/list` against
the installed binary; no turn was started. GitNexus change analysis confirmed
the three intended existing Python symbols (overall CRITICAL, 21 affected
flows). New untracked provider/test files are not represented by that existing
index and were checked through the tests and direct diff/file review.
