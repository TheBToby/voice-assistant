/*
 * Wake word gating - state machine, pre-wake buffer, chime and UI hooks.
 *
 * Overlay for the livekit/client-sdk-esp32 `voice_agent` example.
 * See firmware/README.md and docs/esp32-xvf3800.md (section 6).
 *
 * States (connected standby):
 *   ARMED  - the capture path publishes digital silence; frames are fed to the
 *            wake word engine and kept in a pre-wake ring buffer.
 *   ACTIVE - on detection: local chime + LED hook, the buffered pre-wake audio
 *            is flushed ahead of the live audio (so the wake phrase itself
 *            reaches the STT) and after CONFIG_WAKE_WORD_FOLLOWUP_TIMEOUT_S
 *            without speech the module re-arms.
 */
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <sys/param.h>

#include "esp_err.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#include "esp_codec_dev.h"
#include "codec_init.h"

#include "wake_word.h"
#include "wake_word_engine.h"

#if !CONFIG_WAKE_WORD_ENABLE
/* The Kconfig symbols below depend on WAKE_WORD_ENABLE and don't exist when
   it is disabled - define harmless fallbacks so the (unused) static helpers
   in this file still compile. */
#define CONFIG_WAKE_WORD_PRE_BUFFER_MS       0
#define CONFIG_WAKE_WORD_FOLLOWUP_TIMEOUT_S  0
#define CONFIG_WAKE_WORD_SPEECH_RMS_THRESHOLD 500
#define CONFIG_WAKE_WORD_DET_THRESHOLD       50
#define CONFIG_WAKE_WORD_CHIME_ENABLE        0
#define CONFIG_WAKE_WORD_CHIME_RATE          16000
#define CONFIG_WAKE_WORD_CHIME_CHANNELS      2
#endif

static const char *TAG = "wake_word";

/* Input format the module is designed for (matches the Opus encode format of
 * the voice_agent example and the WakeNet model requirement). */
#define WW_SAMPLE_RATE 16000

typedef struct {
    bool              enabled;      /*!< false = passthrough (always publish)   */
    wake_word_state_t state;
    uint32_t          sample_rate;  /*!< negotiated capture rate                */

    /* Pre-wake ring buffer (keeps the last N ms of audio while armed) */
    uint8_t          *ring;
    int               ring_size;    /*!< capacity in bytes                      */
    int               ring_head;    /*!< index of oldest byte                   */
    int               ring_fill;    /*!< used bytes                             */

    /* Ordered copy of the ring handed to the capture path after waking */
    uint8_t          *pending;
    int               pending_size;
    int               pending_pos;

    /* Follow-up window idle tracking */
    uint64_t          idle_us;

    /* Local wake chime */
    SemaphoreHandle_t chime_sem;
    TaskHandle_t      chime_task;
} ww_ctx_t;

static ww_ctx_t s_ctx;
static bool s_engine_ready;   /*!< WakeNet model instance loaded */

static inline int ww_pre_buffer_bytes(void)
{
    return CONFIG_WAKE_WORD_PRE_BUFFER_MS * WW_SAMPLE_RATE / 1000 * 2;
}

static inline uint64_t ww_frame_us(int num_samples)
{
    return (uint64_t)num_samples * 1000000ULL / s_ctx.sample_rate;
}

/* Ring buffer (single producer: the capture read path; drained once on wake) */
static void ww_ring_push(const uint8_t *data, int len)
{
    if (s_ctx.ring == NULL || len <= 0) {
        return;
    }
    if (len >= s_ctx.ring_size) {
        memcpy(s_ctx.ring, data + (len - s_ctx.ring_size), s_ctx.ring_size);
        s_ctx.ring_head = 0;
        s_ctx.ring_fill = s_ctx.ring_size;
        return;
    }
    if (s_ctx.ring_fill + len > s_ctx.ring_size) {
        int dropped = s_ctx.ring_fill + len - s_ctx.ring_size;
        s_ctx.ring_fill -= dropped;
        s_ctx.ring_head = (s_ctx.ring_head + dropped) % s_ctx.ring_size;
    }
    int pos = (s_ctx.ring_head + s_ctx.ring_fill) % s_ctx.ring_size;
    int first = s_ctx.ring_size - pos;
    if (first > len) {
        first = len;
    }
    memcpy(s_ctx.ring + pos, data, first);
    if (len > first) {
        memcpy(s_ctx.ring, data + first, len - first);
    }
    s_ctx.ring_fill += len;
}

static void ww_ring_to_pending(void)
{
    if (s_ctx.pending == NULL || s_ctx.ring == NULL) {
        s_ctx.pending_size = 0;
        s_ctx.pending_pos = 0;
        return;
    }
    int first = s_ctx.ring_size - s_ctx.ring_head;
    if (first > s_ctx.ring_fill) {
        first = s_ctx.ring_fill;
    }
    memcpy(s_ctx.pending, s_ctx.ring + s_ctx.ring_head, first);
    if (s_ctx.ring_fill > first) {
        memcpy(s_ctx.pending + first, s_ctx.ring, s_ctx.ring_fill - first);
    }
    s_ctx.pending_size = s_ctx.ring_fill;
    s_ctx.pending_pos = 0;
    s_ctx.ring_head = 0;
    s_ctx.ring_fill = 0;
}

/* Speech activity: mean square of the frame above a threshold? */
static bool ww_frame_has_speech(const int16_t *samples, int num_samples)
{
    int64_t acc = 0;
    for (int i = 0; i < num_samples; i++) {
        acc += (int64_t)samples[i] * samples[i];
    }
    int64_t mean_sq = acc / (num_samples > 0 ? num_samples : 1);
    int64_t threshold_sq =
        (int64_t)CONFIG_WAKE_WORD_SPEECH_RMS_THRESHOLD * CONFIG_WAKE_WORD_SPEECH_RMS_THRESHOLD;
    return mean_sq > threshold_sq;
}

#if CONFIG_WAKE_WORD_CHIME_ENABLE
/*
 * Local wake chime. Rendered procedurally (two short notes with decay) so no
 * audio asset is needed, and played from its own task so the capture path is
 * never blocked. Format must match the playback stream configured by the
 * codec board (see CONFIG_WAKE_WORD_CHIME_*; defaults 16 kHz stereo to match
 * the renderer frame info of the example).
 */
static void ww_chime_task(void *arg)
{
    (void)arg;
    esp_codec_dev_handle_t play = get_playback_handle();
    if (play == NULL) {
        ESP_LOGW(TAG, "No playback handle - chime disabled");
        vTaskDelete(NULL);
        return;
    }
    const int rate = CONFIG_WAKE_WORD_CHIME_RATE;
    const int channels = CONFIG_WAKE_WORD_CHIME_CHANNELS;
    const int dur_ms = 180;
    const int total_samples = rate * channels * dur_ms / 1000;
    int16_t *pcm = heap_caps_malloc(total_samples * sizeof(int16_t),
                                    MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (pcm == NULL) {
        pcm = malloc(total_samples * sizeof(int16_t));
    }
    if (pcm == NULL) {
        ESP_LOGW(TAG, "No memory for chime - disabled");
        vTaskDelete(NULL);
        return;
    }

    for (;;) {
        if (xSemaphoreTake(s_ctx.chime_sem, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        const int total_per_ch = rate * dur_ms / 1000;
        const int switch_sample = total_per_ch / 2; /* second note midway */
        for (int i = 0; i < total_samples; i++) {
            int t = i / channels;
            float env = 1.0f - (float)t / (float)total_per_ch;
            float freq = (t < switch_sample) ? 880.0f : 1318.5f; /* A5 -> E6 */
#ifndef M_PI
#define M_PI 3.14159265358979f
#endif
            int16_t v = (int16_t)(sinf(2.0f * (float)M_PI * freq * t / rate) *
                                  12000.0f * env * env);
            pcm[i] = v;
        }
        if (esp_codec_dev_write(play, pcm, total_samples * sizeof(int16_t)) < 0) {
            ESP_LOGD(TAG, "chime write failed (format mismatch? tune "
                          "CONFIG_WAKE_WORD_CHIME_*)");
        }
    }
}
#endif /* CONFIG_WAKE_WORD_CHIME_ENABLE */

void wake_word_init(void)
{
#if CONFIG_WAKE_WORD_ENABLE
    memset(&s_ctx, 0, sizeof(s_ctx));
    s_ctx.enabled = true;
    s_ctx.state = WAKE_WORD_ARMED;
    s_ctx.sample_rate = WW_SAMPLE_RATE;

    int pre_bytes = ww_pre_buffer_bytes();
    if (pre_bytes > 0) {
        s_ctx.ring = heap_caps_malloc(pre_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (s_ctx.ring == NULL) {
            s_ctx.ring = heap_caps_malloc(pre_bytes, MALLOC_CAP_8BIT);
        }
        s_ctx.pending = heap_caps_malloc(pre_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (s_ctx.pending == NULL) {
            s_ctx.pending = heap_caps_malloc(pre_bytes, MALLOC_CAP_8BIT);
        }
        s_ctx.ring_size = pre_bytes;
        if (s_ctx.ring == NULL || s_ctx.pending == NULL) {
            ESP_LOGW(TAG, "No memory for the %d-byte pre-wake buffer - "
                          "wake word disabled", pre_bytes);
            s_ctx.enabled = false;
        }
    }

    if (s_ctx.enabled) {
        wake_word_engine_cfg_t ecfg = {
            .sample_rate = WW_SAMPLE_RATE,
            .det_threshold = CONFIG_WAKE_WORD_DET_THRESHOLD / 100.0f,
        };
        s_engine_ready = (wake_word_engine_init(&ecfg) == ESP_OK);
        if (!s_engine_ready) {
            ESP_LOGW(TAG, "Wake word engine init failed - running in "
                          "passthrough (always listening) mode");
            s_ctx.enabled = false;
        }
    }

#if CONFIG_WAKE_WORD_CHIME_ENABLE
    if (s_ctx.enabled) {
        s_ctx.chime_sem = xSemaphoreCreateBinary();
        if (s_ctx.chime_sem == NULL ||
            xTaskCreatePinnedToCore(ww_chime_task, "ww_chime", 4096, NULL,
                                    4, &s_ctx.chime_task, 1) != pdPASS) {
            ESP_LOGW(TAG, "Failed to create chime task - disabled");
            if (s_ctx.chime_sem != NULL) {
                vSemaphoreDelete(s_ctx.chime_sem);
                s_ctx.chime_sem = NULL;
            }
        }
    }
#endif /* CONFIG_WAKE_WORD_CHIME_ENABLE */

    if (s_ctx.enabled) {
        ESP_LOGI(TAG, "Gating armed: pre-buffer %d ms, follow-up %d s, "
                      "threshold %d %%, speak the wake phrase to start",
                 (int)CONFIG_WAKE_WORD_PRE_BUFFER_MS,
                 (int)CONFIG_WAKE_WORD_FOLLOWUP_TIMEOUT_S,
                 (int)CONFIG_WAKE_WORD_DET_THRESHOLD);
    } else {
        ESP_LOGI(TAG, "Passthrough mode: audio is always published");
    }
#else
    ESP_LOGI(TAG, "Disabled (CONFIG_WAKE_WORD_ENABLE=n) - always publishing");
#endif /* CONFIG_WAKE_WORD_ENABLE */
}

void wake_word_deinit(void)
{
#if CONFIG_WAKE_WORD_ENABLE
    if (s_ctx.chime_task != NULL) {
        vTaskDelete(s_ctx.chime_task);
        s_ctx.chime_task = NULL;
    }
    if (s_ctx.chime_sem != NULL) {
        vSemaphoreDelete(s_ctx.chime_sem);
        s_ctx.chime_sem = NULL;
    }
    wake_word_engine_deinit();
    s_engine_ready = false;
    free(s_ctx.ring);
    free(s_ctx.pending);
    memset(&s_ctx, 0, sizeof(s_ctx));
#endif
}

void wake_word_on_capture_open(void)
{
#if CONFIG_WAKE_WORD_ENABLE
    if (!s_ctx.enabled || s_engine_ready) {
        return;
    }
    /* The esp-capture AEC source freed the process-global esp-sr model list
     * when the capture path was closed - recreate the model instance. */
    wake_word_engine_cfg_t ecfg = {
        .sample_rate = WW_SAMPLE_RATE,
        .det_threshold = CONFIG_WAKE_WORD_DET_THRESHOLD / 100.0f,
    };
    s_engine_ready = (wake_word_engine_init(&ecfg) == ESP_OK);
    if (s_engine_ready) {
        ESP_LOGI(TAG, "Wake word engine re-armed after capture (re)open");
    } else {
        ESP_LOGW(TAG, "Wake word engine re-init failed - passthrough mode");
        s_ctx.enabled = false;
    }
#endif
}

void wake_word_on_capture_close(void)
{
#if CONFIG_WAKE_WORD_ENABLE
    if (s_engine_ready) {
        /* Destroys the model instance; the esp-sr model list itself is freed
         * by the esp-capture AEC source. */
        wake_word_engine_deinit();
        s_engine_ready = false;
    }
    wake_word_engine_reset();
#endif
}

void wake_word_note_format(uint32_t sample_rate, int channels)
{
#if CONFIG_WAKE_WORD_ENABLE
    if (!s_ctx.enabled) {
        return;
    }
    if (sample_rate != WW_SAMPLE_RATE || channels != 1) {
        ESP_LOGW(TAG, "Capture negotiated %u Hz/%d ch; wake word needs "
                      "%d Hz/1 ch - disabling gating", (unsigned)sample_rate,
                 channels, WW_SAMPLE_RATE);
        s_ctx.enabled = false;
    }
#else
    (void)sample_rate;
    (void)channels;
#endif
}

void wake_word_process_frame(int16_t *samples, int num_samples)
{
#if CONFIG_WAKE_WORD_ENABLE
    if (!s_ctx.enabled || samples == NULL || num_samples <= 0) {
        return;
    }

    switch (s_ctx.state) {
    case WAKE_WORD_ARMED:
        /* Keep the tail of the audio so the wake phrase itself can be
         * flushed to the pipeline (and therefore to STT) after waking. */
        ww_ring_push((const uint8_t *)samples, num_samples * (int)sizeof(int16_t));
        if (wake_word_engine_feed(samples, num_samples)) {
            ESP_LOGI(TAG, "Wake word detected (%d ms buffered), going active",
                     s_ctx.ring_fill * 1000 / (WW_SAMPLE_RATE * 2));
            wake_word_engine_reset();
            ww_ring_to_pending();
            s_ctx.state = WAKE_WORD_ACTIVE;
            s_ctx.idle_us = 0;
            wake_word_led_on_wake();
#if CONFIG_WAKE_WORD_CHIME_ENABLE
            if (s_ctx.chime_sem != NULL) {
                xSemaphoreGive(s_ctx.chime_sem);
            }
#endif
        }
        break;

    case WAKE_WORD_ACTIVE:
        /* Echo-style follow-up: stay awake while speech is heard, re-arm
         * after the configured quiet period. */
        if (ww_frame_has_speech(samples, num_samples)) {
            s_ctx.idle_us = 0;
        } else {
            s_ctx.idle_us += ww_frame_us(num_samples);
        }
        if (s_ctx.idle_us >=
            (uint64_t)CONFIG_WAKE_WORD_FOLLOWUP_TIMEOUT_S * 1000000ULL) {
            ESP_LOGI(TAG, "Follow-up window elapsed, re-arming");
            wake_word_engine_reset();
            s_ctx.state = WAKE_WORD_ARMED;
            wake_word_led_on_idle();
        }
        break;
    }
#else
    (void)samples;
    (void)num_samples;
#endif /* CONFIG_WAKE_WORD_ENABLE */
}

int wake_word_take_pending(uint8_t *dst, int max_bytes)
{
    if (s_ctx.pending == NULL || s_ctx.pending_pos >= s_ctx.pending_size) {
        return 0;
    }
    int n = MIN(max_bytes, s_ctx.pending_size - s_ctx.pending_pos);
    if (n <= 0) {
        return 0;
    }
    memcpy(dst, s_ctx.pending + s_ctx.pending_pos, n);
    s_ctx.pending_pos += n;
    return n;
}

wake_word_state_t wake_word_get_state(void)
{
#if CONFIG_WAKE_WORD_ENABLE
    return s_ctx.enabled ? s_ctx.state : WAKE_WORD_ACTIVE;
#else
    return WAKE_WORD_ACTIVE;
#endif
}

/* Weak UI hooks - override in board code (e.g. XVF3800 LED ring driver). */
__attribute__((weak)) void wake_word_led_on_wake(void)
{
}

__attribute__((weak)) void wake_word_led_on_idle(void)
{
}