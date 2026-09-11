#include "wake_word.h"

#include <stdlib.h>
#include <string.h>

#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"

#include "esp_afe_config.h"
#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "esp_wn_iface.h"
#include "esp_wn_models.h"
#include "model_path.h"
#include "sdkconfig.h"

static const char *TAG = "wake_word";

// Unset Kconfig bools are not defined in sdkconfig.h, so the AFE mode has
// to be resolved at preprocessing time.
#if CONFIG_LK_WAKE_WORD_AFE_HIGH_PERF
#define WW_AFE_MODE     AFE_MODE_HIGH_PERF
#define WW_AFE_MODE_STR "high-perf"
#else
#define WW_AFE_MODE     AFE_MODE_LOW_COST
#define WW_AFE_MODE_STR "low-cost"
#endif

// 1 s of 16 kHz mono audio; the engine consumes it in feed-chunk blocks, so
// this covers chime playback on the fetch path plus some task jitter.
#define STREAM_BUFFER_BYTES (16000 * sizeof(int16_t))

typedef struct {
    wake_word_config_t cfg;
    esp_afe_sr_iface_t *afe_handle;
    esp_afe_sr_data_t *afe_data;
    StreamBufferHandle_t stream;
    int feed_chunk;              // samples per feed (from the AFE)
    // Cross-task state. Each field has a single writer task; volatile 32-bit
    // aligned accesses are safe for that pattern on this target.
    volatile bool armed;              // written by session task
    volatile bool detection_pending;  // fetch task only (test-and-clear there)
    volatile int32_t last_speech_ms;  // written by fetch task
    volatile float volume_db;         // written by fetch task
} ww_ctx_t;

static ww_ctx_t s_ww;

static int32_t now_ms(void)
{
    return (int32_t)(esp_timer_get_time() / 1000);
}

// ---------------------------------------------------------------------------
// Feed task: mono PCM stream -> AFE
// ---------------------------------------------------------------------------

static void feed_task(void *arg)
{
    int16_t *buf = heap_caps_malloc(s_ww.feed_chunk * sizeof(int16_t),
                                    MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (buf == NULL) {
        ESP_LOGE(TAG, "Feed buffer alloc failed");
        vTaskDelete(NULL);
        return;
    }
    const size_t want = s_ww.feed_chunk * sizeof(int16_t);
    size_t have = 0;
    for (;;) {
        // Accumulate until a full AFE feed chunk is available; the stream
        // buffer may return partial reads, which must not be discarded.
        size_t got = xStreamBufferReceive(s_ww.stream, (uint8_t *)buf + have,
                                          want - have, portMAX_DELAY);
        have += got;
        if (have < want) {
            continue;
        }
        s_ww.afe_handle->feed(s_ww.afe_data, buf);
        have = 0;
    }
}

// ---------------------------------------------------------------------------
// Fetch task: AFE results -> wake word events / VAD timestamps
// ---------------------------------------------------------------------------

static void fetch_task(void *arg)
{
    for (;;) {
        afe_fetch_result_t *res = s_ww.afe_handle->fetch(s_ww.afe_data);
        if (res == NULL || res->ret_value < 0) {
            // Negative return = error / not enough data yet; retry shortly.
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        // VAD timestamp for the session end-of-utterance logic.
        if (res->vad_state == VAD_SPEECH) {
            s_ww.last_speech_ms = now_ms();
        }
        s_ww.volume_db = res->data_volume;

        if (res->wakeup_state == WAKENET_DETECTED &&
            s_ww.armed && !s_ww.detection_pending) {
            // Stop further detections until the session re-arms us, then
            // deliver the event outside the AFE-internal locking context.
            s_ww.detection_pending = true;
            s_ww.armed = false;
            s_ww.afe_handle->disable_wakenet(s_ww.afe_data);
            ESP_LOGI(TAG, "Wake word detected (volume %.1f dB)", res->data_volume);
            if (s_ww.cfg.on_detected != NULL) {
                s_ww.cfg.on_detected(s_ww.cfg.ctx);
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

void wake_word_push(const int16_t *samples, int count)
{
    if (s_ww.stream == NULL || samples == NULL || count <= 0) {
        return;
    }
    size_t bytes = count * sizeof(int16_t);
    size_t sent = xStreamBufferSend(s_ww.stream, samples, bytes, 0);
    if (sent != bytes) {
        static int drop_log;
        if (++drop_log % 100 == 1) {
            ESP_LOGW(TAG, "Wake word buffer full - dropped %u/%u bytes",
                     (unsigned)(bytes - sent), (unsigned)bytes);
        }
    }
}

void wake_word_set_armed(bool armed)
{
    if (s_ww.afe_data == NULL) {
        return;
    }
    if (armed && !s_ww.armed) {
        s_ww.afe_handle->enable_wakenet(s_ww.afe_data);
        s_ww.detection_pending = false;
        ESP_LOGI(TAG, "Wake word armed");
    } else if (!armed && s_ww.armed) {
        ESP_LOGD(TAG, "Wake word disarmed");
    }
    s_ww.armed = armed;
}

bool wake_word_is_armed(void)
{
    return s_ww.armed;
}

uint32_t wake_word_ms_since_speech(void)
{
    int32_t last = s_ww.last_speech_ms;
    if (last == 0) {
        return UINT32_MAX;
    }
    return (uint32_t)(now_ms() - last);
}

float wake_word_last_volume_db(void)
{
    return s_ww.volume_db;
}

esp_err_t wake_word_init(const wake_word_config_t *cfg)
{
#if !CONFIG_LK_WAKE_WORD
    ESP_LOGI(TAG, "Wake word disabled (CONFIG_LK_WAKE_WORD=n)");
    return ESP_OK;
#else
    if (cfg != NULL) {
        s_ww.cfg = *cfg;
    }

    // Load the model partition and check the wake word model is present.
    srmodel_list_t *models = esp_srmodel_init("model");
    if (models == NULL || models->num == 0) {
        ESP_LOGE(TAG, "No esp-sr models in the 'model' partition - "
                      "check partitions.csv and CONFIG_SR_WN_*");
        return ESP_ERR_NOT_FOUND;
    }
    char *wn_name = esp_srmodel_filter(models, ESP_WN_PREFIX, NULL);
    if (wn_name == NULL) {
        ESP_LOGE(TAG, "No wake word model flashed - select one via CONFIG_SR_WN_*");
        return ESP_ERR_NOT_FOUND;
    }
    ESP_LOGI(TAG, "Wake word model: %s", wn_name);

    // Single-mic AFE for the already-DSP-processed XMOS signal: the ESP32
    // side only adds VAD + WakeNet (+ WakeNet AGC for far-field detection).
    afe_config_t *afe_cfg = afe_config_init("M", models, AFE_TYPE_SR, WW_AFE_MODE);
    if (afe_cfg == NULL) {
        return ESP_ERR_NO_MEM;
    }
    afe_cfg->aec_init = false;         // XMOS hardware AEC already applied
    afe_cfg->se_init = false;          // single (already beamformed) channel
    afe_cfg->ns_init = false;          // XMOS noise reduction already applied
    afe_cfg->vad_init = true;
    afe_cfg->wakenet_init = true;
    afe_cfg->agc_init = true;          // helps far-field / quiet speakers
    afe_cfg->agc_mode = AFE_AGC_MODE_WAKENET;
    afe_cfg->memory_alloc_mode = AFE_MEMORY_ALLOC_INTERNAL_PSRAM_BALANCE;
    afe_cfg->afe_perferred_core = 0;
    afe_cfg->afe_perferred_priority = 4;

    s_ww.afe_handle = esp_afe_handle_from_config(afe_cfg);
    if (s_ww.afe_handle == NULL) {
        return ESP_ERR_NO_MEM;
    }
    s_ww.afe_data = s_ww.afe_handle->create_from_config(afe_cfg);
    if (s_ww.afe_data == NULL) {
        return ESP_ERR_NO_MEM;
    }
    s_ww.feed_chunk = s_ww.afe_handle->get_feed_chunksize(s_ww.afe_data);
    const int rate = s_ww.afe_handle->get_samp_rate(s_ww.afe_data);
    ESP_LOGI(TAG, "AFE ready: %d Hz, feed chunk %d samples (%s mode), wakenet %s",
             rate, s_ww.feed_chunk, WW_AFE_MODE_STR, wn_name);

    // Detection sensitivity (permille; higher = fewer false accepts).
    float threshold = (float)CONFIG_LK_WAKE_WORD_THRESHOLD_PERMILLE / 1000.0f;
    if (s_ww.afe_handle->set_wakenet_threshold(s_ww.afe_data, 1, threshold) > 0) {
        ESP_LOGI(TAG, "WakeNet threshold set to %.2f", threshold);
    }

    // PCM tap buffer (PSRAM) shared by capture (producer) and feed (consumer).
    uint8_t *stream_store = heap_caps_malloc(STREAM_BUFFER_BYTES,
                                             MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (stream_store == NULL) {
        return ESP_ERR_NO_MEM;
    }
    static StaticStreamBuffer_t stream_meta;
    s_ww.stream = xStreamBufferCreateStatic(STREAM_BUFFER_BYTES, 1,
                                            stream_store, &stream_meta);
    if (s_ww.stream == NULL) {
        return ESP_ERR_NO_MEM;
    }

    s_ww.armed = true;
    s_ww.detection_pending = false;
    s_ww.last_speech_ms = 0;
    s_ww.volume_db = -100.0f;

    if (xTaskCreatePinnedToCore(fetch_task, "ww_fetch", 6144, NULL, 5, NULL, 1) != pdPASS ||
        xTaskCreatePinnedToCore(feed_task, "ww_feed", 4096, NULL, 5, NULL, 0) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
#endif
}

