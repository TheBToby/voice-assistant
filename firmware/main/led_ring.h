/*
 * LED ring visualization for the XVF3800 (12 RGB LEDs, driven via the XMOS
 * control port - see xvf3800.c).
 *
 * The effects are a faithful C port of the reference integration's animation
 * package (Respeaker-XVF3800-ESPHome-integration, packages/leds.yaml): one
 * user ring color drives every phase, with these per-state effects:
 *
 *   IDLE      user color, solid (reference: the "LED Ring" light, solid)
 *   WAKE      beam snapshot in user color at 0.8 (waiting_for_command)
 *   LISTENING live beam direction, user color at 1.0 (listening_for_command)
 *   THINKING  breathe, user color, 1 Hz at 0.6
 *   SPEAKING  comet counter-clockwise, user color, 1 rev/s at 0.8
 *   TIMER     fast breathe (5 Hz), user color at 1.0 (timer ringing)
 *   + active countdown bar with tick dip while idle (timer_tick at 0.7)
 *   ERROR     breathe, red, 3 Hz at 0.8 (falls back to the previous state)
 *   MUTED     dim red ring (local extension - the reference has no mute
 *             indication; the XMOS GPO mute state is polled while idle)
 *   OFF       dark
 *
 * The beam effect maps the DSP beam azimuth to the ring with the reference's
 * +5 LED offset, eases the shown position towards the target (0.5 s) and
 * fades the ring over 4 LEDs around it (FADE_LEDS = 3).
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    LED_RING_STATE_OFF = 0,
    LED_RING_STATE_IDLE,
    LED_RING_STATE_WAKE,
    LED_RING_STATE_LISTENING,
    LED_RING_STATE_THINKING,
    LED_RING_STATE_SPEAKING,
    LED_RING_STATE_TIMER,
    LED_RING_STATE_ERROR,
} led_ring_state_t;

/// Start the render task (no-op when CONFIG_LK_LEDS is disabled).
esp_err_t led_ring_init(void);

/// Switch the ring to `state`. WAKE auto-falls back to LISTENING after its
/// snapshot phase; ERROR auto-falls back to the state it interrupted.
void led_ring_set_state(led_ring_state_t state);

led_ring_state_t led_ring_get_state(void);

/// Update the speaker direction (degrees, 0 = front, clockwise) used by the
/// beam effect. Callers poll the XMOS azimuth and refresh this.
void led_ring_set_beam_deg(float degrees);

/// Forget the current beam direction (used when the DSP has no fresh data).
void led_ring_clear_beam(void);

/// Progress of the next timer to expire for the idle countdown bar
/// (0..1); pass ratio <= 0 to clear it.
void led_ring_set_timer_progress(float ratio);

/// Periodic room-state hook: switches between IDLE (connected) and OFF.
void led_ring_set_room_connected(bool connected);

#ifdef __cplusplus
}
#endif
