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

When the stack runs inside a Coder workspace, plain LAN IPs don't work and
the Coder proxy only carries HTTP/WebSocket — **not WebRTC media (UDP)**.
Signal-only access via the app proxy (`wss://livekit-7880--...`, dashboard
app "LiveKit Signaling (7880)", owner-shared: open its URL once in a normal
tab so the auth cookie is set) is good for a health check, but for actual
**voice** use Coder's TCP/UDP tunnel from your laptop:

1. `docker compose --profile web up -d webtest` (workspace)
2. Mint a token (workspace): `make token ID=web-1 ROOM=home` — with
   `PUBLIC_LIVEKIT_WS_URL=ws://localhost:7880` and `NODE_IP=127.0.0.1`
   (both in `.env`), LiveKit advertises loopback ICE candidates
3. On your **laptop** (NOT in the workspace shell — inside the workspace the
   command fails with `bind: address already in use` because LiveKit already
   listens on 7880 there), tunnel signal + media ports. Requires the Coder
   CLI (`curl -L https://coder.com/install.sh | sh`) and
   `coder login https://coder.baechtold.rocks` on the laptop:
   ```bash
   coder port-forward tobias-baechtold/voice-assistant \
     --tcp 7880:7880 --tcp 7881:7881 --udp 50000-50200:50000-50200
   ```
   Keep this running while you test.
4. Open the web test client via the forwarded 8080 port
   (`https://8080--main--<workspace>--<owner>.<coder-domain>/`), enter
   **`ws://localhost:7880`** (resolved on *your* laptop through the tunnel)
   and the token, click **Connect & talk** — ask: *"Wie spät ist es?"*

5. **Chrome/Edge only**: Chromium excludes loopback addresses from WebRTC
   peer connections by default, so ICE toward `127.0.0.1` is silently
   skipped ("could not establish pc connection"). Restart the browser with
   the loopback flag allowed:
   ```bash
   # macOS (quit Chrome completely first):
   open -na "Google Chrome" --args --allow-loopback-in-peer-connection
   # Linux:
   google-chrome --allow-loopback-in-peer-connection
   ```
   Firefox and Safari do not need this flag.

Notes: `NODE_IP` (livekit-server `$NODE_IP`) must be empty for LAN
deployments so the server advertises its real address; 127.0.0.1 is only
correct together with the tunnel. The `make token` URL hint reflects
`PUBLIC_LIVEKIT_WS_URL`; the URL field in the client is what counts.


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
