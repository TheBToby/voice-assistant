# ESP32 SDK for LiveKit (vendored)

Vendored copy of [`livekit/client-sdk-esp32`](https://github.com/livekit/client-sdk-esp32)
`components/livekit` @ **v0.3.10** (Apache-2.0, see `LICENSE.txt`), so the
firmware builds against a known SDK revision and can carry local fixes.

> [!IMPORTANT]
> This SDK is currently in Developer Preview mode and not ready for production use.
> There may be bugs and APIs are subject to change during this period.

## Local change vs. upstream v0.3.10

- `core/signaling.c`: signaling WebSocket buffer raised 20 KB → 64 KB.
  Without it the server's `JoinResponse` (which grows once an agent is in the
  room) arrives fragmented and join fails with `Join Incomplete`
  (upstream issue livekit/client-sdk-esp32#86). Details in the comment at the
  `SIGNAL_WS_BUFFER_SIZE` definition.

The upstream `examples/` and `test_app/` directories are omitted; the firmware
using this component lives in `firmware/main/` (based on the upstream
`minimal` example, adapted for the Seeed Studio reSpeaker XVF3800).

## Features

- **Supported chips**: ESP32-S3 and ESP32-P4
- **Bidirectional audio**: Opus encoding, acoustic echo cancellation (AEC)
- **Video publishing**: H.264 encoding, subscribing coming soon
- **AI Agents**: interact with agents in the cloud built with [LiveKit Agents](https://docs.livekit.io/agents/)
- **Real-time data**: data streams, data packets, remote method calls (RPC)
