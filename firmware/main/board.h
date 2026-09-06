#pragma once

#include "esp_codec_dev.h"

#ifdef __cplusplus
extern "C" {
#endif

/// Initialize the reSpeaker XVF3800 audio hardware for the XIAO ESP32-S3:
/// I2C bus (AIC3104 speaker codec control), the I2S bridge to the XMOS
/// XVF3800 (which performs mic array processing + AEC in hardware), and the
/// raw-I2S playback/capture devices used by the media pipeline.
void board_init(void);

/// Playback device (ESP32 I2S TX -> XMOS -> AIC3104 -> speaker).
esp_codec_dev_handle_t get_playback_handle(void);

/// Capture device (XMOS mic array/AEC -> I2S RX -> ESP32).
esp_codec_dev_handle_t get_record_handle(void);

#ifdef __cplusplus
}
#endif
