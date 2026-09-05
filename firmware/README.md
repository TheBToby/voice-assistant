# ESP32 wake word overlay (ESP-SR WakeNet, connected standby)

This directory adds an **on-device wake word** to the `voice_agent` firmware
from [livekit/client-sdk-esp32](https://github.com/livekit/client-sdk-esp32)
(pinned `0.3.2`, see `docs/esp32-xvf3800.md` for the base setup).

Behavior (Echo-like, **connected standby**):

- The LiveKit room lifecycle is unchanged: the device joins at boot, the agent
  greets once, timer announcements and `assistant.event` data messages keep
  working.
- While **armed**, the capture source publishes **digital silence**; audio is
  fed to a WakeNet detector and kept in a ~1.5 s pre-wake ring buffer (PSRAM).
  Nothing of substance leaves the device.
- On detection: **local chime** (no cloud round trip), `wake_word_led_on_wake()`
  UI hook, the pre-wake buffer is flushed ahead of the live audio (so the wake
  phrase itself reaches the STT), and live audio flows.
- After `CONFIG_WAKE_WORD_FOLLOWUP_TIMEOUT_S` (default 10 s) without speech the
  module **re-arms** (Echo-style follow-up window).
- If the engine fails to initialize (no model, no PSRAM, unexpected format) the
  firmware degrades to the original always-listening behavior - never to a
  broken device.

## Files

| File | Purpose |
|---|---|
| `main/wake_word_engine.h` | Engine-agnostic detection interface (swappable engine) |
| `main/wake_word_engine_esp_sr.c` | ESP-SR WakeNet engine (standalone `detect()` on the AEC source's processed frames) |
| `main/wake_word.h` / `main/wake_word.c` | State machine (armed/active), pre-wake buffer, chime task, weak LED hooks |
| `main/media.c` | Example `media.c` with a gating wrapper around the `esp_capture` AEC audio source |
| `main/board.c` | Example `board.c` patched for the XVF3800: skips the Korvo-2 BSP init (`bsp_i2c_init`/`bsp_leds_init`) that asserts on this board (§3.2 of the setup guide) |
| `main/CMakeLists.txt`, `main/Kconfig.projbuild` | Example files extended with the wake word sources and the "Wake Word" menu |
| `partitions.csv` | Example partition table + esp-sr `model` partition, app grown to 4 MB (8 MB flash) |
| `sdkconfig.defaults.wakeword` | 8 MB flash, model-in-flash, default model, gating tuning |

Note: esp-sr is **already a dependency** of the build - `espressif/esp_capture`
(~0.7, pulled in by the LiveKit SDK) depends on `espressif/esp-sr` for its AEC
audio source. No new component is added; we only enable a WakeNet model and
run it on the already-AEC'd 16 kHz mono frames.

Lifecycle note: the esp-capture AEC source frees the process-global esp-sr
model list whenever the capture path closes (e.g. a LiveKit reconnect). The
gating source therefore re-initializes the WakeNet model instance on every
capture open (`wake_word_on_capture_open/close` in `wake_word.c`).

## Apply the overlay

From the `voice_agent/` project created in `docs/esp32-xvf3800.md` (§2), with
the ESP-IDF 5.4/5.5 environment active (not 6.x — see `docs/esp32-xvf3800.md`
§1):

```bash
cd voice_agent
VA=/opt/src/voice-assistant   # path of the voice-assistant repo on your machine
# overlay sources + project files (replaces media.c, board.c, CMakeLists.txt,
# Kconfig.projbuild and partitions.csv; adds wake_word.*)
cp -r "$VA/firmware/main/." main/
cp "$VA/firmware/partitions.csv" .
cat "$VA/firmware/sdkconfig.defaults.wakeword" >> sdkconfig.defaults
rm -f sdkconfig   # pick up the new defaults cleanly
idf.py set-target esp32s3
idf.py menuconfig
```

Note: the overlay was derived from the `0.3.2` example. If your `voice_agent`
project was created from a newer release (check the pinned version in
`main/idf_component.yml`), diff the replaced files (`media.c`,
`CMakeLists.txt`, `Kconfig.projbuild`, `partitions.csv`) against your example
first and re-apply the wake-word changes instead of blind-copying.

In `menuconfig` re-apply the base settings from `docs/esp32-xvf3800.md` (§3):
WiFi, `CONFIG_LK_EXAMPLE_USE_PREGENERATED` + server URL + token, and the
`XVF3800` codec board entry (§3.1 - `managed_components/` board_cfg.txt is
unaffected). The wake word settings live under **"Wake Word"**, the model
under **"ESP Speech Recognition" → Load Multiple Wake Words** (default
`wn9_hilexin`, "Hi Lexin"; keep only the models you use to save space).

## Build & flash

```bash
idf.py build
idf.py -p /dev/ttyACM0 flash monitor
```

`idf.py flash` also flashes `build/srmodels/srmodels.bin` into the `model`
partition (handled by esp-sr's build script - `add_dependencies(flash
srmodels_bin)`).

## Verify

1. Boot log shows `wake_word: WakeNet 'wn9_hilexin' ready: phrase "Hi Lexin",
   16000 Hz, chunk 512 samples` and `wake_word: Gating armed: ...`.
2. While armed, the agent sees **no audible audio** (say anything without the
   wake phrase: `docker compose logs -f agent` shows no turns being taken).
3. Say the wake phrase: `wake_word: Wake word detected (... ms buffered), going
   active`, local chime plays, and the utterance reaches the agent as one turn.
4. After ~10 s without speech: `wake_word: Follow-up window elapsed,
   re-arming`.
5. The agent's own spoken replies (XMOS AEC path) must not wake the device.

## Tuning

| Symptom | Knob |
|---|---|
| Device doesn't wake reliably | lower `WAKE_WORD_DET_THRESHOLD` (50 % default); check mic level |
| False wakes | raise `WAKE_WORD_DET_THRESHOLD`; choose a longer/2-word phrase |
| Wakes, then first words are cut off | raise `WAKE_WORD_PRE_BUFFER_MS` |
| Re-arms mid-conversation | raise `WAKE_WORD_FOLLOWUP_TIMEOUT_S` / lower `WAKE_WORD_SPEECH_RMS_THRESHOLD` |
| No chime | `WAKE_WORD_CHIME_RATE/CHANNELS` must match the playback stream (16 kHz/2 ch in the example) |

Monitor CPU/heap with the SDK's task stats (`show_threads()`, see the SDK
README) and `heap_caps_get_free_size` - WakeNet adds a few ms per 32 ms chunk
inline in the capture path, and the esp-capture AEC source's internal esp-sr
AFE may also run a wakenet when one is flashed (double detection cost is
possible; measure and, if needed, disable `WAKE_WORD_ENABLE` or patch
`managed_components/espressif__esp_capture*/.../capture_audio_aec_src.c` to
set `afe_config->wakenet_init = false`).

## LED ring

`wake_word_led_on_wake()` / `wake_word_led_on_idle()` are weak no-ops.
Override them in `board.c` with the XVF3800 LED control to get the visual
"listening" state (verify the LED wiring for your board revision against
Seeed's wiki first). The agent-side `assistant.event` data messages (timers)
remain available for richer animations.

## Custom wake words (e.g. German)

The stock WakeNet9 models are English/Chinese. Options, in order of effort:

1. **Espressif WakeNet customization** - order a custom `wn9`/`wn9s` model for
   your phrase via Espressif's wake-word customization service; flash it in
   `srmodels.bin` (engine unchanged).
2. **microWakeWord (TFLite Micro)** - open-source models, trainable for
   arbitrary phrases; implement a small `wake_word_engine_*` behind
   `wake_word_engine.h` (mel frontend + TFLite-Micro inference) - the capture
   seam stays untouched.
3. **livekit-wakeword** - LiveKit's open-source trainer produces
   openWakeWord-compatible ONNX models (30+ languages incl. German, very low
   false-positive rate) but has no ESP32 runtime yet; until then its models
   can be converted to a TFLite-Micro engine like (2), or run host-side as an
   alternative gating architecture (see `docs/architecture.md`).
