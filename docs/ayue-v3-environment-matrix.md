# Ayue V3 Environment Matrix

The table records variable names and public defaults only. Secret values from `social.env` and existing Server configuration must never be copied into this document.

## Loading boundaries

| Service | Working directory | Dotenv path | Isolated Python |
|---|---|---|---|
| Social / Public Ayue V3 / Private Ayue V2 | `Server/ayue_for_demo/social_demotest` | `Server/ayue_for_demo/social_demotest/.env` through `load_dotenv()` | `Server/ayue_for_demo/.local-venv/social/bin/python` |
| Matchmaker | `Server/ayue_for_demo/matchmaker_agent` | Explicit `Server/ayue_for_demo/matchmaker_agent/.env` | `Server/ayue_for_demo/.local-venv/matchmaker/bin/python` |
| Risk backend | `Server/risk_backend` | Existing risk configuration; not copied into Ayue dotenv files | Existing Server environment |
| Guardrail classifier | standalone `llama-server` | `Server/.env` model path, alias, port, timeout and retry budget | Native binary configured by `GUARDRAIL_SERVER_BIN` |

## Social service variables

`Required` means the integration cannot provide the associated core capability without a non-empty value. Optional provider features follow their documented failure policy or remain disabled; pair-chat Risk specifically uses `deliver_degraded` when unavailable.

| Variable | Required | Public default | Source preference | Validation |
|---|---:|---|---|---|
| `MONGO_URI` | yes | none | `social.env`, existing Server env | Mongo ping |
| `MONGO_DB_NAME` | yes | `profiling_db` | `social.env`, existing Server env, example | Non-empty and Mongo database selectable |
| `DEMO_DESTRUCTIVE_TOOLS_ENABLED` | no | `off` | `social.env`, example | Boolean; must remain off outside disposable demo data |
| `RISK_SERVICE_URL` | yes for pair chat | `http://127.0.0.1:8001` | existing Server env, example | HTTP endpoint reachable |
| `RISK_TIMEOUT_SEC` | yes for pair chat | `20` | existing Server env, example | Number greater than or equal to 20 |
| `OLLAMA_HOST` | yes | `https://ollama.com` | `social.env`, existing Server env, example | HTTPS/HTTP endpoint reachable |
| `OLLAMA_API_KEY` | yes for configured remote host | none | `social.env`, existing Server env | Non-empty; never printed |
| `OLLAMA_CHAT_MODEL` | yes | `deepseek-v4-flash:cloud` | `social.env`, existing Server env, example | Non-empty |
| `OLLAMA_FAST_CHAT_MODEL` | no | main chat model | example/runtime | Empty means reuse main model |
| `AYUE_OLLAMA_TIMEOUT_SECONDS` | no | `30` | example/runtime | Number from 5 through 120 |
| `AYUE_LOCAL_DEBUG_TRACE` | no | `off` | `social.env`, example | Boolean |
| `AYUE_RUNTIME_MODEL_SETTINGS_TOKEN` | no | empty | deployment secret | Never exposed to Flutter |
| `AYUE_ALLOWED_RUNTIME_MODELS` | no | empty | deployment config | Bounded allowlist |
| `GOOGLE_AI_STUDIO_API_KEY` | yes for embeddings/profile features | none | `social.env`, existing Server env | Non-empty; never printed |
| `GOOGLE_EMBEDDING_MODEL` | yes | `models/gemini-embedding-2` | `social.env`, existing Server env, example | Non-empty |
| `AYUE_DEFAULT_TIMEZONE` | no | `Asia/Taipei` | example/runtime | Valid IANA timezone |
| `AYUE_CALENDAR_STATE_MONGO` | no | `on` | example/runtime | Boolean |
| `AYUE_RELATIONSHIP_REFERENCE_MONGO` | no | implementation default | deployment config | Boolean |
| `AYUE_PROFILE_SKILLS_MODE` | no | `on` | `social.env`, example | Boolean |
| `AYUE_PROFILE_SKILLS_USER_ALLOWLIST` | no | empty | `social.env`, example | Comma-separated owner IDs |
| `TAVILY_API_KEY` | no | empty | `social.env` | Empty disables provider-backed web search |
| `TAVILY_PROJECT` | no | empty | deployment config | Optional provider project label |
| `GIPHY_API_KEY` | no | empty | `social.env` | Empty disables GIF delivery |
| `GIPHY_GIF_ENABLED` | no | `on` | `social.env`, example | Boolean plus key availability |
| `AYUE_MAPS_ENABLED` | no | `on` | example/runtime | Boolean |
| `AYUE_MAPS_MONGO_CACHE` | no | `off` | example/runtime | Boolean |
| `OSM_NOMINATIM_URL` | no | official Nominatim search URL | example/runtime | Public HTTPS URL |
| `OSM_OVERPASS_URL` | no | official Overpass URL | example/runtime | Public HTTPS URL |
| `OSM_OVERPASS_FALLBACK_URL` | no | Kumi Overpass URL | example/runtime | Public HTTPS URL |
| `OSM_USER_AGENT` | yes when maps enabled | `AyueDatingDemo/1.0 (educational demo)` | example/runtime | Non-empty identifiable user agent |
| `AYUE_GOOGLE_PLACE_CARDS_ENABLED` | no | `off` | `social.env`, example | Boolean |
| `AYUE_PUBLIC_PLACE_CARDS_ENABLED` | no | `off` | example/runtime | Boolean; Flutter must support text fallback |
| `GOOGLE_PLACES_SERVER_API_KEY` | only when Google place cards enabled | empty | `social.env` | Non-empty server-restricted key |
| `GOOGLE_MAPS_BROWSER_API_KEY` | only for browser map embed | empty | `social.env` | Non-empty referrer-restricted key |
| `AYUE_GOOGLE_PLACE_PHOTOS_ENABLED` | no | `off` | `social.env`, example | Boolean and quota approval |
| `AYUE_GOOGLE_DISTANCE_MATRIX_ENABLED` | no | `on` | `social.env`, example | Boolean |
| `MATCH_AGENT_CANDIDATE_LIMIT` | no | `3` | example/runtime | Integer 1–10 |
| `MATCH_VECTOR_QUALIFICATION_MIN` | no | `0.55` | example/runtime | Number 0–1 |
| `AYUE_V3_SIMPLE_CHAT_FAST_PATH` | no | `off` | `social.env`, example | Boolean |
| `AYUE_V3_WEB_PLACE_BOOTSTRAP_FAST_PATH` | no | `off` | `social.env`, example | Boolean |
| `AYUE_SUBAGENT_MAX_READS` | no | `3` | `social.env`, example | Bounded positive integer |
| `AYUE_SUBAGENT_MAX_PARALLEL` | no | `2` | example/runtime | Bounded positive integer |

Test-only switches such as `AYUE_TEST_MODE`, `AYUE_RUN_PERSONA_LIVE`, `AYUE_RUN_PRIVATE_SCOPE_LIVE`, and `AYUE_LIVE_PLANNER_SMOKE` are not placed in runtime `.env` files.

## Matchmaker variables

| Variable | Required | Public default | Source preference | Validation |
|---|---:|---|---|---|
| `LLM_API_KEY` | yes | none | existing Server env, `social.env` | Non-empty; never printed |
| `LLM_BASE_URL` | yes | none | existing Server env, `social.env` | HTTP/HTTPS endpoint reachable |
| `LLM_MODEL_ID` | yes | none | existing Server env, `social.env` | Non-empty |
| `NEO4J_URI` | yes | none | existing Server env | Neo4j driver connectivity |
| `NEO4J_USERNAME` | yes | none | existing Server env | Non-empty |
| `NEO4J_PASSWORD` | yes | none | existing Server env | Non-empty; never printed |
| `NEO4J_DATABASE` | yes | `neo4j` | existing Server env, example | Read session can open |
| `MATCH_GLOBAL_RULE_LIMIT` | no | `2` | example/runtime | Integer 0–5 |
| `MATCH_GLOBAL_RULE_CHAR_LIMIT` | no | `30` | example/runtime | Integer 10–140 |
| `MATCH_GLOBAL_RULE_SIMILARITY_THRESHOLD` | no | `0.38` | example/runtime | Number 0–1 |

## Keys intentionally not migrated

The following keys exist in the supplied `social.env` but belong to removed rollout or compatibility paths. Public Ayue is always V3 in the canonical backend, so they are not copied into runtime configuration:

- `AYUE_AGENTIC_USER_ALLOWLIST`
- `AYUE_AGENT_V2_MODE`
- `AYUE_AGENT_V3_MODE`
- `AYUE_AGENT_V3_USER_ALLOWLIST`
- `AYUE_CONVERSATION_COMPACTION_MODE`
- `AYUE_GOOGLE_PLACE_DETAILS_FULL`
- `AYUE_PRIVATE_AGENTIC_MODE`
- `AYUE_PRIVATE_AGENTIC_USER_ALLOWLIST`
- `AYUE_PRIVATE_PUBLIC_CONTINUITY`
- `AYUE_PUBLIC_CONVERSATION_CONTINUITY`
- `AYUE_SUBAGENT_TIMEOUT_MS`
