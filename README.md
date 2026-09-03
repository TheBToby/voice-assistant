# Self-Hosted AI Voice Assistant (reSpeaker XVF3800 + LiveKit)

A low-latency, fully self-hosted voice assistant in the style of an Amazon Echo:

- **Hardware**: Seeed Studio [reSpeaker XVF3800](https://wiki.seeedstudio.com/respeaker_xvf3800_introduction/)
  (4-mic array with on-chip AEC/beamforming) + XIAO ESP32-S3, connected via the
  official [LiveKit ESP32 SDK](https://github.com/livekit/client-sdk-esp32)
- **Transport**: self-hosted [LiveKit Server](https://github.com/livekit/livekit) (WebRTC, Opus)
- **Agent**: [LiveKit Agents](https://github.com/livekit/agents) (Python) voice pipeline
- **STT/TTS**: ElevenLabs (Scribe v2 realtime STT, Turbo v2.5 TTS)
- **LLM**: OpenAI-compatible API (OpenAI by default; Ollama/vLLM/LM Studio work via `OPENAI_BASE_URL`)
- **Skills & integrations**: MCP servers — Home Assistant (official MCP Server
  integration), a bundled weather MCP server (Open-Meteo, no API key), and any
  custom MCP server via a single env var
- **Built-in skills**: current time/date, Echo-style countdown timers (with
  spoken announcements), plus everything your MCP servers provide

## Why not vocode-core? (architecture verification)

The original plan proposed `vocode-core` as the agent framework. Verification
showed it cannot support this stack:

| Requirement | vocode-core | LiveKit Agents 1.7 (chosen) |
|---|---|---|
| LiveKit transport | ⚠️ Legacy demo only, uses the **removed** livekit-agents v0.x API (`JobRequest`) | ✅ Native (it *is* the LiveKit agent framework) |
| ElevenLabs STT | ❌ (batch transcribers only; no ElevenLabs STT) | ✅ Streaming `scribe_v2_realtime` |
| ElevenLabs TTS | ✅ | ✅ |
| MCP / Home Assistant | ❌ No MCP support | ✅ First-class `MCPToolset` |
| Maintenance | ❌ Last release 2024-06, seeking maintainers | ✅ Actively developed |
| ESP32 client | — | ✅ Official `livekit/client-sdk-esp32` (ESP32-S3) |

Everything else from the original plan (self-hosted LiveKit, ElevenLabs, ESP32
client SDK, MCP-based Home Assistant + weather) is supported as proposed and
implemented here. See `docs/architecture.md` for details.

## Repository layout

```
├── docker-compose.yml        # full stack (host networking, LAN-first)
├── .env.example              # all configuration (copy to .env)
├── livekit/livekit.yaml      # LiveKit server config (keys come from .env)
├── agent/                    # voice pipeline (LiveKit Agents)
│   ├── main.py               # entrypoint (worker)
│   ├── assistant.py          # Agent + built-in skills (time, timers)
│   ├── config.py             # env-driven config & MCP server registry
│   ├── timers.py, clock.py   # pure-logic skill modules (unit tested)
│   └── Dockerfile
├── services/weather-mcp/     # bundled MCP server (Open-Meteo) - skill template
├── scripts/
│   ├── mint_token.py         # mint access tokens for devices/browsers
│   └── smoke_test.py         # end-to-end smoke test (WebRTC round trip)
├── tests/
│   ├── unit/                 # host-run unit tests (pytest)
│   └── web/index.html        # browser test client (livekit-client)
├── caddy/Caddyfile           # optional TLS proxy (profile "tls")
└── docs/                     # architecture, deployment, configuration,
                              # ESP32/XVF3800 firmware guide, testing guide
```

## Quickstart (LAN deployment)

Prereqs: Linux host with Docker, devices on the same network.

```bash
# 1. Configure
cp .env.example .env
$EDITOR .env                # set LIVEKIT_API_SECRET, ELEVEN_API_KEY, OPENAI_API_KEY,
                            # DEFAULT_LOCATION (your city), TZ

# 2. Start the stack
docker compose up -d --build

# 3. Verify the LiveKit server is up
curl http://localhost:7880/          # -> OK

# 4. Watch the agent connect
docker compose logs -f agent         # "assistant ready"

# 5. Smoke-test the full audio path
docker compose --profile smoke run --rm smoke
```

Talk to it from any of:

- **reSpeaker XVF3800** — flash the LiveKit firmware, see
  `docs/esp32-xvf3800.md` (token: `make token ID=respeaker-1 ROOM=home`)
- **Browser** — `docker compose --profile web up -d webtest`, open
  `http://localhost:8080`, paste URL + token
- **Terminal** — run the agent locally in console mode (see `docs/testing.md`)

Example things to say:

> "What time is it?" · "Set a pizza timer for 7 minutes" · "What's the weather?"
> "Turn off the kitchen light" (Home Assistant) · "Cancel the pizza timer"

## Documentation

| Doc | Content |
|---|---|
| `docs/architecture.md` | System design, data flow, latency budget, security model |
| `docs/deployment.md` | Step-by-step deployment, TLS/public access, autostart, troubleshooting |
| `docs/configuration.md` | Every env var, swapping LLM/providers, adding MCP servers & skills |
| `docs/esp32-xvf3800.md` | Flashing the XVF3800/XIAO ESP32-S3 with the LiveKit client firmware |
| `docs/testing.md` | Unit tests, smoke test, browser client, console mode |

## Latency notes

The pipeline is optimized for the "say it, get an answer" feel:

- Opus/WebRTC transport (~50–150 ms on LAN), XMOS AEC/beamforming on-device
- Streaming ElevenLabs Scribe STT (partial transcripts as you speak)
- Silero VAD + English turn detector for natural turn-taking
- `preemptive_generation=True` (TTS starts before the LLM finishes its sentence)
- Function tools execute locally (µs) — only weather/HA calls hit the network

Measured end-to-end on LAN with `gpt-4.1-mini`: typically **~0.8–1.5 s** from
end-of-speech to first audio out.

## Known limitations / roadmap

- **No on-device wake word yet** — the agent is always listening (VAD-based
  turn-taking), like Echo in "follow-up" mode. Wake-word options are listed in
  `docs/architecture.md`.
- The LiveKit ESP32 SDK is in **developer preview** (APIs may change).
- Turn detector is English-only (set `ENABLE_TURN_DETECTOR=false` otherwise).
