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
| Device client | `livekit/client-sdk-esp32` (ESP32-S3) | `voice_agent` example as firmware base |
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

## Wake word

Implemented as an **on-device wake word with connected standby** (overlay in
`firmware/`, applied to the `voice_agent` firmware - see `firmware/README.md`
and `docs/esp32-xvf3800.md` §6):

```
                        ┌───────────────────── XIAO ESP32-S3 ─────────────────────┐
XMOS XU316 ── I2S ──▶  │ esp_capture AEC source ──▶ gating wrapper               │
(4-mic, AEC,  NS)      │   (mono 16 kHz PCM)        ├─▶ WakeNet detector (esp-sr) │
                       │                            ├─▶ pre-wake ring buffer      │
                       │                            └─▶ ARMED: silence → Opus     │
                       │                                ACTIVE: buffer + live     │
                       │ state: ARMED ──wake──▶ ACTIVE ──(10 s quiet)──▶ ARMED     │
                       │ feedback: local chime + wake_word_led_* hooks             │
                       └───────────────────────────────────────────────────────────┘
```

- **Room lifecycle unchanged**: the device joins at boot (agent greets once),
  timer announcements and `assistant.event` data messages keep working. Only
  the *audio leaving the device* is gated.
- **On wake** the pre-wake buffer (default 1.5 s) is flushed ahead of the live
  audio, so the wake phrase itself reaches the STT; a procedurally generated
  chime plays locally; after a 10 s quiet window the gate re-arms.
- **Engine seam is swappable** (`firmware/main/wake_word_engine.h`): ESP-SR
  WakeNet ships as default; microWakeWord (TFLite-Micro) or livekit-wakeword
  models (once LiveKit ships an ESP32 runtime) drop in without touching the
  capture path. Stock models are English/Chinese; custom phrases (e.g. German)
  need Espressif's WakeNet customization, a microWakeWord model, or the
  livekit-wakeword route.
- **Graceful degradation**: if the engine fails to init (no model partition,
  format mismatch, OOM) the firmware falls back to always-listening.

Alternatives considered (see the git history of this section for details):

| Option | Verdict |
|---|---|
| **A. Disconnected standby** (connect on wake, `hello-wakeword` style) | Max privacy, but +0.5-1.5 s wake latency (WebRTC connect + agent dispatch), re-triggers the greeting per session, and timer announcements would be missed while disconnected. |
| **B. Connected standby (implemented)** | No added latency, greeting stays a boot event, timer events keep working, graceful degradation. |
| **C. Agent-side transcript filter** | No firmware change, but all audio is always streamed to cloud STT (privacy/cost), and turn-taking still triggers. |
| **D. Host-side gate** (e.g. [livekit/livekit-wakeword](https://github.com/livekit/livekit-wakeword) in the agent before STT) | Zero-firmware custom phrases (multilingual, Apache-2.0, 100× fewer false positives than vanilla openWakeWord), but chime/LED are triggered from the host (+50-250 ms, host dependency). Good stopgap; its models are the planned future on-device engine. |

