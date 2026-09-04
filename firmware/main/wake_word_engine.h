/*
 * Engine-agnostic wake word interface.
 *
 * Overlay for the livekit/client-sdk-esp32 `voice_agent` example.
 *
 * The default engine is Espressif ESP-SR WakeNet (wake_word_engine_esp_sr.c).
 * The interface is intentionally minimal so alternative engines can be dropped
 * in later without touching the capture path - e.g. a microWakeWord (TFLite
 * Micro) model, or a livekit-wakeword model once LiveKit ships an ESP32
 * runtime (https://github.com/livekit/livekit-wakeword).
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint32_t sample_rate;   /*!< Engine input sample rate in Hz (16000 for WakeNet) */
    float    det_threshold; /*!< Detection threshold, 0.0 (sensitive) - 1.0 (strict) */
} wake_word_engine_cfg_t;

/**
 * @brief  Initialize the wake word engine
 *
 * Loads the model from the esp-sr "model" partition (srmodels.bin) and
 * prepares the detector. Logs the configured wake phrase on success.
 *
 * @param[in]  cfg  Engine configuration
 * @return
 *       - ESP_OK              Engine ready
 *       - ESP_ERR_NOT_FOUND   No usable model in the "model" partition
 *       - ESP_ERR_NOT_SUPPORTED Engine sample rate differs from cfg->sample_rate
 *       - Others              Initialization failed
 */
esp_err_t wake_word_engine_init(const wake_word_engine_cfg_t *cfg);

/**
 * @brief  Feed mono 16-bit PCM samples into the detector
 *
 * The engine buffers internally and runs one detection per model chunk
 * (WakeNet: typically 512 samples / 32 ms at 16 kHz).
 *
 * @param[in]  samples      Interleaved-free mono samples (may be processed in chunks)
 * @param[in]  num_samples  Number of samples in `samples`
 * @return
 *       - true   Wake word detected. Call wake_word_engine_reset() before
 *                expecting further detections.
 *       - false  No detection
 */
bool wake_word_engine_feed(const int16_t *samples, int num_samples);

/**
 * @brief  Clear the detector state (call whenever the gate re-arms)
 */
void wake_word_engine_reset(void);

/**
 * @brief  Release all engine resources
 */
void wake_word_engine_deinit(void);

#ifdef __cplusplus
}
#endif