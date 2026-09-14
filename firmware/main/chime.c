#include "chime.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "av_render.h"
#include "board.h"
#include "esp_codec_dev.h"
#include "sdkconfig.h"

static const char *TAG = "chime";

// Unset Kconfig symbols are not defined in sdkconfig.h - provide fallbacks
// so the code compiles with the feature flags off.
#ifndef CONFIG_LK_CHIME_VOLUME_PERCENT
#define CONFIG_LK_CHIME_VOLUME_PERCENT 60
#endif
#ifndef CONFIG_LK_TIMER_RING_MINUTES
#define CONFIG_LK_TIMER_RING_MINUTES 5
#endif

#define CHIME_SAMPLE_RATE 16000

// Play format: must match the renderer's fixed hardware format so the frames
// go out over I2S exactly like room audio (see BOARD_I2S_* in board.h).
#define CHIME_OUT_CHANNELS (BOARD_I2S_CHANNELS)
#define CHIME_OUT_BITS     (BOARD_I2S_SLOT_BITS)
#define CHIME_PLAY_FRAMES  256  // mono samples per esp_codec_dev_write chunk

typedef enum {
    CH_CMD_PLAY = 0,
    CH_CMD_RING_START,
    CH_CMD_RING_STOP,
} chime_cmd_t;

typedef struct {
    chime_cmd_t cmd;
    chime_sound_t sound;
} chime_msg_t;

typedef struct {
    int16_t *pcm;      // mono int16 samples, PSRAM
    int len;           // sample count
} chime_sound_buf_t;

static chime_sound_buf_t s_sounds[5];
static QueueHandle_t s_queue;
static void *s_renderer;                 // av_render_handle_t, may be NULL
static esp_codec_dev_handle_t s_dev;     // refreshed via chime_refresh_device
static volatile bool s_ringing;

// ---------------------------------------------------------------------------
// Synthesis: accumulate sine tones with a raised-cosine envelope
// ---------------------------------------------------------------------------

static void tone_add(int16_t *buf, int buf_len, int start_ms, int dur_ms,
                     float freq, float amp)
{
    const int sr = CHIME_SAMPLE_RATE;
    int start = start_ms * sr / 1000;
    int dur = dur_ms * sr / 1000;
    // 8 ms fade-in/out avoids clicks.
    int fade = 8 * sr / 1000;
    for (int i = 0; i < dur && start + i < buf_len; i++) {
        float env = 1.0f;
        if (i < fade) {
            env = (float)i / fade;
        } else if (i > dur - fade) {
            env = (float)(dur - i) / fade;
        }
        float v = amp * env * sinf(2.0f * (float)M_PI * freq * i / sr);
        int32_t acc = buf[start + i] + (int32_t)(v * 32767.0f);
        if (acc > 32767) {
            acc = 32767;
        }
        if (acc < -32768) {
            acc = -32768;
        }
        buf[start + i] = (int16_t)acc;
    }
}

static int16_t *sound_alloc(int ms)
{
    int samples = ms * CHIME_SAMPLE_RATE / 1000;
    int16_t *buf = heap_caps_calloc(1, samples * sizeof(int16_t),
                                    MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (buf == NULL) {
        return NULL;
    }
    // Caller stores len alongside.
    return buf;
}

#define SND_WAKE   0
#define SND_TIMER  1
#define SND_MUTE_ON  2
#define SND_MUTE_OFF 3
#define SND_ERROR  4
#define SND_COUNT  5

static bool sounds_generate(void)
{
    struct { int ms; int idx; } sizes[SND_COUNT] = {
        [SND_WAKE]     = { 380, SND_WAKE },
        [SND_TIMER]    = { 480, SND_TIMER },
        [SND_MUTE_ON]  = { 220, SND_MUTE_ON },
        [SND_MUTE_OFF] = { 220, SND_MUTE_OFF },
        [SND_ERROR]    = { 300, SND_ERROR },
    };
    for (int i = 0; i < SND_COUNT; i++) {
        s_sounds[i].pcm = sound_alloc(sizes[i].ms);
        if (s_sounds[i].pcm == NULL) {
            return false;
        }
        s_sounds[i].len = sizes[i].ms * CHIME_SAMPLE_RATE / 1000;
    }

    // Wake: rising two-tone "da-ding" (G5 -> C6).
    tone_add(s_sounds[SND_WAKE].pcm, s_sounds[SND_WAKE].len, 0, 130, 784.0f, 0.45f);
    tone_add(s_sounds[SND_WAKE].pcm, s_sounds[SND_WAKE].len, 110, 260, 1046.5f, 0.45f);
    // Timer finished: rising three-tone "ta-da" (C6 -> E6 -> G6).
    tone_add(s_sounds[SND_TIMER].pcm, s_sounds[SND_TIMER].len, 0, 120, 1046.5f, 0.45f);
    tone_add(s_sounds[SND_TIMER].pcm, s_sounds[SND_TIMER].len, 100, 120, 1318.5f, 0.45f);
    tone_add(s_sounds[SND_TIMER].pcm, s_sounds[SND_TIMER].len, 200, 270, 1568.0f, 0.45f);
    // Mute on: descend; mute off: ascend.
    tone_add(s_sounds[SND_MUTE_ON].pcm, s_sounds[SND_MUTE_ON].len, 0, 200, 660.0f, 0.40f);
    tone_add(s_sounds[SND_MUTE_ON].pcm, s_sounds[SND_MUTE_ON].len, 30, 180, 440.0f, 0.40f);
    tone_add(s_sounds[SND_MUTE_OFF].pcm, s_sounds[SND_MUTE_OFF].len, 0, 200, 440.0f, 0.40f);
    tone_add(s_sounds[SND_MUTE_OFF].pcm, s_sounds[SND_MUTE_OFF].len, 30, 180, 660.0f, 0.40f);
    // Error: low buzz.
    tone_add(s_sounds[SND_ERROR].pcm, s_sounds[SND_ERROR].len, 0, 110, 233.1f, 0.45f);
    tone_add(s_sounds[SND_ERROR].pcm, s_sounds[SND_ERROR].len, 150, 110, 233.1f, 0.45f);
    return true;
}

// ---------------------------------------------------------------------------
// Playback
// ---------------------------------------------------------------------------

static int32_t s_play_stereo[CHIME_PLAY_FRAMES * CHIME_OUT_CHANNELS];

// Convert a mono span to the wire format and write it to the device.
// int16 mono -> int32 stereo (16-bit sample shifted to the top of the 32-bit
// slot, matching the renderer's 16->32 bit conversion).
static esp_err_t play_span(const int16_t *mono, int samples)
{
    const float vol = CONFIG_LK_CHIME_VOLUME_PERCENT / 100.0f;
    while (samples > 0) {
        int n = samples < CHIME_PLAY_FRAMES ? samples : CHIME_PLAY_FRAMES;
        for (int i = 0; i < n; i++) {
            int32_t v = (int32_t)(((int64_t)mono[i] << 16) * vol);
            for (int c = 0; c < CHIME_OUT_CHANNELS; c++) {
                s_play_stereo[i * CHIME_OUT_CHANNELS + c] = v;
            }
        }
        size_t bytes = n * CHIME_OUT_CHANNELS * (CHIME_OUT_BITS / 8);
        int ret = esp_codec_dev_write(s_dev, s_play_stereo, bytes);
        if (ret != ESP_CODEC_DEV_OK) {
            ESP_LOGW(TAG, "Playback write failed: %d", ret);
            return ESP_FAIL;
        }
        mono += n;
        samples -= n;
    }
    return ESP_OK;
}

static bool dev_ready(void)
{
    if (s_dev == NULL) {
        chime_refresh_device();
    }
    return s_dev != NULL;
}

static void play_sound(chime_sound_t sound)
{
    if (sound >= SND_COUNT || s_sounds[sound].pcm == NULL) {
        return;
    }
    av_render_handle_t renderer = (av_render_handle_t)s_renderer;

    // Pause room audio so our frames do not interleave with the render
    // thread's writes; paused decoding simply buffers in the FIFOs.
    if (renderer != NULL) {
        av_render_pause(renderer, true);
    }
    // The playback device is opened once by the media pipeline (media.c) and
    // never closed - do NOT open/close it here: esp_codec_dev_open() reports
    // ESP_CODEC_DEV_OK even when the device is already open (by the room
    // renderer), and the close then killed the renderer's device so room
    // audio never played again after the first chime.
    if (!dev_ready() ||
        play_span(s_sounds[sound].pcm, s_sounds[sound].len) != ESP_OK) {
        ESP_LOGW(TAG, "Chime dropped (no playback device or write failed)");
    }
    if (renderer != NULL) {
        av_render_pause(renderer, false);
    }
}

static void chime_task(void *arg)
{
    chime_msg_t msg;
    TickType_t ring_timeout = pdMS_TO_TICKS(
        (uint32_t)CONFIG_LK_TIMER_RING_MINUTES * 60u * 1000u);
    for (;;) {
        if (xQueueReceive(s_queue, &msg, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        switch (msg.cmd) {
        case CH_CMD_PLAY:
            play_sound(msg.sound);
            break;
        case CH_CMD_RING_START: {
            s_ringing = true;
            TickType_t start = xTaskGetTickCount();
            while (s_ringing && (xTaskGetTickCount() - start) < ring_timeout) {
                play_sound(CHIME_TIMER_FINISHED);
                // Gap between repeats; a stop request aborts promptly. Any
                // one-shot sound queued during the ring is skipped (the ring
                // takes priority).
                for (int i = 0; i < 16 && s_ringing; i++) {
                    chime_msg_t next;
                    if (xQueueReceive(s_queue, &next, pdMS_TO_TICKS(100)) == pdTRUE) {
                        if (next.cmd == CH_CMD_RING_STOP) {
                            s_ringing = false;
                        } else {
                            ESP_LOGD(TAG, "Skipping sound during timer ring");
                        }
                    }
                }
            }
            s_ringing = false;
            break;
        }
        case CH_CMD_RING_STOP:
            s_ringing = false;
            break;
        }
    }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

esp_err_t chime_init(void)
{
    if (s_queue != NULL) {
        return ESP_OK;
    }
    if (!sounds_generate()) {
        ESP_LOGW(TAG, "Sound synthesis failed (out of PSRAM) - chimes disabled");
        return ESP_ERR_NO_MEM;
    }
    s_queue = xQueueCreate(4, sizeof(chime_msg_t));
    if (s_queue == NULL) {
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreate(chime_task, "chime", 4096, NULL, 4, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "Chimes ready (volume %d%%)", CONFIG_LK_CHIME_VOLUME_PERCENT);
    return ESP_OK;
}

void chime_attach_renderer(void *av_renderer)
{
    s_renderer = av_renderer;
}

void chime_refresh_device(void)
{
    s_dev = get_playback_handle();
}

esp_err_t chime_play(chime_sound_t sound)
{
    if (s_queue == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    const chime_msg_t msg = { .cmd = CH_CMD_PLAY, .sound = sound };
    return (xQueueSend(s_queue, &msg, 0) == pdTRUE) ? ESP_OK : ESP_ERR_NO_MEM;
}

esp_err_t chime_ring_start(void)
{
    if (s_queue == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_ringing) {
        return ESP_OK;
    }
    // Set synchronously so concurrent callers (timers watchdog) see the
    // ring immediately, before the play task dequeues the command.
    s_ringing = true;
    const chime_msg_t msg = { .cmd = CH_CMD_RING_START };
    if (xQueueSend(s_queue, &msg, 0) != pdTRUE) {
        s_ringing = false;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void chime_ring_stop(void)
{
    if (s_queue == NULL) {
        return;
    }
    const chime_msg_t msg = { .cmd = CH_CMD_RING_STOP };
    if (xQueueSend(s_queue, &msg, 0) != pdTRUE) {
        s_ringing = false; // queue full: play task is waiting on the gap loop
    }
}

bool chime_is_ringing(void)
{
    return s_ringing;
}

