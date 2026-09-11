#pragma once

#include "driver/i2c_master.h"
#include "esp_codec_dev.h"

#ifdef __cplusplus
extern "C" {
#endif

// ---------------------------------------------------------------------------
// XMOS I2S wire format (shared by board.c, mic_source.c and media.c)
// The ESP32 is I2S master; the XMOS XVF3800 drives DIN with the processed
// mic array signal as 24-bit samples left-justified in 32-bit slots.
// ---------------------------------------------------------------------------
#define BOARD_I2S_SAMPLE_RATE  16000
#define BOARD_I2S_SLOT_BITS    32
#define BOARD_I2S_CHANNELS     2

/// Initialize the reSpeaker XVF3800 audio hardware for the XIAO ESP32-S3:
/// I2C bus (AIC3104 speaker codec control), the I2S bridge to the XMOS
/// XVF3800 (which performs mic array processing + AEC in hardware), and the
/// raw-I2S playback/capture devices used by the media pipeline.
void board_init(void);

/// DIAG (temporary): opens the record device directly and dumps raw stereo
/// I2S samples to the log. Call before the media pipeline starts.
void board_diag_i2s_dump(void);

/// Playback device (ESP32 I2S TX -> XMOS -> AIC3104 -> speaker).
esp_codec_dev_handle_t get_playback_handle(void);

/// Capture device (XMOS mic array/AEC -> I2S RX -> ESP32).
esp_codec_dev_handle_t get_record_handle(void);

/// Shared I2C bus handle (AIC3104 + XMOS control port hang off it).
/// Initialized by board_init(); NULL before that.
i2c_master_bus_handle_t board_get_i2c_bus(void);

#ifdef __cplusplus
}
#endif
