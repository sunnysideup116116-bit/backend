# App voice assistant

Demo-only, Android-first global voice assistant. It accepts device-produced text
or short PCM16 fallback audio and returns allowlisted UI action proposals. This
package never writes profiles, settings, or posts; Flutter executes confirmed
actions through the current signed-in session.

完整的目前能力、限制與啟用條件請見 [CAPABILITIES.md](CAPABILITIES.md)。

Public routes:

- `GET /api/app-voice/capability`
- `POST /api/app-voice/session`
- `WSS /api/app-voice`

Copy the values from `.env.example` into `Server/.env` and start the complete
stack only through `Server/start_all.sh`. Free Gemini fallback is intended for
synthetic graduation-demo data, not real-user personal information.
