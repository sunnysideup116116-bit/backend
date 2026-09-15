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
