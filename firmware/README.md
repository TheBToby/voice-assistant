# XVF3800 Firmware (LiveKit ESP32 client)

ESP-IDF firmware for the **Seeed Studio reSpeaker XVF3800** (XIAO ESP32-S3
module) that connects the device as a voice-assistant endpoint to the
self-hosted LiveKit server and its agent (see the repo root `README.md` for
the docker stack).

The device is a wake-word voice satellite: bidirectional audio with the
LiveKit room (publish microphone, play back the agent's replies), plus
on-device wake word detection with chime, XMOS beam locking, LED ring
effects, local timer processing, mic AGC and notification sounds.

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
    ├── xvf3800.c         # XMOS control port (I2C): LED ring, azimuth, beam
    │                     #   lock, mic mute, firmware version
    ├── mic_source.c      # mic capture source: wire format -> mono PCM,
    │                     #   de-clip gain, DC block, AGC, wake-word tap,
    │                     #   publish gate
    ├── wake_word.c       # esp-sr AFE + WakeNet9 ("Hey Willow"), VAD
    ├── voice_session.c   # orchestrator: wake -> chime -> beam lock -> gate
    │                     #   open -> agent events -> session end
    ├── chime.c           # synthesized notification sounds (wake/timer/mute)
    ├── led_ring.c        # 12-LED ring effects engine (20 Hz task)
    ├── local_timers.c    # local countdown mirror of the agent's timers
    ├── example.c         # LiveKit room connection + data channel
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

```text
PUBLISH (mic)
  mics -> XMOS (beamforming + AEC, hardware) -> I2S RX 16 kHz/2ch/32-bit slots
       -> mic_source (main/mic_source.c): slot 0 -> mono, 32->16 bit,
          de-clip gain (CONFIG_LK_AUDIO_INPUT_SHIFT), DC blocker,
          software AGC (RMS target + peak limiter)
       -> [tap] wake word engine (always hears the room)
       -> [publish gate] closed = silence to the room (a wake word session
          opens it; the agent's VAD stays quiet while closed)
       -> capture sink: Opus encode 16 kHz mono     (auto-negotiated)
       -> LiveKit engine: frames passed through UNTOUCHED
          (they are already Opus packets - esp_peer does NOT re-encode,
           it only packetizes into RTP)
       -> LiveKit room

SUBSCRIBE (speaker)
  LiveKit room -> Opus 16 kHz mono -> decoder
       -> renderer resampler: ch 1->2, bits 16->32
       -> I2S TX 16 kHz/2ch/32-bit -> XMOS -> AIC3104 -> speaker
  local chimes: synthesized PCM -> playback device while the room renderer
       is paused (av_render_pause); the XMOS AEC cancels them from the mic
```

The XMOS does the acoustic echo cancellation in hardware, so the firmware
uses a custom capture source (`main/mic_source.c`) instead of the generic
audio-device source: it converts the XMOS wire format to 16 kHz/mono/16-bit
PCM, applies the de-clip gain, DC-blocks it and runs a software AGC
(RMS-target gain with an instant peak limiter, `CONFIG_LK_MIC_AGC*`) before
the Opus encoder. Because the source already matches the encoder's input
format, the capture sink inserts no sample converters.

Do not modify the audio frames in the publish path (e.g. in the LiveKit
engine): after the capture sink they are Opus payloads, and touching their
bytes corrupts the bitstream - the room then hears noise/silence and the
receiver's jitter buffer stretches what little decodes, which shows up as
"clipped, slow" audio.

## Voice assistant features

All features are configurable under `idf.py menuconfig` →
*Voice Assistant Features* (defaults below).

| Feature | Module | Behaviour |
|---|---|---|
| Wake word | `wake_word.c` | esp-sr WakeNet9 on the tapped XMOS signal ("Hey Willow" default, more models selectable; threshold configurable). VAD runs alongside for end-of-utterance detection. |
| Wake chime | `chime.c` | Synthesized two-tone, played immediately on detection (the XMOS AEC removes it from the mic path). Mute/error tones included. |
| Publish gate | `mic_source.c` | Mic publishes silence until the wake word; the agent never hears anything in between (like HA voice satellites). Disable with `LK_WAKE_WORD_GATE=n` for an always-open mic. |
| Beam lock | `xvf3800.c` | On wake, the XMOS AEC fixed beams are pinned to the detected speaker azimuth (AEC servicer cmd 81/37) and released at session end - the published signal keeps isolating that speaker. |
| LED ring | `led_ring.c` | Idle breathing (or dim red while mic muted), wake spin, beam direction while listening, agent-driven thinking/speaking effects, timer blink, error blink. |
| Local timers | `local_timers.c` | Mirrors the agent's timers over the LiveKit data channel; counts down locally and rings with jingle + LED even if the agent is down. Rings wait for an idle device; wake word or any speech stops them. |
| Mic AGC | `mic_source.c` | Recommended: normalizes mic level for STT (target -18 dBFS RMS, max +24 dB) and compensates speaker distance; limiter prevents clipping. |

### Session flow

```
"Hey Willow" -> chime + ring spin -> beam lock at speaker direction
             -> publish gate opens -> agent hears the command
             -> agent state events drive the ring (listening/thinking/speaking)
             -> ~1.5 s silence after speech -> gate closes, beam released
```

### Data channel protocol (LiveKit topics)

Agent → device on `assistant.event` (JSON, reliable):

| Event | Payload | Effect on device |
|---|---|---|
| `timer.set` | `id`, `name`, `duration_seconds` | Timer tracked locally |
| `timer.cancel` | `id`, `name` | Timer removed |
| `timer.expired` | `id`, `name` | Ensures the local ring |
| `session.state` | `state` (`listening`/`thinking`/`speaking`) | LED ring effect |

Device → agent on `device.event`: `wake_word`, `timer_ring_stopped` (both
logged by the agent's audit reporter).

The agent publishes the timer events itself; with the `TIMERS_LOCAL=true`
agent setting (default) it no longer speaks the expiry announcement - the
device's local ring replaces it. Set `TIMERS_LOCAL=false` to restore
agent-side TTS announcements (then both fire: device jingle + agent speech).

The wake word only works while the room is connected (the capture pipeline
feeds the detector). The session flow likewise assumes the agent publishes
`session.state`; without it the ring simply stays in the listening effect.

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

`idf.py flash` also writes the wake word model into the new `model`
partition (`srmodels.bin`, packed from the selected `CONFIG_SR_WN_*`).
Existing installs need a one-time full flash because the partition table
gained the `model` partition (and the app partition grew to 4 MB).

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

1. Serial log shows `Room state changed: CONNECTED` (and an IP from DHCP),
   plus `XMOS XVF3800 control port ready (fw ...)` and `Wake word model: wn9_...`.
2. `docker compose logs -f agent` shows the agent joining the device's room.
3. Say the wake word - the chime plays, the ring spins and the beam locks;
   the agent then hears the following command and answers through the speaker.
4. "Stelle einen Timer auf 5 Minuten" - the device rings locally after 5 min;
   wake word or any speech stops the ring.
5. The web console's *Talk* tab (browser client) can join the same room to
   test the device end separately.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Boot loops / garbled log, no `/dev/ttyACM0` | console not on USB Serial/JTAG - keep `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`; use the XIAO's USB-C port |
| `GPIO 44 and 43 are used as console UART I/O pins` warning | same fix - USB Serial/JTAG console frees the I2S data pins |
| No mic audio in the room (agent sees silence), board logs clean | XMOS running the USB firmware variant - reflash the XMOS with the I2S firmware per the Seeed wiki |
| Audio reaches the agent but is unintelligible / full-scale peaks | XMOS output overdriven - keep the de-clip shift (`LK_AUDIO_INPUT_SHIFT`); the AGC log line (`mic_src: N frames: peak=...`) shows the post-gain peak |
| `No esp-sr models in the 'model' partition` | wake word model not flashed - run `idf.py flash` (flashes `srmodels.bin`) or select a model via `CONFIG_SR_WN_*` |
| Wake word never detected | say the wake word close to the device; watch `wake_word` volume in the log; lower `LK_WAKE_WORD_THRESHOLD_PERMILLE`; check the XMOS runs the I2S firmware variant |
| Device wakes up on its own (TV, conversations) | raise `LK_WAKE_WORD_THRESHOLD_PERMILLE` (default 500) or pick a stricter model in menuconfig |
| `XMOS control port 0x2C not responding` | XMOS still booting or wrong firmware variant; LED ring/beam lock/mute stay unavailable, audio still works |
| Timer rings but the agent also announces | agent runs with `TIMERS_LOCAL=false` - set `TIMERS_LOCAL=true` (device rings locally instead of TTS) |
| `Failure reason: Join Incomplete`, `parent stream too short` | signaling fragmentation - mitigated by the vendored SDK's 64 KB buffer (`components/livekit/README.md`); raise `SIGNAL_WS_BUFFER_SIZE` if a much larger join payload reappears |
| Agent never joins the device's room | token/room mismatch: mint with the same `ROOM=`; use `ws://<host-LAN-IP>:7880` (never `localhost`); check TCP 7880 + UDP 50000-60200 reachability |
| Speaker silent, mic path fine | AIC3104 unmute didn't apply - check the boot log for `AIC3104 reg ... write failed` (I2C wiring / XMOS firmware variant) |
| WiFi drops mid-conversation | real-time audio needs RSSI better than ~ -70 dBm; check the `rssi:` line in the boot log |

## Roadmap ideas (not implemented)

- SET/MUTE buttons (no buttons on this board; the XMOS mute GPO + LED
  feedback are already wired in `xvf3800.c` / `led_ring.c`)
- XMOS-side mic gain / AGC tuning via the I2C Audio Manager (ResID 35/17)
- Hardware watchdog for the media pipeline
- "Stop" wake word for the timer ring (currently any speech stops it)
