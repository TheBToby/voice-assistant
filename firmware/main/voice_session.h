/*
 * Voice session orchestrator - ties wake word, publish gate, beam lock, LED
 * ring, local timers, chimes and the LiveKit room into one flow:
 *
 *   wake word -> chime + spin + beam lock at the speaker direction
 *             -> publish gate opens (the agent hears the command)
 *             -> agent state events steer the ring (listening/thinking/speaking)
 *             -> VAD-detected end of speech closes the gate, releases the
 *                beam and returns the ring to idle
 *
 * While the gate is closed the mic source publishes silence: the room sees a
 * live but silent track, so the agent's VAD never triggers and nothing the
 * device overhears reaches the STT - same privacy model as the Home
 * Assistant voice satellites.
 *
 * The wake word also stops a ringing timer ("stop the timer"), and timer
 * events published by the agent are mirrored into local_timers.c.
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/// Create the session task and register the wake word callback.
/// Call after the modules it drives are initialized (mic_source, wake_word,
/// chime, led_ring, local_timers, xvf3800). The LiveKit room may connect
/// later; the room-state hook updates the flow dynamically.
esp_err_t voice_session_init(void);

/// Room connection state hook (from example.c). Closes an open session when
/// the room drops.
void voice_session_set_room_connected(bool connected);

/// True while a wake-word session is open (gate open).
bool voice_session_is_active(void);

/// Handle one agent JSON event from the "assistant.event" data topic
/// (e.g. {"event":"timer.set",...} or {"event":"session.state","state":...}).
/// Called from the room's data-receive callback context.
void voice_session_on_agent_event(const char *json, size_t len);

#ifdef __cplusplus
}
#endif
