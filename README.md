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
  integration) and any custom MCP server (e.g. weather) — configurable at
  runtime in the web console
- **Web console**: configuration & diagnostics UI with SSO (OIDC) or
  password login — runtime settings, MCP server management, device registry,
  connectivity status and a configurable audit trail
- **Built-in skills**: current time/date, Echo-style countdown timers (with
  spoken announcements), plus everything your MCP servers provide
- **Multi-language**: replies, STT/TTS and built-in skills follow `LANGUAGE`
  (German by default, English built in)

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
client SDK, MCP-based Home Assistant + weather via your own MCP server) is
supported as proposed and implemented here. See `docs/architecture.md` for details.

## Repository layout

```
├── docker-compose.yml        # full stack (host networking, LAN-first)
├── .env.example              # all configuration (copy to .env)
├── livekit/livekit.yaml      # LiveKit server config (keys come from .env)
├── agent/                    # voice pipeline (LiveKit Agents)
│   ├── main.py               # entrypoint (worker; pulls runtime config,
│   │                         #  reports audit events to the console)
│   ├── assistant.py          # Agent + built-in skills (time, timers)
│   ├── audit.py              # best-effort audit reporter → console
│   ├── config.py             # env config + console runtime overrides
│   ├── i18n.py               # localization (German default, English)
│   ├── timers.py, clock.py   # pure-logic skill modules (unit tested)
│   └── Dockerfile
├── ui/                       # web console (configuration & diagnostics)
│   ├── serve.py              # uvicorn launcher
│   ├── app/                  # FastAPI app + pure logic modules (unit tested)
│   ├── static/               # single-page UI (vanilla JS, no build step)
│   │   └── talk/             # browser test client (Talk tab)
│   ├── requirements.txt
│   └── Dockerfile
├── firmware/                   # ESP32 firmware (XVF3800 + XIAO ESP32-S3):
│                               # LiveKit client with bidirectional audio
│                               # (firmware/README.md)
├── scripts/
│   ├── mint_token.py         # mint access tokens for devices/browsers
│   └── smoke_test.py         # end-to-end smoke test (WebRTC round trip)
├── tests/
│   └── unit/                 # host-run unit tests (pytest, agent + console)
├── caddy/Caddyfile           # optional TLS proxy (profile "tls")
└── docs/                     # architecture, console, deployment,
                              # configuration, testing
```

## Quickstart (LAN deployment)

Prereqs: Linux host with Docker, devices on the same network.

```bash
# 1. Configure
cp .env.example .env
$EDITOR .env                # set LIVEKIT_API_SECRET, ELEVEN_API_KEY, OPENAI_API_KEY,
                            # LANGUAGE (default: de), DEFAULT_LOCATION (your city), TZ

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

- **reSpeaker XVF3800** — flash the firmware in `firmware/`, see
  `firmware/README.md` (token: `make token ID=respeaker-1 ROOM=home`,
  or mint tokens in the web console)
- **Browser** — open the console at `http://localhost:8090`, sign in and use
  the **Talk** tab (token is minted for you)
- **Terminal** — run the agent locally in console mode (see `docs/testing.md`)

Example things to say (with `LANGUAGE=de`, the default):

> "Wie spät ist es?" · "Stelle einen Pizza-Timer für 7 Minuten" · "Wie wird das
> Wetter?" (via your configured weather MCP server) · "Mach das Küchenlicht
> aus" (Home Assistant) · "Brech den Pizza-Timer ab"

In English (`LANGUAGE=en`):

> "What time is it?" · "Set a pizza timer for 7 minutes" · "Turn off the
> kitchen light" · "Cancel the pizza timer"

## Documentation

| Doc | Content |
|---|---|
| `docs/architecture.md` | System design, data flow, latency budget, security model |
| `docs/console.md` | Web console: login (SSO/password), settings, MCP servers, diagnostics, audit trail |
| `docs/deployment.md` | Step-by-step deployment, TLS/public access, autostart, troubleshooting |
| `docs/configuration.md` | Every env var, swapping LLM/providers, adding MCP servers & skills |
| `firmware/README.md` | XVF3800/XIAO ESP32-S3 firmware: hardware bring-up, build & flash, token setup, troubleshooting |
| `docs/testing.md` | Unit tests, smoke test, browser client, console mode |

## Latency notes

The pipeline is optimized for the "say it, get an answer" feel:

- Opus/WebRTC transport (~50–150 ms on LAN), XMOS AEC/beamforming on-device
- Streaming ElevenLabs Scribe STT (partial transcripts as you speak)
- Silero VAD + language-matched turn detector (English or multilingual) for
  natural turn-taking
- `preemptive_generation=True` (TTS starts before the LLM finishes its sentence)
- Function tools execute locally (µs) — only MCP tool calls hit the network

Measured end-to-end on LAN with `gpt-4.1-mini`: typically **~0.8–1.5 s** from
end-of-speech to first audio out.

## Known limitations / roadmap

- **On-device wake word** — not yet implemented: the device currently streams
  the (XMOS AEC-processed) microphone continuously, like an open mic.
  Planned: ESP-SR WakeNet on the ESP32-S3 gating the published track
  (connected standby: room stays joined, silence published while armed,
  pre-wake buffer flushed on wake, local chime, Echo-style follow-up window).
  Stock models are English/Chinese; custom phrases (e.g. German) need
  Espressif's WakeNet customization, a microWakeWord model, or LiveKit's
  [`livekit-wakeword`](https://github.com/livekit/livekit-wakeword) once its
  ESP32 runtime ships. See the roadmap in `firmware/README.md`.
- The LiveKit ESP32 SDK is in **developer preview** (APIs may change).
- Built-in skill replies (time, timers) are localized for German (default) and
  English; other `LANGUAGE` values fall back to English strings and VAD-only
  endpointing (set `ENABLE_TURN_DETECTOR=false` to skip the model download).
