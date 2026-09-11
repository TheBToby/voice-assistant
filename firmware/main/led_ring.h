/*
 * LED ring visualization for the XVF3800 (12 RGB LEDs, driven via the XMOS
 * control port - see xvf3800.c).
 *
 * A 20 Hz task renders the frame for the current state and pushes it to the
 * XMOS (unchanged frames skip the bus). States roughly follow the Home
 * Assistant Voice PE vocabulary so the agent-driven transitions feel familiar:
 *
 *   IDLE      dim breathing in the idle color (also shows mute = dim red)
 *   WAKE      one-shot spin while the wake chime plays -> LISTENING
 *   LISTENING beam direction lit (speaker isolation feedback)
 *   THINKING  comet (agent processing, driven by agent state events)
 *   SPEAKING  pulse (agent speaking, driven by agent state events)
 *   TIMER     blinking while a timer rings
 *   ERROR     quick red triple-blink, then falls back to the previous state
 *   OFF       dark
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
/// spin; ERROR auto-falls back to the state it interrupted.
void led_ring_set_state(led_ring_state_t state);

led_ring_state_t led_ring_get_state(void);

/// Update the speaker direction (degrees, 0 = front, clockwise) used by the
/// LISTENING state. Callers poll the XMOS azimuth and refresh this.
void led_ring_set_beam_deg(float degrees);

/// Forget the current beam direction (used when the DSP has no fresh data).
void led_ring_clear_beam(void);

/// Periodic room-state hook: switches between IDLE (connected) and OFF.
void led_ring_set_room_connected(bool connected);

#ifdef __cplusplus
}
#endif
