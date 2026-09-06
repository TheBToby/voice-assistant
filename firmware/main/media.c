/*
 * Media pipeline for the reSpeaker XVF3800.
 *
 * Capture: the XMOS XVF3800 performs mic array beamforming + AEC in hardware
 * and hands the ESP32 a processed 16 kHz stereo signal in 32-bit I2S slots.
 * The capture source is pinned to exactly that wire format (so esp_codec_dev
 * never reconfigures the bus) and the capture-sink pipeline converts it to
 * the Opus stream LiveKit expects (16 kHz / mono / 16-bit) - the converters
 * (bit depth + channels) are inserted automatically during negotiation.
 *
 * Playback: the Opus track from the room is decoded to 16 kHz mono 16-bit
 * PCM; the renderer is told the hardware wants 16 kHz stereo 32-bit, so its
 * built-in resampler expands bit depth and duplicates the channel before the
 * frames go out over I2S to the XMOS (and on to the AIC3104 / speaker).
 */

#include "esp_check.h"
#include "esp_log.h"
#include "esp_capture_defaults.h"
#include "esp_capture_sink.h"
#include "av_render_default.h"
#include "esp_audio_dec_default.h"
#include "esp_audio_enc_default.h"

#include "board.h"
#include "media.h"

static const char *TAG = "media";

// Must match the I2S configuration in board.c (the XMOS wire format).
#define MEDIA_SAMPLE_RATE     16000
#define MEDIA_CHANNELS        2
#define MEDIA_BITS_PER_SAMPLE 32

#define NULL_CHECK(condition, message) \
    ESP_RETURN_ON_FALSE(condition, -1, TAG, message)

typedef struct {
    esp_capture_sink_handle_t capturer_handle;
    esp_capture_audio_src_if_t *audio_source;
} capture_system_t;

typedef struct {
    audio_render_handle_t audio_renderer;
    av_render_handle_t av_renderer_handle;
} renderer_system_t;

static capture_system_t  capturer_system;
static renderer_system_t renderer_system;

static int build_capturer_system(void)
{
    esp_codec_dev_handle_t record_handle = get_record_handle();
    NULL_CHECK(record_handle, "Failed to get record handle");

    esp_capture_audio_dev_src_cfg_t codec_cfg = {
        .record_handle = record_handle,
    };
    capturer_system.audio_source = esp_capture_new_audio_dev_src(&codec_cfg);
    NULL_CHECK(capturer_system.audio_source, "Failed to create audio source");

    // Pin the source capabilities to the XMOS I2S wire format. Without this
    // the source would negotiate a default 16-bit format and the record
    // device would reconfigure the I2S bus away from the 32-bit slots the
    // XMOS drives.
    esp_capture_audio_info_t fixed_caps = {
        .format_id = ESP_CAPTURE_FMT_ID_PCM,
        .sample_rate = MEDIA_SAMPLE_RATE,
        .channel = MEDIA_CHANNELS,
        .bits_per_sample = MEDIA_BITS_PER_SAMPLE,
    };
    capturer_system.audio_source->set_fixed_caps(capturer_system.audio_source, &fixed_caps);

    esp_capture_cfg_t cfg = {
        .sync_mode = ESP_CAPTURE_SYNC_MODE_AUDIO,
        .audio_src = capturer_system.audio_source,
    };
    NULL_CHECK(esp_capture_open(&cfg, &capturer_system.capturer_handle) == ESP_CAPTURE_ERR_OK,
               "Failed to open capture system");
    return 0;
}

static int build_renderer_system(void)
{
    esp_codec_dev_handle_t render_device = get_playback_handle();
    NULL_CHECK(render_device, "Failed to get render device handle");

    i2s_render_cfg_t i2s_cfg = {
        .play_handle = render_device,
    };
    renderer_system.audio_renderer = av_render_alloc_i2s_render(&i2s_cfg);
    NULL_CHECK(renderer_system.audio_renderer, "Failed to create I2S renderer");

    av_render_cfg_t render_cfg = {
        .audio_render = renderer_system.audio_renderer,
        .audio_raw_fifo_size = 8 * 4096,
        .audio_render_fifo_size = 100 * 1024,
        .allow_drop_data = false,
    };
    renderer_system.av_renderer_handle = av_render_open(&render_cfg);
    NULL_CHECK(renderer_system.av_renderer_handle, "Failed to create AV renderer");

    // Hardware format: the renderer's resampler converts the decoded Opus
    // track (16 kHz / mono / 16-bit) to this before writing to I2S.
    av_render_audio_frame_info_t frame_info = {
        .sample_rate = MEDIA_SAMPLE_RATE,
        .channel = MEDIA_CHANNELS,
        .bits_per_sample = MEDIA_BITS_PER_SAMPLE,
    };
    av_render_set_fixed_frame_info(renderer_system.av_renderer_handle, &frame_info);

    return 0;
}

int media_init(void)
{
    // Register default audio encoder and decoder
    esp_audio_enc_register_default();
    esp_audio_dec_register_default();

    // Build capturer and renderer systems
    NULL_CHECK(build_capturer_system() == 0, "Failed to build capturer system");
    NULL_CHECK(build_renderer_system() == 0, "Failed to build renderer system");
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
