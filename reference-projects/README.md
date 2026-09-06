# Reference Projects

Upstream sources used as the basis for `firmware/`. Kept for reference only -
nothing here is built directly.

| Folder | What it is | Used for |
|---|---|---|
| `livekit-client-sdk-esp32/` | LiveKit ESP32 client SDK, upstream `v0.3.10-8-g5116e37` (component version `0.3.10`) | Vendored (trimmed) into `firmware/components/livekit` + `firmware/components/example_utils`; `main/` is based on the `minimal` example |
| `XVF3800-esp32-client-agora/` | Seeed's XVF3800 ESP32-S3 voice-assistant client for the Agora/TEN stack (ESP-ADF based) | Hardware bring-up reference: I2S pin map (WS=GPIO7, BCLK=GPIO8, DIN=GPIO43, DOUT=GPIO44, no MCLK), 16 kHz/32-bit format, AIC3104 unmute register sequence (`main/aic3104_ng.c`) |

Both are Apache-2.0 / MIT licensed by their respective owners. The local
adaptation lives in `firmware/` (see `firmware/README.md`).
