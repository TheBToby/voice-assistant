/*
 * Media pipeline (capture + render) with wake-word gating.
 *
 * Overlay replacing the `voice_agent` example's media.c (pinned
 * livekit/client-sdk-esp32 0.3.2). Differences vs the original example:
 *   - the esp_capture AEC audio source is wrapped by a gating source that
 *     feeds the wake word engine, publishes digital silence while armed, and
 *     flushes the buffered pre-wake audio after the wake word (see wake_word.c);
 *   - media_init() initializes the wake word module before the capture system.
 * Everything else is unchanged from the example.
 */
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "esp_check.h"
#include "esp_log.h"

#include "codec_init.h"
#include "av_render_default.h"
#include "esp_audio_dec_default.h"
#include "esp_audio_enc_default.h"
#include "esp_capture_defaults.h"
#include "esp_capture_sink.h"

#include "media.h"
#include "wake_word.h"

static const char *TAG = "media";

#define NULL_CHECK(pointer, message) \
    ESP_RETURN_ON_FALSE(pointer != NULL, -1, TAG, message)

typedef struct {
    esp_capture_sink_handle_t capturer_handle;
    esp_capture_audio_src_if_t *audio_source;
} capture_system_t;

typedef struct {
    audio_render_handle_t audio_renderer;
    av_render_handle_t av_renderer_handle;
} renderer_system_t;

/*
 * Gating wrapper around the esp_capture AEC audio source.
 *
 * The `iface` member must stay first: the wrapper is recovered from the
 * interface pointer by a plain cast (first-member trick).
 */
typedef struct {
    esp_capture_audio_src_if_t iface;   /*!< interface exposed to esp_capture */
    esp_capture_audio_src_if_t *inner;  /*!< AEC source from esp_capture      */
    uint32_t sample_rate;               /*!< negotiated source sample rate    */
    uint8_t *scratch;                   /*!< one inner frame buffer           */
    int scratch_size;
    uint64_t samples_out;               /*!< for a monotonic pts              */
} ww_audio_src_t;

static capture_system_t capturer_system;
static renderer_system_t renderer_system;
static ww_audio_src_t s_ww_audio_src;

#if CONFIG_MIC_LEVEL_TAP
/* One-per-second mic level report (XMOS I2S capture path diagnostics). */
static int      s_tap_samples;
static uint64_t s_tap_sum_sq;
static int16_t  s_tap_min = INT16_MAX;
static int16_t  s_tap_max = INT16_MIN;
#endif

static esp_capture_err_t ww_src_open(esp_capture_audio_src_if_t *h)
{
    ww_audio_src_t *s = (ww_audio_src_t *)h;
    if (s->inner == NULL) {
        return ESP_CAPTURE_ERR_INVALID_STATE;
    }
    /* Open the inner AEC source first: its open (re)creates the esp-sr model
     * list and AFE. The wake word engine is then (re)armed on top of the
     * fresh models - re-arming before the open would leave it with a stale
     * model instance that never detects. */
    esp_capture_err_t err = s->inner->open(s->inner);
    if (err != ESP_CAPTURE_ERR_OK) {
        return err;
    }
    wake_word_on_capture_open();
    return ESP_CAPTURE_ERR_OK;
}

static esp_capture_err_t ww_src_get_support_codecs(esp_capture_audio_src_if_t *h,
                                                   const esp_capture_format_id_t **codecs,
                                                   uint8_t *num)
{
    ww_audio_src_t *s = (ww_audio_src_t *)h;
    if (s->inner == NULL) {
        return ESP_CAPTURE_ERR_INVALID_STATE;
    }
    return s->inner->get_support_codecs(s->inner, codecs, num);
}

static esp_capture_err_t ww_src_set_fixed_caps(esp_capture_audio_src_if_t *h,
                                               const esp_capture_audio_info_t *fixed_caps)
{
    ww_audio_src_t *s = (ww_audio_src_t *)h;
    if (s->inner == NULL) {
        return ESP_CAPTURE_ERR_INVALID_STATE;
    }
    return s->inner->set_fixed_caps(s->inner, fixed_caps);
}

static esp_capture_err_t ww_src_negotiate_caps(esp_capture_audio_src_if_t *h,
                                               esp_capture_audio_info_t *in_caps,
                                               esp_capture_audio_info_t *out_caps)
{
    ww_audio_src_t *s = (ww_audio_src_t *)h;
    if (s->inner == NULL) {
        return ESP_CAPTURE_ERR_INVALID_STATE;
    }
    esp_capture_err_t err = s->inner->negotiate_caps(s->inner, in_caps, out_caps);
    if (err == ESP_CAPTURE_ERR_OK) {
        s->sample_rate = out_caps->sample_rate;
        wake_word_note_format(out_caps->sample_rate, out_caps->channel);
    }
    return err;
}

static esp_capture_err_t ww_src_start(esp_capture_audio_src_if_t *h)
{
    ww_audio_src_t *s = (ww_audio_src_t *)h;
    if (s->inner == NULL) {
        return ESP_CAPTURE_ERR_INVALID_STATE;
    }
    s->samples_out = 0;
    return s->inner->start(s->inner);
}

static esp_capture_err_t ww_src_stop(esp_capture_audio_src_if_t *h)
{
    ww_audio_src_t *s = (ww_audio_src_t *)h;
    if (s->inner == NULL) {
        return ESP_CAPTURE_ERR_INVALID_STATE;
    }
    return s->inner->stop(s->inner);
}

static esp_capture_err_t ww_src_close(esp_capture_audio_src_if_t *h)
{
    ww_audio_src_t *s = (ww_audio_src_t *)h;
    if (s->inner == NULL) {
        return ESP_CAPTURE_ERR_INVALID_STATE;
    }
    esp_capture_err_t err = s->inner->close(s->inner);
    /* The AEC source's close frees the process-global esp-sr model list -
     * drop the engine's model instance so the next open re-creates it. */
    wake_word_on_capture_close();
    return err;
}

static esp_capture_err_t ww_src_read_frame(esp_capture_audio_src_if_t *h,
                                           esp_capture_stream_frame_t *frame)
{
    ww_audio_src_t *s = (ww_audio_src_t *)h;
    if (s->inner == NULL) {
        return ESP_CAPTURE_ERR_NOT_SUPPORTED;
    }
    int need = frame->size;
    if (need <= 0 || frame->data == NULL) {
        return ESP_CAPTURE_ERR_INVALID_ARG;
    }
    if (s->scratch_size < need) {
        uint8_t *buf = realloc(s->scratch, need);
        if (buf == NULL) {
            return ESP_CAPTURE_ERR_NO_MEM;
        }
        s->scratch = buf;
        s->scratch_size = need;
    }

    uint8_t *dst = frame->data;
    int filled = 0;
    while (filled < need) {
        /* 1. Audio buffered before the wake word: flushed first so the wake
         *    phrase itself reaches the pipeline (and therefore the STT). */
        int n = wake_word_take_pending(dst + filled, need - filled);
        if (n > 0) {
            filled += n;
            continue;
        }
        /* 2. One frame from the inner AEC source (already AEC/NS processed,
         *    mono 16-bit PCM). */
        esp_capture_stream_frame_t inner_frame = {
            .stream_type = frame->stream_type,
            .pts = 0,
            .data = s->scratch,
            .size = need - filled,
        };
        esp_capture_err_t err = s->inner->read_frame(s->inner, &inner_frame);
        if (err != ESP_CAPTURE_ERR_OK) {
            return err;
        }
        /* 3. Wake word gating: zeroed while armed, live audio once awake.
         *    Detection runs inline (WakeNet needs a few ms per 32 ms chunk -
         *    far below real time on the ESP32-S3). */
        wake_word_process_frame((int16_t *)s->scratch, (need - filled) / 2);
        memcpy(dst + filled, s->scratch, need - filled);
        filled = need;
    }

#if CONFIG_MIC_LEVEL_TAP
    {
        int16_t *pcm = (int16_t *)dst;
        int samples = filled / (int)sizeof(int16_t);
        for (int i = 0; i < samples; i++) {
            int16_t v = pcm[i];
            s_tap_sum_sq += (int64_t)v * v;
            if (v < s_tap_min) {
                s_tap_min = v;
            }
            if (v > s_tap_max) {
                s_tap_max = v;
            }
        }
        s_tap_samples += samples;
        int rate = s->sample_rate ? (int)s->sample_rate : 16000;
        if (s_tap_samples >= rate) {
            int rms = (int)__builtin_sqrt(s_tap_sum_sq /
                          (uint64_t)(s_tap_samples ? s_tap_samples : 1));
            ESP_LOGI(TAG, "mic tap: %d ms rms=%d min=%d max=%d",
                     s_tap_samples * 1000 / rate, rms, s_tap_min, s_tap_max);
            s_tap_samples = 0;
            s_tap_sum_sq = 0;
            s_tap_min = INT16_MAX;
            s_tap_max = INT16_MIN;
        }
    }
#endif

    frame->pts = (uint32_t)(s->samples_out * 1000ULL /
                            (s->sample_rate ? s->sample_rate : 16000));
    s->samples_out += (uint64_t)need / 2;
    return ESP_CAPTURE_ERR_OK;
}

static int build_capturer_system(void)
{
    esp_codec_dev_handle_t record_handle = get_record_handle();
    NULL_CHECK(record_handle, "Failed to get record handle");

    esp_capture_audio_aec_src_cfg_t codec_cfg = {
        .record_handle = record_handle,
        /* XVF3800: the XMOS delivers 2-ch/32-bit standard I2S - reading 4
         * slots (Korvo/ES7210 heritage) yields half-wave/zeroed capture.
         * The AEC source maps slot 0 -> mic, slot 1 -> playback reference. */
        .channel = 2,
        .channel_mask = 1 | 2
    };
    s_ww_audio_src.inner = esp_capture_new_audio_aec_src(&codec_cfg);
    NULL_CHECK(s_ww_audio_src.inner, "Failed to create audio source");

    s_ww_audio_src.iface = (esp_capture_audio_src_if_t){
        .open = ww_src_open,
        .get_support_codecs = ww_src_get_support_codecs,
        .set_fixed_caps = ww_src_set_fixed_caps,
        .negotiate_caps = ww_src_negotiate_caps,
        .start = ww_src_start,
        .read_frame = ww_src_read_frame,
        .stop = ww_src_stop,
        .close = ww_src_close,
    };
    capturer_system.audio_source = &s_ww_audio_src.iface;

    esp_capture_cfg_t cfg = {
        .sync_mode = ESP_CAPTURE_SYNC_MODE_AUDIO,
        .audio_src = capturer_system.audio_source
    };
    esp_capture_open(&cfg, &capturer_system.capturer_handle);
    NULL_CHECK(capturer_system.capturer_handle, "Failed to open capture system");
    return 0;
}

static int build_renderer_system(void)
{
    esp_codec_dev_handle_t render_device = get_playback_handle();
    NULL_CHECK(render_device, "Failed to get render device handle");

    i2s_render_cfg_t i2s_cfg = {
        .play_handle = render_device
    };
    renderer_system.audio_renderer = av_render_alloc_i2s_render(&i2s_cfg);
    NULL_CHECK(renderer_system.audio_renderer, "Failed to create I2S renderer");

    // Set initial speaker volume
    esp_codec_dev_set_out_vol(i2s_cfg.play_handle, CONFIG_LK_EXAMPLE_SPEAKER_VOLUME);

    av_render_cfg_t render_cfg = {
        .audio_render = renderer_system.audio_renderer,
        .audio_raw_fifo_size = 8 * 4096,
        .audio_render_fifo_size = 100 * 1024,
        .allow_drop_data = false,
    };
    renderer_system.av_renderer_handle = av_render_open(&render_cfg);
    NULL_CHECK(renderer_system.av_renderer_handle, "Failed to create AV renderer");

    av_render_audio_frame_info_t frame_info = {
        .sample_rate = 16000,
        .channel = 2,
        .bits_per_sample = 16,
    };
    av_render_set_fixed_frame_info(renderer_system.av_renderer_handle, &frame_info);

    return 0;
}

int media_init(void)
{
    // Wake word gating (passthrough if disabled or init fails)
    wake_word_init();

    // Register default audio encoder and decoder
    esp_audio_enc_register_default();
    esp_audio_dec_register_default();

    // Build capturer and renderer systems
    build_capturer_system();
    build_renderer_system();
    return 0;
}

esp_capture_handle_t media_get_capturer(void)
{
    return capturer_system.capturer_handle;
}

av_render_handle_t media_get_renderer(void)
{
    return renderer_system.av_renderer_handle;
}