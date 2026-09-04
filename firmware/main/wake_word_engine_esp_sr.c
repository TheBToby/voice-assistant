/*
 * Wake word engine implementation based on Espressif ESP-SR WakeNet.
 *
 * Uses the standalone WakeNet API (esp_wn_handle_from_name / detect) on the
 * already processed frames delivered by the esp_capture AEC audio source
 * (the AEC source runs an esp-sr AFE internally: AEC/NS are already applied,
 * output is mono 16 kHz 16-bit PCM). esp-sr itself is already part of the
 * build: espressif/esp_capture (pinned ~0.7 by livekit/client-sdk-esp32)
 * depends on espressif/esp-sr.
 *
 * Model selection: enable exactly one model under
 *   menuconfig -> ESP Speech Recognition -> Load Multiple Wake Words
 * (e.g. CONFIG_SR_WN_WN9_HILEXIN=y) and keep the "model" partition from
 * firmware/partitions.csv. `idf.py flash` then also flashes srmodels.bin.
 */
#include <string.h>
#include <stdlib.h>

#include "esp_err.h"
#include "esp_log.h"
#include "esp_heap_caps.h"

#include "esp_wn_iface.h"
#include "esp_wn_models.h"
#include "model_path.h"

#include "wake_word_engine.h"

static const char *TAG = "ww_engine";

/* DET_MODE_95 = aggressive recall, model weights loaded from PSRAM. The
 * effective operating point is tuned via the detection threshold (see
 * CONFIG_WAKE_WORD_DET_THRESHOLD). */
#define WW_DET_MODE  DET_MODE_95

static const esp_wn_iface_t *s_wn;
static model_iface_data_t *s_wn_data;
static int16_t            *s_chunk;         /* one WakeNet detection chunk */
static int                 s_chunk_samples;
static int                 s_chunk_fill;

/*
 * Destroy the model instance and chunk buffer. The esp-sr model list itself
 * (esp_srmodel_init("model")) is a process-global singleton also used by the
 * esp-capture AEC source - it is never deinit'ed here.
 */
static void ww_engine_shutdown(void)
{
    if (s_wn != NULL && s_wn_data != NULL) {
        s_wn->destroy(s_wn_data);
    }
    if (s_chunk != NULL) {
        free(s_chunk);
    }
    s_wn_data = NULL;
    s_wn = NULL;
    s_chunk = NULL;
    s_chunk_fill = 0;
}

esp_err_t wake_word_engine_init(const wake_word_engine_cfg_t *cfg)
{
    if (cfg == NULL || cfg->sample_rate == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    /* Re-initialization must be safe: the capture path can be closed and
     * re-opened (LiveKit reconnects) at any time. */
    ww_engine_shutdown();

    srmodel_list_t *models = esp_srmodel_init("model");
    if (models == NULL) {
        ESP_LOGE(TAG, "esp_srmodel_init(\"model\") failed - is the 'model' "
                      "partition in partitions.csv and srmodels.bin flashed?");
        return ESP_ERR_NOT_FOUND;
    }

    char *model_name = esp_srmodel_filter(models, ESP_WN_PREFIX, NULL);
    if (model_name == NULL) {
        ESP_LOGE(TAG, "No WakeNet model in the 'model' partition. Enable one "
                      "under menuconfig -> ESP Speech Recognition -> Load "
                      "Multiple Wake Words (e.g. CONFIG_SR_WN_WN9_HILEXIN=y), "
                      "rebuild and flash again.");
        return ESP_ERR_NOT_FOUND;
    }

    s_wn = esp_wn_handle_from_name(model_name);
    if (s_wn == NULL) {
        ESP_LOGE(TAG, "WakeNet handle not available for '%s'", model_name);
        return ESP_ERR_NOT_SUPPORTED;
    }

    s_wn_data = s_wn->create(model_name, WW_DET_MODE);
    if (s_wn_data == NULL) {
        ESP_LOGE(TAG, "Failed to create WakeNet model '%s'", model_name);
        s_wn = NULL;
        return ESP_ERR_NO_MEM;
    }

    int rate = s_wn->get_samp_rate(s_wn_data);
    if (rate != (int)cfg->sample_rate) {
        ESP_LOGE(TAG, "WakeNet model '%s' needs %d Hz but the capture pipeline "
                      "negotiated %d Hz - wake word disabled",
                 model_name, rate, (int)cfg->sample_rate);
        ww_engine_shutdown();
        return ESP_ERR_NOT_SUPPORTED;
    }

    s_chunk_samples = s_wn->get_samp_chunksize(s_wn_data);
    s_chunk = heap_caps_malloc(s_chunk_samples * sizeof(int16_t),
                               MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (s_chunk == NULL) {
        s_chunk = malloc(s_chunk_samples * sizeof(int16_t));
    }
    if (s_chunk == NULL) {
        ESP_LOGE(TAG, "Failed to allocate %d-sample detection chunk",
                 s_chunk_samples);
        ww_engine_shutdown();
        return ESP_ERR_NO_MEM;
    }
    s_chunk_fill = 0;

    s_wn->set_det_threshold(s_wn_data, cfg->det_threshold, 1);

    char *words = esp_srmodel_get_wake_words(models, model_name);
    ESP_LOGI(TAG, "WakeNet '%s' ready: phrase \"%s\", %d Hz, chunk %d samples, "
                  "threshold %.2f",
             model_name, words ? words : "?", rate, s_chunk_samples,
             cfg->det_threshold);
    return ESP_OK;
}

bool wake_word_engine_feed(const int16_t *samples, int num_samples)
{
    if (s_wn == NULL || s_wn_data == NULL || s_chunk == NULL || samples == NULL ||
        num_samples <= 0) {
        return false;
    }

    const int16_t *src = samples;
    int remaining = num_samples;
    while (remaining > 0) {
        int n = s_chunk_samples - s_chunk_fill;
        if (n > remaining) {
            n = remaining;
        }
        memcpy(s_chunk + s_chunk_fill, src, n * sizeof(int16_t));
        s_chunk_fill += n;
        src += n;
        remaining -= n;
        if (s_chunk_fill < s_chunk_samples) {
            break;
        }
        s_chunk_fill = 0;
        if (s_wn->detect(s_wn_data, s_chunk) == WAKENET_DETECTED) {
            return true;
        }
    }
    return false;
}

void wake_word_engine_reset(void)
{
    if (s_wn != NULL && s_wn_data != NULL) {
        s_wn->clean(s_wn_data);
    }
    s_chunk_fill = 0;
}

void wake_word_engine_deinit(void)
{
    /* Destroys the model instance and chunk buffer. The esp-sr model list is
     * a process-global singleton (also referenced by the esp-capture AEC
     * source) and is intentionally left alone. */
    ww_engine_shutdown();
}