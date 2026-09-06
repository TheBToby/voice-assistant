# Deployment guide

## Prerequisites

- Linux host (the compose file uses `network_mode: host` for LAN-native
  WebRTC), Docker ≥ 24 with the compose plugin
- Outbound internet for: Docker Hub, PyPI (first build), ElevenLabs, OpenAI,
  your MCP servers (e.g. weather), and Hugging Face (one-time turn-detector
  model download)
- (For the device) reSpeaker XVF3800 with XIAO ESP32-S3 on the same network

> macOS/Windows or prefer bridge networking? See "Bridge networking" below.

## 1. Configure

```bash
cp .env.example .env
```

Minimum changes:

| Variable | Set to |
|---|---|
| `LIVEKIT_API_SECRET` | any long random string |
| `ELEVEN_API_KEY` | your ElevenLabs API key |
| `OPENAI_API_KEY` | your OpenAI API key |
| `DEFAULT_LOCATION` | your city (default for weather) |
| `LANGUAGE` | `de` (German, default) or `en` — spoken replies, skills, STT/TTS |
| `TZ` | your timezone (correct spoken time) |
| `PUBLIC_LIVEKIT_WS_URL` | `ws://<host-LAN-IP>:7880` (printed by the token helper) |

Home Assistant (optional but recommended): see `docs/configuration.md`.

## 2. Start the stack

```bash
docker compose up -d --build     # livekit + agent
docker compose ps
```

First start downloads the Silero VAD and turn-detector models into the model
cache (named volume `model-cache`, or a host folder of your choice via
`MODEL_CACHE_DIR` — see the "Persistent data locations" block in
`.env.example`; `CADDY_DATA_DIR`/`CADDY_CONFIG_DIR` work the same way for the
`tls` profile); the agent logs `assistant ready` when done:

```bash
docker compose logs -f agent
```

## 3. Verify

```bash
curl http://localhost:7880/                       # -> OK  (LiveKit)
curl http://localhost:8090/healthz                # -> {"ok":true...} (console)
make smoke                                        # or:
docker compose --profile smoke build smoke
docker compose --profile smoke run --rm smoke     # full audio round trip
```

Sign in to the **web console** at `http://<host-ip>:8090` — default login is
`UI_EMAIL` / `UI_PASSWORD` from `.env` (if `UI_PASSWORD` is empty, a one-off
password was printed to `docker compose logs console` on first start). The
dashboard shows LiveKit/agent/MCP connectivity, and every interaction lands
in the audit trail.

The smoke test joins a room, verifies the agent auto-dispatches, and asserts
non-silent agent audio comes back (the greeting). Exit code 0 = PASS.
Before configuring real API keys, the audio check fails with *"all frames
were silent"* — that still proves the agent joins and publishes; it turns
green once `ELEVEN_API_KEY` is set.

## 4. Connect clients

```bash
# device token (flash the firmware in firmware/, see firmware/README.md)
python3 scripts/mint_token.py --identity respeaker-1 --room home
# ...or mint tokens in the console: Devices tab -> "Mint a device token"

# browser: console Talk tab -> http://<host-ip>:8090/talk
```

## Public access (optional, `tls` profile)

For access from outside the LAN:

1. Create a DNS record for `voice.example.com` → your router/public IP, open
   TCP 80/443.
2. In `.env`: `VA_DOMAIN=voice.example.com`.
3. `docker compose --profile tls up -d caddy` — Caddy terminates wss/https
   with automatic Let's Encrypt certificates and proxies to LiveKit.
4. Devices/browsers connect with `wss://voice.example.com` + tokens.
5. The console is reachable at `https://voice.example.com/console` (set
   `UI_ROOT_PATH=/console` in `.env`; the cookie is sent with `Secure`).
6. Enable TURN in `livekit/livekit.yaml` (uncomment `turn:` and set a domain +
   secret, or run coturn) so media can relay through restrictive NATs — see
   https://docs.livekit.io/home/self-hosting/turn-server/
7. Keep `.env` out of backups/vaults; rotate `LIVEKIT_API_SECRET` if leaked
   (restart the stack afterwards).

## Bridge networking (non-Linux hosts / advanced)

Replace `network_mode: host` on `livekit` with explicit port mappings
(`7880/tcp`, `7881/tcp`, `50000-50200/udp`), set `rtc.use_external_ip: true`
(cloud VM) or `rtc.node_ip: <host-LAN-IP>` (LAN), and point `LIVEKIT_URL`
at the service name instead of `localhost`.
Details: https://docs.livekit.io/home/self-hosting/deployment/

## Docker inside LXC/VM (e.g. Proxmox) and clients in other subnets

With the default `network_mode: host`, the containers bind directly on the
LXC's network stack — there are **no Docker port mappings to open**. Devices
in *other* subnets work as long as routing between the subnets exists and
these ports reach the LXC from the device side:

- `7880/tcp` — signaling WebSocket
- `7881/tcp` — WebRTC TCP fallback (only used when UDP is blocked)
- `50000-50200/udp` — media (ICE connectivity checks + SRTP)

If the Proxmox/LXC firewall (the `pct` firewall option) is enabled, allow
exactly those ports on the LXC. Routing must be **bidirectional** — the server
also sends packets back to the device's ICE candidates. Optional tuning: set
`NODE_IP=<LXC-LAN-IP>` in `.env` so the server only advertises the LAN
candidate; by default it also advertises its docker bridges (172.17/172.18.x)
and — with `rtc.enable_loopback_candidate: true`, used by the port-forward
test flow — 127.0.0.1. Harmless, but ICE wastes a few checks on them.

## Autostart & operations

- `restart: unless-stopped` keeps services alive across reboots (enable the
  Docker service at boot).
- Logs: `docker compose logs -f [service]`, rotated (10 MB × 3).
- Update: `git pull && docker compose build && docker compose up -d`
- Token helper: `make token ID=... ROOM=...` runs `mint_token.py` inside the
  agent image (no local Python deps needed); the printed URL comes from
  `PUBLIC_LIVEKIT_WS_URL`

## Troubleshooting

| Symptom | Fix |
|---|---|
| `curl http://localhost:7880/` fails | `docker compose logs livekit`; port 7880 in use? |
| Agent exits with auth error | `LIVEKIT_API_KEY/SECRET` in `.env` don't match the server's `LIVEKIT_KEYS` — restart stack after fixing |
| Agent logs MCP connect errors | wrong `HOME_ASSISTANT_URL`/token, or integration not added in HA (404), wrong token (401) |
| No agent audio in smoke test | first run may still be downloading models — check `docker compose logs agent`; verify `GREETING` is not empty |
| Device can't reach server | firewall UDP 50000-50200 + TCP 7880/7881; device must be on same network (or configure TURN) |
| Agent crashes with `Illegal instruction` on import | your CPU lacks the x86-64-v2 baseline; keep the pinned `livekit-agents==1.5.17` + `numpy~=1.26.4` (default) — on a modern CPU you may bump to `~=1.7.1` in `agent/requirements.txt` |
| Agent log shows `[W:onnxruntime:Default ...] Failed to persist telemetry device ID; using an in-memory identifier` | ONNX Runtime (silero VAD / turn detector) couldn't store its Microsoft telemetry device ID under `~/.cache` (= the model-cache mount) because that mount is not owned by the container user — e.g. a root-created `MODEL_CACHE_DIR` bind mount, a stale root-owned volume, or rootless Docker. Harmless (it falls back to an in-memory ID), and the stack sets `ORT_DISABLE_TELEMETRY=1` so it shouldn't appear at all. If you bind-mount `MODEL_CACHE_DIR`, hand it to the container user once: `sudo chown -R 1000:1000 /path/to/model-cache` (root-owned mounts would also break model downloads) |
| Weather lookups fail | check your weather MCP server entry in `MCP_SERVERS_JSON`; agent logs show MCP connect/tool errors |
| Assistant answers in the wrong language | set `LANGUAGE` (`de`/`en`) and `docker compose up -d --force-recreate agent` |
