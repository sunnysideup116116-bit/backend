# Registration voice assistant

This Server-root package owns the registration-only Gemini Live integration.
Flutter sends PCM16 audio to the public Social route; API keys, key rotation,
Gemini tool calls, validation, and session tickets remain on Server.

## Environment

```dotenv
VOICE_REGISTRATION_ENABLED=on
VOICE_LOCAL_RATE_LIMIT_ENABLED=off
VOICE_REGISTRATION_THINKING_LEVEL=low
VOICE_SILENCE_DURATION_MS=500
VOICE_MAX_SESSION_SECONDS=120
```

`VOICE_LOCAL_RATE_LIMIT_ENABLED=off` keeps the limiter implementation dormant.
Changing it to `on` applies the configured per-identity and global limits after
restarting through `Server/start_all.sh`. The 120-second audio/session safety
boundary remains active in either mode.

Put these values in `Server/.env`; model selection, including
`VOICE_REGISTRATION_MODEL`, is also defined there. `registration_voice/.env.example`
contains the complete feature-specific template. Spoken input is limited by policy to
Taiwan Mandarin and English, and Chinese transcript/form output is always
Traditional Chinese. Other languages are rejected without changing the form.
`VOICE_SILENCE_DURATION_MS=500` means a 0.5-second pause ends the current
utterance/turn; it does not close the two-minute WebSocket session.

The key pool reads dedicated `VOICE_GOOGLE_API_KEY_1...` keys first, then the
project's existing `GOOGLE_API_KEYS1...`, `GOOGLE_API_KEY_1...`, and compatible
single-key variables. Every new Live session starts at the next key. A key that
fails setup or returns a quota/transient error enters a cooldown and the same
client WebSocket automatically tries the next available key. Keys and key
fragments are never returned or logged.

Google applies Gemini rate limits per project, not per API key. Rotation gives
fair session distribution and failover, but multiple keys from the same Google
project still share that project's quota. Check the active project limits in
Google AI Studio; Preview-model limits can change independently of this code.

## Public routes

- `GET /api/registration/voice/capability`
- `POST /api/registration/voice/session`
- `WSS /api/registration/voice`

The session endpoint returns a one-use ticket, not a Google credential. The
WebSocket accepts control JSON plus binary 16kHz mono PCM16 frames and returns
status plus revisioned `form_patch` events. Input transcription is not requested
or displayed; the model extracts fields directly from audio. Password is absent
from every accepted form/tool schema.

## Verification

Unit tests are offline:

```bash
PYTHONPATH=Server Server/.local-venv/social/bin/python -m pytest -q \
  Server/registration_voice/tests
```

The opt-in smoke scripts use free Google quota and synthetic, non-user data:

```bash
cd Server
VOICE_LIVE_SMOKE=1 .local-venv/social/bin/python \
  -m registration_voice.live_smoke

VOICE_LIVE_SMOKE=1 VOICE_SMOKE_PUBLIC=1 .local-venv/social/bin/python \
  -m registration_voice.server_smoke

# Optional: prove that the public WSS remains usable beyond the old 60 seconds.
VOICE_LIVE_SMOKE=1 VOICE_SMOKE_PUBLIC=1 VOICE_SMOKE_HOLD_SECONDS=65 \
  .local-venv/social/bin/python -m registration_voice.server_smoke

# Optional: verify that two utterances stay on one Gemini Live connection.
VOICE_LIVE_SMOKE=1 VOICE_SMOKE_PUBLIC=1 VOICE_SMOKE_TURNS=2 \
  .local-venv/social/bin/python -m registration_voice.server_smoke
```

The second command requires the complete Server to be running through
`Server/start_all.sh` and verifies the fixed public HTTPS/WSS reverse proxy.
