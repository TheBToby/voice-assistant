# XVF3800 Firmware (LiveKit ESP32 client)

ESP-IDF firmware for the **Seeed Studio reSpeaker XVF3800** (XIAO ESP32-S3
module) that connects the device as a voice-assistant endpoint to the
self-hosted LiveKit server and its agent (see the repo root `README.md` for
the docker stack).

The firmware is intentionally minimal: WiFi + LiveKit room connection with
**bidirectional audio** (publish microphone, play back the agent's replies).
No wake word, no buttons - those are future work (see Roadmap).

## Layout

```
firmware/
├── CMakeLists.txt / partitions.csv / sdkconfig.defaults
├── components/
│   ├── livekit/          # vendored LiveKit ESP32 SDK v0.3.10 (+1 local fix,
│   │                     #   see components/livekit/README.md)
│   └── example_utils/    # vendored from the SDK: WiFi Kconfig + helper
└── main/
    ├── board.c           # XVF3800 hardware: I2S bridge, I2C, AIC3104, devices
    ├── media.c           # capture (mic) + render (speaker) pipelines
    ├── example.c         # LiveKit room connection
    └── main.c            # app entrypoint
```

## Hardware bring-up facts

| Bus | Setting | Value |
|---|---|---|
| I2S (ESP32 = master, XMOS = slave) | format | standard I2S, 16 kHz, 32-bit slots, stereo, **no MCLK** |
| | pins | BCLK=GPIO8, WS=GPIO7, DIN=GPIO43 (mics), DOUT=GPIO44 (speaker) |
| I2C | bus | 100 kHz, SDA=GPIO5, SCL=GPIO6 |
| | devices | 0x2C = XMOS XVF3800, 0x18 = AIC3104 speaker codec |

Pin sources: XVF3800 schematic and Seeed's own XIAO ESP32-S3 client
(`reference-projects/XVF3800-esp32-client-agora` in this repo). The AIC3104
DAC powers up with muted outputs; `board.c` unmutes it over I2C at boot
(register set ported from Seeed's working client) - without it the speaker
stays silent.

Two consequences of GPIO43/44 being wired to I2S:

- **Console**: GPIO43/44 are also the ESP32-S3's default UART0 TX/RX. The
  firmware routes the console to the USB Serial/JTAG controller
  (`CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`, already set) - logs come over the
  same USB-C port, which then enumerates as `/dev/ttyACM0`.
- **XMOS DSP firmware**: the XMOS must run its stock **I2S-capable** firmware
  variant (the default ships USB-audio on some units). Check/flash it per
  Seeed's reSpeaker XVF3800 wiki before debugging the ESP32 side.

## Audio path (who converts what)

```
mics -> XMOS (beamforming + AEC, hardware) -> I2S RX 16 kHz/2ch/32-bit
     -> capture sink: bit 32->16, channels 2->1      (auto-inserted)
     -> Opus 16 kHz mono                              -> LiveKit room
LiveKit room -> Opus 16 kHz mono -> decoder
     -> renderer resampler: ch 1->2, bits 16->32
     -> I2S TX 16 kHz/2ch/32-bit -> XMOS -> AIC3104 -> speaker
```

The XMOS does the acoustic echo cancellation in hardware, so the firmware
uses the plain audio device source (`esp_capture_new_audio_dev_src`) - no
software AEC. The capture source pins its caps to the XMOS wire format
(16 kHz/2ch/32-bit) so the I2S bus is never reconfigured underneath it.

## Prerequisites

- ESP-IDF **>= 5.4** (upstream SDK requirement; tested by upstream with
  v5.4/v5.5). First-time setup: https://docs.espressif.com/projects/esp-idf/
- reSpeaker XVF3800 with the XIAO ESP32-S3 mounted, XMOS running the I2S
  firmware variant (see above)
- The docker stack from the repo root running (`docker compose up -d`)

## Build & flash

```bash
cd firmware
idf.py set-target esp32s3          # once per build directory
idf.py build
idf.py -p /dev/ttyACM0 flash monitor
```

WiFi credentials and the LiveKit server URL are preset in
`sdkconfig.defaults` (edit there, or override via `idf.py menuconfig`).

## Connect the device token

Tokens are minted by the stack (room name is encoded in the token):

```bash
make token ID=respeaker-1 ROOM=home     # from the repo root
```

Then put the token into the firmware - either `idf.py menuconfig` →
*LiveKit Example* → *Room access token*, or edit `CONFIG_LK_EXAMPLE_TOKEN=`
in `sdkconfig.defaults` and rebuild. The device identity (`respeaker-1`)
and room (`home`) are whatever you minted.

## Verify

1. Serial log shows `Room state changed: CONNECTED` (and an IP from DHCP).
2. `docker compose logs -f agent` shows the agent joining the device's room.
3. Speak - the agent should answer through the speaker.
4. The web console's *Talk* tab (browser client) can join the same room to
   test the device end separately.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Boot loops / garbled log, no `/dev/ttyACM0` | console not on USB Serial/JTAG - keep `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`; use the XIAO's USB-C port |
| `GPIO 44 and 43 are used as console UART I/O pins` warning | same fix - USB Serial/JTAG console frees the I2S data pins |
| No mic audio in the room (agent sees silence), board logs clean | XMOS running the USB firmware variant - reflash the XMOS with the I2S firmware per the Seeed wiki |
| Audio reaches the agent but is unintelligible / full-scale peaks | XMOS output overdriven - lower the mic level on the XMOS side (Seeed I2C Audio Manager, ResID 35 volume / AGC) |
| `Failure reason: Join Incomplete`, `parent stream too short` | signaling fragmentation - mitigated by the vendored SDK's 64 KB buffer (`components/livekit/README.md`); raise `SIGNAL_WS_BUFFER_SIZE` if a much larger join payload reappears |
| Agent never joins the device's room | token/room mismatch: mint with the same `ROOM=`; use `ws://<host-LAN-IP>:7880` (never `localhost`); check TCP 7880 + UDP 50000-60200 reachability |
| Speaker silent, mic path fine | AIC3104 unmute didn't apply - check the boot log for `AIC3104 reg ... write failed` (I2C wiring / XMOS firmware variant) |
| WiFi drops mid-conversation | real-time audio needs RSSI better than ~ -70 dBm; check the `rssi:` line in the boot log |

## Roadmap (not in this minimal firmware)

- On-device wake word (ESP-SR WakeNet) gating the published track
- SET/MUTE buttons + LED ring via the XMOS I2C control port (0x2C)
- XMOS-side mic gain / AGC tuning via the I2C Audio Manager (ResID 35/17)
- Hardware watchdog for the media pipeline
