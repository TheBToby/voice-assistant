#pragma once

#include "esp_capture_audio_src_if.h"
#include "esp_codec_dev.h"

#ifdef __cplusplus
extern "C" {
#endif

/// Creates the XVF3800 mic capture source.
///
/// Wraps the raw-I2S record device (`get_record_handle()`) and delivers
/// 16 kHz / mono / 16-bit PCM to the capture sink, converting from the XMOS
/// wire format (16 kHz / stereo / 32-bit slots) and applying the
/// CONFIG_LK_AUDIO_INPUT_SHIFT de-clip gain on the way.
///
/// The returned interface is self-contained (fixed caps, no negotiation
/// needed) and must be freed with `mic_source_destroy()`.
esp_capture_audio_src_if_t *mic_source_create(esp_codec_dev_handle_t rec_dev);

/// Releases a source created by `mic_source_create()`.
void mic_source_destroy(esp_capture_audio_src_if_t *src);

/// Publish gate (wake-word session control).
///
/// While closed, frames are still read from the mic and tapped into the wake
/// word engine, but the source outputs silence - the room sees a live, silent
/// track and the agent's VAD stays quiet. Opened by voice_session.c after a
/// wake word, closed at the end of the utterance.
void mic_source_set_gate(bool open);

/// True while the publish gate is open.
bool mic_source_gate_open(void);

#ifdef __cplusplus
}
#endif
