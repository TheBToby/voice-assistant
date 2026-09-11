/*
 * On-device wake word detection (esp-sr WakeNet) for the XVF3800 client.
 *
 * The XMOS delivers its beamformed + AEC'd signal over I2S; the mic source
 * (main/mic_source.c) taps its 16 kHz mono PCM conversion into this module
 * via `wake_word_push()` so the wake word engine always hears the room even
 * while the publish gate is closed.
 *
 * Engine layout (esp-sr AFE, single-mic input "M"):
 *   - AEC/SE/NS disabled: the XMOS DSP already performs all of that in
 *     hardware; running it again on the processed signal degrades it.
 *   - VAD enabled (WebRTC): drives the end-of-utterance detection that
 *     closes a voice session (see voice_session.c).
 *   - WakeNet enabled with the model selected in menuconfig (default
 *     wn9_heywillow_tts, "Hey Willow").
 *
 * The wake word needs the esp-sr "model" data partition (see partitions.csv
 * and CONFIG_SR_WN_* in Kconfig).
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/// Called from the WakeNet fetch task when the wake word is detected while
/// the detector is armed. Keep the callback short (it delays detection).
typedef void (*wake_word_detected_cb_t)(void *ctx);

typedef struct {
    wake_word_detected_cb_t on_detected;
    void *ctx;
} wake_word_config_t;

/// Initialize the AFE + WakeNet and start the feed/fetch tasks.
/// Requires the "model" partition with the selected wake word model.
esp_err_t wake_word_init(const wake_word_config_t *cfg);

/// Feed captured mono 16 kHz PCM into the engine (non-blocking; drops
/// samples if the internal buffer is full). Called from the capture path.
void wake_word_push(const int16_t *samples, int count);

/// Arm/disarm detection. Disarmed while a voice session is open so the wake
/// word cannot retrigger mid-command; re-armed when the session closes.
void wake_word_set_armed(bool armed);

/// True when the detector is armed.
bool wake_word_is_armed(void);

/// Milliseconds since the VAD last reported speech (UINT32_MAX if never).
uint32_t wake_word_ms_since_speech(void);

/// Latest AFE input volume in dB (diagnostics), refreshed per fetch.
float wake_word_last_volume_db(void);

#ifdef __cplusplus
}
#endif
