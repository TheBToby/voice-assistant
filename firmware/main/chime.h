/*
 * Local notification sounds for the XVF3800 client.
 *
 * The chimes are synthesized at init (sine tones with soft envelopes) instead
 * of shipping binary assets - no flash space, no licensing, easy to tune.
 *
 * Playback path: the media pipeline opens the I2S playback device once and
 * keeps it open; local sounds pause the room rendering (av_render_pause),
 * write the generated PCM straight to the playback device in the wire format
 * (16 kHz / stereo / 32-bit slots), and resume the renderer afterwards. The
 * XMOS AEC uses the I2S playback as its reference, so chimes are cancelled
 * from the mic signal and never re-enter the publish path.
 */

#pragma once

#include <stdbool.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    CHIME_WAKE = 0,        ///< Rising two-tone after the wake word
    CHIME_TIMER_FINISHED,  ///< Timer expiry pattern (starts the ring loop)
    CHIME_MUTE_ON,         ///< Descending tone (mic muted)
    CHIME_MUTE_OFF,        ///< Ascending tone (mic unmuted)
    CHIME_ERROR,           ///< Low double-buzz
} chime_sound_t;

/// Create the sound set and start the playback task. Safe to call before the
/// media pipeline exists; the playback device handle is picked up lazily and
/// the room renderer attaches itself via media_init() -> chime_attach_renderer().
esp_err_t chime_init(void);

/// Attach the AV renderer used by the room so local playback can pause it.
void chime_attach_renderer(void *av_renderer);

/// (Re-)acquire the playback device handle. Called from init; kept public so
/// the media pipeline can refresh it after (re)connection.
void chime_refresh_device(void);

/// Queue a one-shot sound (returns immediately). Ignored while disabled.
esp_err_t chime_play(chime_sound_t sound);

/// Start the timer ring: repeats CHIME_TIMER_FINISHED with a gap until
/// `chime_ring_stop()` or the configured timeout.
esp_err_t chime_ring_start(void);

/// Stop the timer ring (no-op if not ringing).
void chime_ring_stop(void);

/// True while the timer ring loop is active.
bool chime_is_ringing(void);

#ifdef __cplusplus
}
#endif
