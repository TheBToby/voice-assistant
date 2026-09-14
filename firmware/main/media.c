/*
 * Media pipeline for the reSpeaker XVF3800.
 *
 * Capture: the XMOS XVF3800 performs mic array beamforming + AEC in hardware
 * and hands the ESP32 a processed 16 kHz stereo signal in 32-bit I2S slots
 * (24-bit samples, left-justified). The mic source (main/mic_source.c) wraps
 * the record device: it converts slot 0 to 16 kHz / mono / 16-bit PCM and
 * applies the de-clip gain (CONFIG_LK_AUDIO_INPUT_SHIFT). The capture sink
 * then only has to encode that PCM to the Opus stream LiveKit expects
 * (16 kHz / mono) - no sample converters are inserted.
 *
 * The capture sink delivers OPUS-ENCODED frames. The LiveKit engine forwards
 * them to esp_peer untouched (esp_peer does NOT re-encode audio - it only
 * packetizes into RTP). The encoded payload must never be modified anywhere
 * on this path.
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
#include "chime.h"
#include "media.h"
#include "mic_source.h"

static const char *TAG = "media";

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

    // Mic source: XMOS wire format -> 16 kHz mono 16-bit PCM + de-clip gain.
    // The fixed caps make the capture sink negotiate a converter-free path:
    // only the Opus encoder sits between this source and the engine.
    capturer_system.audio_source = mic_source_create(record_handle);
    NULL_CHECK(capturer_system.audio_source, "Failed to create mic source");

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
    // track (16 kHz / mono / 16-bit) to the XMOS wire format before writing
    // to I2S (see BOARD_I2S_* in board.h).
    av_render_audio_frame_info_t frame_info = {
        .sample_rate = BOARD_I2S_SAMPLE_RATE,
        .channel = BOARD_I2S_CHANNELS,
        .bits_per_sample = BOARD_I2S_SLOT_BITS,
    };
    av_render_set_fixed_frame_info(renderer_system.av_renderer_handle, &frame_info);

    // Open the playback device here and keep it open for the lifetime of the
    // pipeline. The I2S renderer only opens it lazily on the first subscribed
    // audio stream, and esp_codec_dev reports ESP_CODEC_DEV_OK for an
    // ALREADY-open device - chime.c used to misread that as "I opened it",
    // closed the device behind the renderer's back after each local sound,
    // and the render thread then failed every following write (agent replies
    // silently never played again). With the open owned here and never
    // released, both writers are safe: the chime only writes while the
    // renderer is paused, and nothing closes the device.
    esp_codec_dev_sample_info_t fs = {
        .sample_rate = BOARD_I2S_SAMPLE_RATE,
        .channel = BOARD_I2S_CHANNELS,
        .bits_per_sample = BOARD_I2S_SLOT_BITS,
    };
    NULL_CHECK(esp_codec_dev_open(render_device, &fs) == ESP_CODEC_DEV_OK,
               "Failed to open playback device");

    // Local notification sounds pause the room renderer while they play
    // (see main/chime.c).
    chime_attach_renderer(renderer_system.av_renderer_handle);

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
