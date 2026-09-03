# Testing guide

Three layers, from fast to end-to-end:

## 1. Unit tests (host, no services needed)

```bash
pip install pytest
make unit-tests          # python3 -m pytest tests/unit -v
```

Covers the pure-logic modules: timer service (expiry, cancel, auto-names,
duplicate rejection), clock formatting, agent config (MCP registry, HA URL
join, validation), and smoke-test helpers (WAV chunking, RMS). The tests stub
`livekit`/`httpx`, so nothing heavy is installed on the host.

## 2. Smoke test (running stack, no human required)

```bash
make smoke            # builds the test image, then runs the checks
# equivalent:
#   docker compose --profile smoke build smoke
#   docker compose --profile smoke run --rm smoke
```

What it checks (exit code 0 = all pass):

1. `livekit-http` — the LiveKit endpoint answers on port 7880
2. `room-join` — a test participant can connect with a minted token
3. `agent-joins-and-speaks` — the agent auto-dispatches into the room and
   returns **non-silent** audio (it speaks `GREETING`), proving WebRTC + TTS
   + the return path

> Before adding your real API keys, result 3 intentionally fails with
> *"agent sent N frames but they were all silent"* — that proves the agent
> joined and published its track, while TTS could not run without a valid
> `ELEVEN_API_KEY`. After filling in `.env`, it turns green.

Full speech round-trip (STT → LLM → TTS), optional:

```bash
# a) synthesize the test phrase with your ElevenLabs key, or
docker compose --profile smoke run --rm smoke \
  python /app/scripts/smoke_test.py --tts-text "hello there"
# b) or supply your own speech WAV (16-bit PCM mono):
docker compose --profile smoke run --rm smoke \
  python /app/scripts/smoke_test.py --wav /app/tests/assets/hello.wav
```

Then say things out loud and watch the agent reason in the logs:

```bash
docker compose logs -f agent
```

## 3. Browser test client (interactive, from a laptop)

```bash
docker compose --profile web up -d webtest   # nginx serving tests/web
```

The LiveKit client SDK is vendored at `tests/web/livekit-client.umd.js`
(pinned v2.22.2 UMD build) and served same-origin — the test client needs no
CDN access.

Open `http://<host-ip>:8080`, paste the URL (`ws://<host-ip>:7880`) and a
token (`make token ID=web-1 ROOM=home`), click **Connect & talk**.

> Microphone access needs a *secure context*: browse from `localhost`, via
> the `tls` profile (https://), or run Chrome with
> `--unsafely-treat-insecure-origin-as-secure=http://<host-ip>:8080`.
> The data-message log line shows `assistant.event` payloads (e.g. timer
> expiry) — the same messages the device can consume.

### In a Coder workspace (remote, no LAN access)

When the stack runs inside a Coder workspace, use the Coder proxies instead
of LAN IPs — the wildcard HTTPS access also provides the secure context the
microphone needs. The workspace template exposes LiveKit signaling via a
`coder_app` (slug `livekit-7880`, share = owner):

| What | URL |
|---|---|
| Web test client (port 8080) | `https://8080--main--<workspace>--<owner>.<coder-domain>/` (VS Code port forwarding) |
| LiveKit signaling (port 7880) | `wss://livekit-7880--main--<workspace>--<owner>.<coder-domain>` (dashboard app "LiveKit Signaling (7880)") |

For this deployment (`coder.baechtold.rocks`, workspace `voice-assistant`,
owner `tobias-baechtold`):

1. Start the test client:
   `docker compose --profile web up -d webtest`
2. Mint a token — `PUBLIC_LIVEKIT_WS_URL` in `.env` already points at the
   proxied signaling URL, so the printed URL hint is correct:
   `make token ID=web-1 ROOM=home`
3. Open the web test client, paste the signaling URL
   `wss://livekit-7880--main--voice-assistant--tobias-baechtold.coder.baechtold.rocks`
   and the token, click **Connect & talk** — then ask: *"Wie spät ist es?"*

Notes: app-proxy URLs require your Coder login (share = owner). The 7880
`coder_app` was added to the `docker` template — apply it to the workspace
once via a workspace update/restart (dashboard or `coder update`).


## 4. Console mode (agent on your dev machine)

Runs the agent with your laptop mic/speakers — handy for prompt iteration:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r agent/requirements.txt
cd agent
LIVEKIT_URL=ws://<host>:7880 LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=... \
  python main.py console
```

## What to test manually (feature checklist)

- [ ] "What time is it?" / "What's the date?" — answers via `get_current_time`
- [ ] `LANGUAGE=de` (default): greeting "Sprachassistent bereit.", "Wie spät
  ist es?" answered with 24-hour time ("Es ist ... Uhr"), timer confirmations
  and expiry announcements in German
- [ ] "Set a pizza timer for 7 minutes" — confirmation, then spoken expiry
  announcement + `assistant.event` data message
- [ ] "Set another timer for 2 minutes called laundry" · "list my timers" ·
  "cancel the laundry timer"
- [ ] "Wie wird das Wetter?" / "What's the weather?" — via the weather MCP
  server you configured in `MCP_SERVERS_JSON`
- [ ] Home Assistant: "turn off the kitchen light", "is the bedroom window
  open?" (requires HA MCP configured + entities exposed)
- [ ] Interruption: start speaking while the agent talks — it should stop and
  listen (AEC + VAD + turn detection)
- [ ] Smoke test green after every config change
