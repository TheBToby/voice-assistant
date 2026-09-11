/*
 * Mic capture source for the reSpeaker XVF3800.
 *
 * Why this exists
 * ---------------
 * The LiveKit engine's capture sink is configured for OPUS (16 kHz / mono /
 * 16-bit), and esp_capture's generic converters (bit 32->16, channel 2->1)
 * are inserted to get there from the raw XMOS wire format. That chain has two
 * problems on this board:
 *
 *   1. It offers no place to apply gain. The XMOS mic level runs hot: speech
 *      peaks slam into digital full scale (the diagnostics WAV in
 *      firmware/diagnostics shows 4% of samples clipped), which breaks STT.
 *      A previous fix applied the gain reduction in the LiveKit engine - but
 *      the frames the engine receives are already Opus-ENCODED (the sink's
 *      output port sits on the encoder), so shifting their bytes corrupted
 *      the Opus bitstream instead of reducing the volume.
 *
 *   2. The generic 32->16 bit converter truncates instead of scaling for
 *      left-justified 24-bit sample data.
 *
 * This source therefore does the wire-format conversion and the gain
 * reduction itself, on real PCM samples, before the encoder ever sees them:
 *
 *   I2S wire (16 kHz / stereo / 32-bit slots, 24-bit left-justified)
 *     -> take slot 0 (processed mic; slot 1 duplicates it on this XMOS
 *        firmware - taking L-only stays correct even if slot 1 ever carries
 *        the AEC reference instead)
 *     -> >>16 (32-bit slot -> 16-bit PCM)
 *     -> >>CONFIG_LK_AUDIO_INPUT_SHIFT (de-clip gain, -6 dB per bit)
 *     -> DC blocker (removes the XMOS output offset)
 *     -> software AGC (RMS-target gain + instant peak limiter; normalizes
 *        the level for STT, compensates speaker distance)
 *     -> 16 kHz / mono / 16-bit PCM frame
 *
 * The frame is additionally tapped into the wake word engine (which must
 * hear the room even while the publish gate is closed), and the publish
 * gate zeroes the frame while no wake-word session is open (the room then
 * sees a live but silent track - the agent's VAD stays quiet).
 *
 * The source advertises exactly the target format, so the capture sink needs
 * no converters at all - only the Opus encoder.
 */

#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
#include <math.h>

#include "esp_log.h"

#include "board.h"
#include "mic_source.h"
#include "wake_word.h"

#include "sdkconfig.h"

static const char *TAG = "mic_src";

// Format this source advertises to the capture sink (= Opus encoder input).
#define MIC_SAMPLE_RATE     16000
#define MIC_CHANNELS        1
#define MIC_BITS_PER_SAMPLE 16

#define MIC_GAIN_SHIFT CONFIG_LK_AUDIO_INPUT_SHIFT

typedef struct {
    // base MUST stay the first member: this struct is used as its handle.
    esp_capture_audio_src_if_t  base;
    esp_codec_dev_handle_t      rec_dev;
    esp_capture_audio_info_t    caps;       // advertised (fixed) caps
    bool                        started;
    uint32_t                    frame_no;   // mono frames handed out
    uint32_t                    diag_frames;
    int16_t                     diag_peak;
    int32_t                    *scratch;    // wire-format read buffer
    int                         scratch_bytes;
    // Publish gate (wake word session): when closed the source still reads
    // and taps the mic, but publishes silence.
    volatile bool               gate_open;
    // DC blocker state (one-pole high-pass).
    float                       dc_x_prev;
    float                       dc_y_prev;
    // Software AGC state.
    float                       agc_gain_db;
} mic_source_t;

// Single-instance registry so the gate API can reach the source created by
// mic_source_create() without changing the capture-sink interface.
static mic_source_t *s_source;

static esp_capture_err_t mic_source_open(esp_capture_audio_src_if_t *h)
{
    (void)h;
    return ESP_CAPTURE_ERR_OK;
}

static esp_capture_err_t mic_source_get_support_codecs(esp_capture_audio_src_if_t *h,
                                                       const esp_capture_format_id_t **codecs,
                                                       uint8_t *num)
{
    (void)h;
    static const esp_capture_format_id_t support_codecs[] = {ESP_CAPTURE_FMT_ID_PCM};
    *codecs = support_codecs;
    *num = 1;
    return ESP_CAPTURE_ERR_OK;
}

static esp_capture_err_t mic_source_set_fixed_caps(esp_capture_audio_src_if_t *h,
                                                   const esp_capture_audio_info_t *fixed_caps)
{
    if (h == NULL || fixed_caps == NULL) {
        return ESP_CAPTURE_ERR_INVALID_ARG;
    }
    mic_source_t *src = (mic_source_t *)h;
    // The conversion chain is hard-wired to one format; only accept it.
    if (fixed_caps->sample_rate != MIC_SAMPLE_RATE ||
        fixed_caps->channel != MIC_CHANNELS ||
        fixed_caps->bits_per_sample != MIC_BITS_PER_SAMPLE) {
        ESP_LOGE(TAG, "Unsupported fixed caps %lu Hz %d ch %d bit",
                 (unsigned long)fixed_caps->sample_rate,
                 fixed_caps->channel, fixed_caps->bits_per_sample);
        return ESP_CAPTURE_ERR_NOT_SUPPORTED;
    }
    src->caps = *fixed_caps;
    return ESP_CAPTURE_ERR_OK;
}

static esp_capture_err_t mic_source_negotiate_caps(esp_capture_audio_src_if_t *h,
                                                   esp_capture_audio_info_t *in_caps,
                                                   esp_capture_audio_info_t *out_caps)
{
    mic_source_t *src = (mic_source_t *)h;
    if (in_caps->format_id != ESP_CAPTURE_FMT_ID_PCM) {
        return ESP_CAPTURE_ERR_NOT_SUPPORTED;
    }
    *out_caps = src->caps;
    return ESP_CAPTURE_ERR_OK;
}

static esp_capture_err_t mic_source_start(esp_capture_audio_src_if_t *h)
{
    mic_source_t *src = (mic_source_t *)h;
    if (src->started) {
        return ESP_CAPTURE_ERR_OK;
    }
    // Open the record device at the RAW XMOS wire format. esp_codec_dev
    // configures the I2S RX channel accordingly (16 kHz / stereo / 32-bit).
    esp_codec_dev_sample_info_t fs = {
        .sample_rate = BOARD_I2S_SAMPLE_RATE,
        .channel = BOARD_I2S_CHANNELS,
        .bits_per_sample = BOARD_I2S_SLOT_BITS,
    };
    if (esp_codec_dev_open(src->rec_dev, &fs) != ESP_CODEC_DEV_OK) {
        ESP_LOGE(TAG, "Failed to open record device at wire format");
        return ESP_CAPTURE_ERR_NOT_SUPPORTED;
    }
    src->started = true;
    src->frame_no = 0;
    src->diag_frames = 0;
    src->diag_peak = 0;
    src->dc_x_prev = 0.0f;
    src->dc_y_prev = 0.0f;
    src->agc_gain_db = 0.0f;
    return ESP_CAPTURE_ERR_OK;
}

// ---------------------------------------------------------------------------
// Per-frame processing: shift -> DC block -> AGC -> wake word tap -> gate
// ---------------------------------------------------------------------------

#if CONFIG_LK_MIC_AGC
#define MIC_AGC_ENABLED 1
#else
#define MIC_AGC_ENABLED 0
// Fallbacks so the AGC tuning macros below still expand when the feature
// is disabled (unset Kconfig symbols are not defined in sdkconfig.h).
#ifndef CONFIG_LK_MIC_AGC_TARGET_DB
#define CONFIG_LK_MIC_AGC_TARGET_DB (-18)
#endif
#ifndef CONFIG_LK_MIC_AGC_MAX_GAIN_DB
#define CONFIG_LK_MIC_AGC_MAX_GAIN_DB 24
#endif
#endif

// AGC tuning (menuconfig): target RMS level and maximum applied boost.
#define AGC_TARGET_DBFS  (CONFIG_LK_MIC_AGC_TARGET_DB)   // negative dBFS
#define AGC_MAX_GAIN_DB  (CONFIG_LK_MIC_AGC_MAX_GAIN_DB)
// Gain slew: max ±0.25 dB per frame (~32 ms) -> ~7.5 dB/s. Slow enough to
// avoid chasing syllables, fast enough to adapt while a speaker approaches.
#define AGC_STEP_DB      0.25f
// Frames quieter than this RMS are treated as silence: gain is held (no
// noise pumping) but can still be reduced by the limiter.
#define AGC_SILENCE_RMS  100.0f
// Limiter ceiling: scale the frame down when a sample exceeds this (keeps a
// safety margin to digital full scale for the Opus encoder).
#define AGC_LIMIT_PEAK   26000.0f

static void mic_process_frame(mic_source_t *src, const int32_t *wire,
                              int16_t *out, int samples)
{
    const int shift = 16 + MIC_GAIN_SHIFT;  // slot -> 16-bit, then de-clip gain
    const float dc_a = 0.995f;              // one-pole HPF, ~11 Hz @ 16 kHz
    float gain = MIC_AGC_ENABLED
        ? powf(10.0f, src->agc_gain_db / 20.0f) : 1.0f;

    // Pass 1: shift + DC block + gain apply, track RMS/peak.
    double sum_sq = 0.0;
    float peak = 0.0f;
    for (int i = 0; i < samples; i++) {
        float x = (float)(wire[2 * i] >> shift);  // left slot only (see header)
        // DC blocker: y[n] = x[n] - x[n-1] + a * y[n-1]
        float y = x - src->dc_x_prev + dc_a * src->dc_y_prev;
        src->dc_x_prev = x;
        src->dc_y_prev = y;
        float g = y * gain;
        out[i] = (int16_t)g;
        sum_sq += (double)y * (double)y;
        float a = fabsf(g);
        if (a > peak) {
            peak = a;
        }
    }
    const float rms = sqrtf((float)(sum_sq / samples));

    // Pass 2: limiter - scale the frame if the boosted signal would clip,
    // and back the AGC off quickly so it re-converges from below.
    float limiter_gain = 1.0f;
    if (peak > AGC_LIMIT_PEAK) {
        limiter_gain = AGC_LIMIT_PEAK / peak;
        if (MIC_AGC_ENABLED) {
            src->agc_gain_db -= 3.0f;
        }
        for (int i = 0; i < samples; i++) {
            out[i] = (int16_t)(out[i] * limiter_gain);
        }
    }

    // Pass 3: AGC convergence (once per frame, RMS driven).
    if (MIC_AGC_ENABLED && rms > AGC_SILENCE_RMS && limiter_gain == 1.0f) {
        const float dbfs = 20.0f * log10f(rms / 32768.0f + 1e-9f);
        float error = (float)AGC_TARGET_DBFS - dbfs;
        if (error > AGC_STEP_DB) {
            error = AGC_STEP_DB;
        } else if (error < -AGC_STEP_DB) {
            error = -AGC_STEP_DB;
        }
        src->agc_gain_db += error;
        if (src->agc_gain_db < 0.0f) {
            src->agc_gain_db = 0.0f;
        } else if (src->agc_gain_db > (float)AGC_MAX_GAIN_DB) {
            src->agc_gain_db = (float)AGC_MAX_GAIN_DB;
        }
    }

    // Tap: the wake word engine always hears the room (pre-gate).
    wake_word_push(out, samples);

    // Gate: publish silence while no session is open.
    if (!src->gate_open) {
        memset(out, 0, samples * sizeof(int16_t));
    }

    int32_t a = (int32_t)(peak * limiter_gain);
    if (a > src->diag_peak) {
        src->diag_peak = (int16_t)(a > 32767 ? 32767 : a);
    }
}

static esp_capture_err_t mic_source_read_frame(esp_capture_audio_src_if_t *h,
                                               esp_capture_stream_frame_t *frame)
{
    mic_source_t *src = (mic_source_t *)h;
    if (src->started == false) {
        return ESP_CAPTURE_ERR_INVALID_STATE;
    }

    // frame->size is mono 16-bit bytes; the wire carries 4x that volume
    // (2 channels x 32-bit slots).
    const int samples = frame->size / (MIC_CHANNELS * (MIC_BITS_PER_SAMPLE / 8));
    const int wire_bytes = samples * BOARD_I2S_CHANNELS * (BOARD_I2S_SLOT_BITS / 8);
    if (src->scratch == NULL || src->scratch_bytes < wire_bytes) {
        int32_t *buf = (int32_t *)realloc(src->scratch, wire_bytes);
        if (buf == NULL) {
            return ESP_CAPTURE_ERR_NO_MEM;
        }
        src->scratch = buf;
        src->scratch_bytes = wire_bytes;
    }
    if (esp_codec_dev_read(src->rec_dev, src->scratch, wire_bytes) != ESP_CODEC_DEV_OK) {
        return ESP_CAPTURE_ERR_INTERNAL;
    }

    int16_t *out = (int16_t *)frame->data;
    mic_process_frame(src, src->scratch, out, samples);

    // The capture framework overwrites pts from its own frame counter; set a
    // consistent value anyway so the frame is meaningful on its own.
    frame->pts = (uint32_t)(((uint64_t)src->frame_no * samples * 1000) / MIC_SAMPLE_RATE);
    src->frame_no++;

    // DIAG: log the first frame's raw slots once per start (alignment check:
    // left-justified 24-bit data shows low byte == 0), then the post-gain
    // peak periodically for tuning LK_AUDIO_INPUT_SHIFT (keep peaks < 16000).
    if (src->frame_no == 1) {
        ESP_LOGI(TAG, "first frame raw slots: %08" PRIX32 " %08" PRIX32 " %08" PRIX32 " %08" PRIX32,
                 (uint32_t)src->scratch[0], (uint32_t)src->scratch[1],
                 (uint32_t)src->scratch[2], (uint32_t)src->scratch[3]);
    }
    if (++src->diag_frames >= 250) {
        ESP_LOGI(TAG, "%u frames: peak=%d gate=%s agc=%.1f dB%s",
                 (unsigned)src->diag_frames, src->diag_peak,
                 src->gate_open ? "open" : "closed",
                 (double)src->agc_gain_db,
                 src->diag_peak >= 16000 ? " (HOT)" : "");
        src->diag_frames = 0;
        src->diag_peak = 0;
    }
    return ESP_CAPTURE_ERR_OK;
}

static esp_capture_err_t mic_source_stop(esp_capture_audio_src_if_t *h)
{
    mic_source_t *src = (mic_source_t *)h;
    if (src->started) {
        esp_codec_dev_close(src->rec_dev);
        src->started = false;
    }
    return ESP_CAPTURE_ERR_OK;
}

static esp_capture_err_t mic_source_close(esp_capture_audio_src_if_t *h)
{
    (void)h;
    return ESP_CAPTURE_ERR_OK;
}

esp_capture_audio_src_if_t *mic_source_create(esp_codec_dev_handle_t rec_dev)
{
    if (rec_dev == NULL) {
        ESP_LOGE(TAG, "No record device handle");
        return NULL;
    }
    mic_source_t *src = (mic_source_t *)calloc(1, sizeof(mic_source_t));
    if (src == NULL) {
        return NULL;
    }
    src->rec_dev = rec_dev;
    src->caps.format_id = ESP_CAPTURE_FMT_ID_PCM;
    src->caps.sample_rate = MIC_SAMPLE_RATE;
    src->caps.channel = MIC_CHANNELS;
    src->caps.bits_per_sample = MIC_BITS_PER_SAMPLE;
    s_source = src;

    src->base.open = mic_source_open;
    src->base.get_support_codecs = mic_source_get_support_codecs;
    src->base.set_fixed_caps = mic_source_set_fixed_caps;
    src->base.negotiate_caps = mic_source_negotiate_caps;
    src->base.start = mic_source_start;
    src->base.read_frame = mic_source_read_frame;
    src->base.stop = mic_source_stop;
    src->base.close = mic_source_close;
    return &src->base;
}

void mic_source_destroy(esp_capture_audio_src_if_t *src)
{
    mic_source_t *s = (mic_source_t *)src;
    if (s == NULL) {
        return;
    }
    if (s->started) {
        mic_source_stop(src);
    }
    free(s->scratch);
    free(s);
}

void mic_source_set_gate(bool open)
{
    // The gate flag is written from the session task and read from the
    // capture task; a volatile bool is sufficient on this target.
    if (s_source != NULL) {
        s_source->gate_open = open;
    }
}

bool mic_source_gate_open(void)
{
    return s_source != NULL && s_source->gate_open;
}

