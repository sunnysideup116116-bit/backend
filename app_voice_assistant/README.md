# App voice assistant

Demo-only, Android-first global voice assistant. It accepts device-produced text
or short PCM16 fallback audio and returns allowlisted UI action proposals. This
package never writes profiles, settings, or posts; Flutter executes confirmed
actions through the current signed-in session. It keeps one server-owned,
two-layer voice conversation summary per verified Appwrite account. The summary
is capped at 200 characters and is refreshed by Ollama DeepSeek after a session
ends. Voice-close commands and acknowledgements are removed before summarizing.
DeepSeek is retried first; a bounded local rolling summary still advances the
Appwrite revision if the model is temporarily unavailable.

Current weather questions use the server-owned `read_weather` tool. A resolved
location always triggers both Google Weather and Google Air Quality current
condition requests; Google credentials are never sent to Flutter. An explicitly
spoken place takes priority. When no place is spoken, the Server lazily reads
the city and district saved under the Agent-only location setting; it does not
use the unrelated Appwrite profile region or add a location read to non-weather
turns.

Calendar reads accept both the existing presets and any inclusive
`start_date`/`end_date` interval in the past, present, or future.

完整的目前能力、限制與啟用條件請見 [CAPABILITIES.md](CAPABILITIES.md)。

自然改口、部分草稿、插話後接續與跨連線確認保護，請見
[CONVERSATIONAL_DRAFTS.md](CONVERSATIONAL_DRAFTS.md)。

約會方案比較、長期／本次偏好、App 功能說明與純語音狀態查詢，請見
[VOICE_EXPERIENCE.md](VOICE_EXPERIENCE.md)。

額度感知、狀態模式、Risk 冷卻與傳送回執的 API、行為及部署方式見
[OPERATIONAL_STATUS.md](OPERATIONAL_STATUS.md)。

Public routes:

- `GET /api/app-voice/capability`
- `POST /api/app-voice/session`
- `GET /api/app-voice/tasks`
- `POST /api/app-voice/tasks/{task_ref}/cancel`
- `POST /api/app-voice/tasks/{task_ref}/retry`
- `POST /api/app-voice/tasks/{task_ref}/input`
- `POST /api/app-voice/tasks/{task_ref}/dismiss`
- `POST /api/app-voice/tasks/{task_ref}/undo`
- `WSS /api/app-voice`

Protocol v4 supports three server-side routing modes: `legacy` keeps the
original direct Gemini declarations, `proxy` uses the seven-tool capability
catalog, and `template` uses the official Gemini Live-style direct session with
a small domain-level tool surface. Template mode keeps simple reads and
navigation on one tool round-trip while complex writes still use the existing
confirmation and task boundaries. Protocol-v4 sessions always verify a fresh
Appwrite JWT; task state is stored in the existing Social Mongo database and
never contains raw audio or full transcripts.

Template mode exposes 15 direct tools plus the two shared signed-catalog tools.
Simple reads, navigation and writes keep their direct route. Unified digests,
authorized App search, personal routines and fixed/multi-step workflows use
`find_app_capabilities` and `run_app_capabilities`, with the same owner/session,
permission, revision, confirmation and task checks as proxy mode. They emit
the existing protocol-v4 Flutter events; no Flutter update or catalog migration
is required. Background/multi-step work still requires `VOICE_APP_TASKS_ENABLED`.

`VOICE_APP_VAD_SILENCE_MS` controls how long Gemini Live waits for a speech pause
before ending the user's turn. The default is 700 ms (previously fixed at 450 ms),
bounded to 200–2000 ms; invalid values use the default. This adds up to 250 ms to
the configured silence window to accommodate natural pauses, but is not a
measured device-latency or recognition improvement. Set 450 to restore the old
timing. Restart the Server through `start_all.sh` to load environment changes;
new Live connections and reconnects use the setting. PCM, microphone capture,
barge-in, and the device STT fallback remain unchanged.

Catalog v5 keeps the same seven model-visible tools while adding guided App
coaching, permission repair, an authorized-data search, a unified digest, four
fixed workflows, structured waiting-input cards, retry/edit/dismiss/undo task
controls, and up to eight device-local personal routines. Routines only store a
name, an allowlisted template, and bounded template arguments; they never store
user IDs, database IDs, or capability refs.

Copy the values from `.env.example` into `Server/.env` and start the complete
stack only through `Server/start_all.sh`. Free Gemini fallback is intended for
synthetic graduation-demo data, not real-user personal information.

Create or verify the private Appwrite schema with:

```bash
Server/venv/bin/python Server/app_voice_assistant/setup_memory_appwrite.py --apply
```

The Server rejects public Appwrite endpoints for this feature. Configure
`APPWRITE_INTERNAL_ENDPOINT` with a loopback, private-address, or private service
name ending in `/v1`.
