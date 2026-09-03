# Configuration guide

All configuration lives in `.env` (see `.env.example` for the annotated list).
The agent reads it via `env_file` in docker-compose; changing values requires
`docker compose up -d --force-recreate agent` (or `make restart-agent`).

## Variables

### LiveKit
| Variable | Default | Description |
|---|---|---|
| `LIVEKIT_API_KEY` | `devkey` | API key shared by server + agent + token tool |
| `LIVEKIT_API_SECRET` | — | matching secret (required) |
| `LIVEKIT_URL` | `ws://localhost:7880` | where the agent worker connects |
| `PUBLIC_LIVEKIT_WS_URL` | — | convenience URL printed by `scripts/mint_token.py` |

### ElevenLabs (STT/TTS)
| Variable | Default | Description |
|---|---|---|
| `ELEVEN_API_KEY` | — | required |
| `STT_MODEL` | `scribe_v2_realtime` | streaming STT; use `scribe_v1` for batch |
| `TTS_MODEL` | `eleven_turbo_v2_5` | e.g. `eleven_flash_v2_5` for even lower latency |
| `TTS_VOICE_ID` | `JBFqnCBsd6RMkjVDRZzb` | pick any voice from your ElevenLabs library |

### LLM
| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | — | required unless using a local endpoint |
| `OPENAI_BASE_URL` | — | e.g. `http://host.docker.internal:11434/v1` for Ollama |
| `LLM_MODEL` | `gpt-4.1-mini` | any OpenAI-compatible model name |

**Local LLM example (Ollama):**
```bash
ollama pull llama3.1:8b
# .env:
OPENAI_BASE_URL=http://host.docker.internal:11434/v1
OPENAI_API_KEY=ollama          # any non-empty string
LLM_MODEL=llama3.1:8b
```
(The agent container runs with host networking, so `localhost` also works.)

### Persona & built-in skills
| Variable | Default | Description |
|---|---|---|
| `ASSISTANT_NAME` | `Atlas` | used in the default prompt |
| `ASSISTANT_INSTRUCTIONS` | built-in prompt | full override of the system prompt |
| `GREETING` | `Voice assistant ready.` | spoken on session start; empty = silent |
| `ENABLE_TURN_DETECTOR` | `true` | English turn-detection model; `false` for other languages |
| `DEFAULT_LOCATION` | — | default city for weather tools |
| `WEATHER_UNITS` | `metric` | `metric` or `imperial` |

### MCP servers
| Variable | Description |
|---|---|
| `HOME_ASSISTANT_URL` | e.g. `http://homeassistant.local:8123` (`/api/mcp` appended automatically) |
| `HOME_ASSISTANT_TOKEN` | HA long-lived access token |
| `WEATHER_MCP_URL` | default `http://localhost:8100/mcp`; empty disables |
| `MCP_SERVERS_JSON` | JSON list for any extra MCP servers, see below |

## Home Assistant setup

1. Home Assistant → **Settings → Devices & services → Add Integration →
   "Model Context Protocol Server"** (available since HA 2025.2). Leave
   "Control Home Assistant" enabled if the assistant may change things.
2. Expose entities: **Settings → Voice assistants → Expose** — only exposed
   entities are visible to the assistant.
3. Create a token: click your profile → **Security → Long-Lived Access
   Tokens → Create Token**.
4. In `.env`:
   ```
   HOME_ASSISTANT_URL=http://homeassistant.local:8123
   HOME_ASSISTANT_TOKEN=eyJhbGci...
   ```
5. `make restart-agent`. On success the agent log shows
   `MCP server registered: home-assistant -> http://.../api/mcp`.

Try: *"turn off the kitchen light"*, *"what's the temperature in the living
room?"*, *"is the front door locked?"*. Troubleshooting: HTTP 404 = integration
not configured; HTTP 401 = wrong token.

## Adding any MCP server (no code changes)

```bash
# .env  (headers are optional)
MCP_SERVERS_JSON=[{"id":"music","url":"http://music-mcp.local:9000/mcp"},{"id":"calendar","url":"https://example.com/mcp","headers":{"Authorization":"Bearer xyz"}}]
```

Rules: each entry needs `url`; optional `id` (defaults to `mcp-<n>`) and
`headers`. Duplicate ids keep the first definition. Streamable HTTP and SSE
endpoints are both supported by `MCPServerHTTP`.

## Adding a new skill (code)

Two options:

1. **Function tool** (fastest): add a `@function_tool` method in
   `agent/assistant.py` — see `set_timer` for the pattern (docstring becomes
   the LLM-facing description).
2. **MCP server** (decoupled, restartable independently): copy
   `services/weather-mcp/` — a FastMCP app whose `@mcp.tool()` functions are
   exposed to the agent automatically. Add it to `docker-compose.yml` and
   register its URL via `MCP_SERVERS_JSON` or a dedicated env pair.

## Ports (host networking)

| Port | Service |
|---|---|
| 7880/tcp | LiveKit signaling (ws) |
| 7881/tcp | LiveKit WebRTC TCP fallback |
| 50000–50200/udp | LiveKit WebRTC media |
| 8100/tcp | weather-mcp (for curl tests) |
| 8080/tcp | webtest client (profile `web`) |
| 80/443/tcp | Caddy (profile `tls`) |
