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
│  XIAO ESP32-S3        │               │  └──────▲───────┘        └───────┬────────┘  │
│  livekit/client-sdk-  │               │        │ health/token      runtime   │
└───────────────────────┘               │        ▼                   config +   │
                                        │  ┌──────────────┐    audit events     │
┌───────────────────────┐   WebRTC      │  │ console      │◀───────────────────┘
│ Browser (console UI / │◀──(via lk)───▶│  │ (FastAPI UI  │     ┌─────────────────────┐
│ Talk tab)             │               │  │  + REST API, │     │ user-configured     │
└───────────────────────┘               │  │  SQLite)     │     │ MCP servers         │
                                        │  └──────────────┘     │ (UI-managed, or     │
                                        │                       │ MCP_SERVERS_JSON)   │
                                        │                       └─────────────────────┘
                                        │                                              │
                                        │  outbound: ElevenLabs STT/TTS, OpenAI LLM,   │
                                        │  Home Assistant MCP (/api/mcp), custom MCP   │
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
                                    ├─ turn detector (en or multilingual, auto by LANGUAGE)
                                    ├─ LLM (OpenAI gpt-4.1-mini / any OpenAI-compatible)
                                    │    ├─ function tools: get_current_time,
                                    │    │   set_timer, cancel_timer, list_timers
                                    │    └─ MCP tools: home-assistant + your servers
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
  (`HOME_ASSISTANT_URL`/`TOKEN`, generic `MCP_SERVERS_JSON` — e.g. a weather
  server) into `mcp.MCPToolset` entries, using `MCPServerHTTP` (streamable
  HTTP / SSE) with per-server headers. A failing MCP server logs an error but
  does not prevent the agent from starting.
- **Language**: `LANGUAGE` (default `de`) drives the system prompt, the
  ElevenLabs STT/TTS language, the built-in skill strings (`agent/i18n.py`)
  and the turn-detector choice (English model for `en`, multilingual model
  for `de`, VAD-only endpointing otherwise).

## Component choices (verification result)

| Layer | Choice | Notes |
|---|---|---|
| RTC transport | LiveKit Server v1.9 (self-hosted, host networking) | LAN-first; TURN for remote (docs) |
| Device client | `livekit/client-sdk-esp32` v0.3.10 (ESP32-S3, vendored in `firmware/components/livekit`) | `minimal` example as firmware base, XVF3800 board bring-up ported from Seeed's Agora client |
| Agent framework | LiveKit Agents 1.7 (Python) | replaces vocode-core (unmaintained, incompatible API, no MCP, no ElevenLabs STT) |
| STT | ElevenLabs `scribe_v2_realtime` | streaming, 90+ languages |
| TTS | ElevenLabs `eleven_turbo_v2_5` | streaming, voice id configurable |
| LLM | OpenAI-compatible | `gpt-4.1-mini` default; Ollama/vLLM via `OPENAI_BASE_URL` |
| Home control | Home Assistant **MCP Server** integration | streamable HTTP `/api/mcp` + long-lived token |
| Weather | Any weather MCP server (user-configured) | register via `MCP_SERVERS_JSON` |
| Language | `LANGUAGE` env (default `de`, `en` built in) | prompt, ElevenLabs STT/TTS, built-in skills, turn detector |

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

## Web console & audit trail

The `console` service (FastAPI + SQLite in the `console-data` volume) is the
control plane around the agent:

- **Runtime configuration**: effective settings = env defaults + UI
  overrides (SQLite). The agent fetches them via `GET /internal/config`
  (bearer token derived from `LIVEKIT_API_SECRET` or
  `CONSOLE_INTERNAL_TOKEN`) at every session start, so UI changes apply
  without restarts. Console-managed MCP servers shadow
  `MCP_SERVERS_JSON`/Home Assistant entries with the same id.
- **Diagnostics**: the agent reports sessions, device joins/leaves, tool
  calls, timer events, errors and heartbeats via `POST /internal/events`
  (batched every 2 s, dropped on failure - never blocks the voice
  pipeline). The console enriches these with live LiveKit server API data
  (rooms/participants) and per-MCP-server probes.
- **Audit trail**: append-only event log with configurable retention
  (`diagnostics_history_days`, hourly cleanup). Transcripts (utterances and
  replies) are stored only when explicitly enabled - off by default,
  enforced on both sides. Login attempts and configuration changes (who,
  what) are recorded too.

Details and the API surface: `docs/console.md`.

## Security model

- LiveKit API key/secret only in `.env` on the server; devices get **scoped
  JWTs** (room-restricted, time-limited) minted by `scripts/mint_token.py`
  or the console (audited).
- Console access is protected by local credentials (`UI_EMAIL`/`UI_PASSWORD`,
  constant-time compare, HttpOnly signed cookies + CSRF header) or SSO via
  OIDC; the agent/console internal API uses a separate bearer token.
  Transcript storage is opt-in; the audit trail stores metadata only by
  default.
- Home Assistant access uses a **long-lived token** with least privilege:
  only entities *exposed to assistants* in HA are controllable (HA-side
  access control), and control can be disabled in the MCP integration.
- Everything stays on the LAN except: ElevenLabs (STT/TTS audio), OpenAI
  (text) and any cloud MCP servers you configure (e.g. weather). To keep
  audio local, see
  `docs/configuration.md` (local STT/TTS options) — the pipeline is
  provider-pluggable.
- For internet exposure use the `tls` profile (Caddy, wss://) **plus** TURN;
  rotate `LIVEKIT_API_SECRET` and keep it out of git (`.gitignore`).

## Device firmware

`firmware/` is a self-contained ESP-IDF project for the reSpeaker XVF3800
(XIAO ESP32-S3), based on the LiveKit SDK's `minimal` example with the
XVF3800 board bring-up ported from Seeed's own XIAO client
(`reference-projects/XVF3800-esp32-client-agora`):

```
                        ┌───────────────────── XIAO ESP32-S3 ─────────────────────┐
XMOS XU316 ── I2S ──▶  │ capture: 16 kHz/2ch/32-bit wire format pinned            │
(4-mic, AEC,  NS)      │   └─▶ capture sink: bits 32→16, ch 2→1 → Opus 16 kHz mono │
         ◀── I2S ──    │ render: Opus → PCM → resample ch 1→2, bits 16→32         │
 AIC3104 (speaker)     │   └─▶ I2S TX 16 kHz/2ch/32-bit                           │
                       │ I2C (GPIO5/6): AIC3104 output unmute at boot             │
                       └───────────────────────────────────────────────────────────┘
```

- **AEC/beamforming stay in the XMOS**: the ESP32 publishes the XMOS's
  already-processed mic signal - no software AEC on the device.
- **Format integrity**: the capture source pins its caps to the XMOS wire
  format so the I2S bus is never reconfigured; all conversion to the Opus
  track happens in the capture sink, all conversion back in the renderer.
- **Vendored SDK** (`firmware/components/livekit`, v0.3.10): carries one
  local fix - the signaling WebSocket buffer is raised 20 KB → 64 KB, which
  otherwise breaks joining with an agent in the room (`JoinResponse`
  fragmentation, upstream livekit/client-sdk-esp32#86).
- Hardware details, build/flash steps and troubleshooting:
  `firmware/README.md`.

### Wake word (planned, not yet implemented)

The current firmware publishes continuously (open mic). Planned: an
**on-device wake word with connected standby** - the room stays joined, the
published track is gated to silence until the wake phrase is heard (ESP-SR
WakeNet on the ESP32-S3), pre-wake audio is flushed on wake, a local chime
plays and an Echo-style follow-up window re-arms the gate. Only the *audio
leaving the device* would be gated; room lifecycle, timer announcements and
`assistant.event` data messages are unaffected. Stock WakeNet models are
English/Chinese; custom phrases (e.g. German) need Espressif's WakeNet
customization, a microWakeWord model, or LiveKit's
[`livekit-wakeword`](https://github.com/livekit/livekit-wakeword) once its
ESP32 runtime ships.

Alternatives considered:

| Option | Verdict |
|---|---|
| **A. Disconnected standby** (connect on wake, `hello-wakeword` style) | Max privacy, but +0.5-1.5 s wake latency (WebRTC connect + agent dispatch), re-triggers the greeting per session, and timer announcements would be missed while disconnected. |
| **B. Connected standby (planned)** | No added latency, greeting stays a boot event, timer events keep working. |
| **C. Agent-side transcript filter** | No firmware change, but all audio is always streamed to cloud STT (privacy/cost), and turn-taking still triggers. |
| **D. Host-side gate** (e.g. [livekit/livekit-wakeword](https://github.com/livekit/livekit-wakeword) in the agent before STT) | Zero-firmware custom phrases (multilingual, Apache-2.0, 100× fewer false positives than vanilla openWakeWord), but chime/LED are triggered from the host (+50-250 ms, host dependency). Good stopgap; its models are the planned future on-device engine. |


