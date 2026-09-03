# Architecture

## Overview

```
                                        ┌──────────────────────────────────────────────┐
                                        │  Docker host (Linux, LAN)                    │
                                        │                                              │
┌───────────────────────┐   WebRTC      │  ┌──────────────┐        ┌────────────────┐  │
│ reSpeaker XVF3800     │   Opus/SRTP   │  │ LiveKit      │◀──────▶│ agent          │  │
│  XMOS XU316           │◀────wss/ws───▶│  │ Server       │  ws:// │ (livekit-      │  │
│  (AEC, NS, beamform)  │               │  │ (self-hosted)│        │  agents 1.7)   │  │
│  XIAO ESP32-S3        │               │  └──────────────┘        └───────┬────────┘  │
│  livekit/client-sdk-  │               │        ▲                         │           │
│  esp32 firmware       │               │        │ health/token            │ MCP       │
└───────────────────────┘               │        ▼                         ▼           │
                                        │  ┌──────────────┐    ┌─────────────────────┐ │
┌───────────────────────┐   WebRTC      │  │ webtest      │    │ weather-mcp         │ │
│ Browser test client   │◀──(via lk)───▶│  │ (nginx,      │    │ (FastMCP, Open-     │ │
│ tests/web/index.html  │               │  │  optional)   │    │  Meteo, /mcp)       │ │
└───────────────────────┘               │  └──────────────┘    └─────────────────────┘ │
                                        │                                              │
                                        │  outbound: ElevenLabs STT/TTS, OpenAI LLM,   │
                                        │  Home Assistant MCP (/api/mcp), Open-Meteo   │
                                        └──────────────────────────────────────────────┘
```

## Voice pipeline (agent service)

Implemented with the LiveKit Agents framework (`livekit-agents[mcp,elevenlabs,
openai,silero,turn-detector]~=1.7.1`):

```
mic (XVF3800) ─ Opus/WebRTC ─ LiveKit room ─┐
                                            ▼
                                    AgentSession
                                    ├─ VAD (silero) .............. end-of-turn detection
                                    ├─ STT (ElevenLabs Scribe v2 realtime, streaming)
                                    ├─ turn detector (English model, optional)
                                    ├─ LLM (OpenAI gpt-4.1-mini / any OpenAI-compatible)
                                    │    ├─ function tools: get_current_time,
                                    │    │   set_timer, cancel_timer, list_timers
                                    │    └─ MCP tools: home-assistant, weather, custom
                                    ├─ TTS (ElevenLabs Turbo v2.5, streaming)
                                    └─ preemptive_generation=True
                                            │
                            speaker (XVF3800) ◀─ Opus/WebRTC ─┘
```

Key behaviors:

- **Automatic dispatch**: the agent worker joins *every* new LiveKit room, so
  any participant (ESP32, browser, smoke test) gets an assistant instantly.
- **Timers**: `set_timer` creates an `asyncio` task in `agent/timers.py`. On
  expiry the agent speaks the announcement (`session.say`) even while idle and
  publishes an `assistant.event` data message (topic `assistant.event`) so
  devices can blink LEDs / show UI.
- **MCP**: servers are resolved at session start from the environment
  (`HOME_ASSISTANT_URL`/`TOKEN`, `WEATHER_MCP_URL`, generic `MCP_SERVERS_JSON`)
  into `mcp.MCPToolset` entries, using `MCPServerHTTP` (streamable HTTP / SSE)
  with per-server headers. A failing MCP server logs an error but does not
  prevent the agent from starting.

## Component choices (verification result)

| Layer | Choice | Notes |
|---|---|---|
| RTC transport | LiveKit Server v1.9 (self-hosted, host networking) | LAN-first; TURN for remote (docs) |
| Device client | `livekit/client-sdk-esp32` (ESP32-S3) | `voice_agent` example as firmware base |
| Agent framework | LiveKit Agents 1.7 (Python) | replaces vocode-core (unmaintained, incompatible API, no MCP, no ElevenLabs STT) |
| STT | ElevenLabs `scribe_v2_realtime` | streaming, 90+ languages |
| TTS | ElevenLabs `eleven_turbo_v2_5` | streaming, voice id configurable |
| LLM | OpenAI-compatible | `gpt-4.1-mini` default; Ollama/vLLM via `OPENAI_BASE_URL` |
| Home control | Home Assistant **MCP Server** integration | streamable HTTP `/api/mcp` + long-lived token |
| Weather | Bundled FastMCP server (Open-Meteo) | no API key; template for new skills |

## Latency budget (LAN, typical)

| Segment | Latency |
|---|---|
| Device capture + Opus + network | 50–150 ms |
| VAD end-of-turn + turn detector | 200–500 ms |
| STT partials → final | ~150–300 ms after speech end |
| LLM first token (gpt-4.1-mini) | 300–600 ms |
| TTS first audio chunk | 150–300 ms |
| Return transport | 50–150 ms |
| **Speech-end → first audio** | **~0.8–1.5 s** |

`preemptive_generation` and streaming STT/TTS keep the pipeline saturated; the
greeting path (`session.say`) bypasses the LLM entirely for instant first
contact.

## Security model

- LiveKit API key/secret only in `.env` on the server; devices get **scoped
  JWTs** (room-restricted, time-limited) minted by `scripts/mint_token.py`.
- Home Assistant access uses a **long-lived token** with least privilege:
  only entities *exposed to assistants* in HA are controllable (HA-side
  access control), and control can be disabled in the MCP integration.
- Everything stays on the LAN except: ElevenLabs (STT/TTS audio), OpenAI
  (text), Open-Meteo (weather). To keep audio local, see
  `docs/configuration.md` (local STT/TTS options) — the pipeline is
  provider-pluggable.
- For internet exposure use the `tls` profile (Caddy, wss://) **plus** TURN;
  rotate `LIVEKIT_API_SECRET` and keep it out of git (`.gitignore`).

## Wake word (roadmap)

The current firmware is always-listening with VAD turn-taking. Options to add
an Echo-like wake word later:

1. **ESP32-side**: wake-word model on the XIAO ESP32-S3 (e.g. ESP-SR
   "WakeNet") gating `room.connect()` — keeps the server out of the loop.
2. **Agent-side**: filter utterances in the agent (e.g. via
   `on_user_input_transcript`) until a wake phrase is seen — simplest, but
   audio still streams and VAD still triggers turns.

