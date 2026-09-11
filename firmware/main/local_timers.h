/*
 * Local countdown timers for the XVF3800 client.
 *
 * The agent (Python) owns the conversation-side timer skill; whenever it
 * starts or cancels a timer it publishes a data message on the
 * "assistant.event" topic:
 *
 *   {"event":"timer.set","id":3,"name":"pizza","duration_seconds":300}
 *   {"event":"timer.cancel","id":3,"name":"pizza"}
 *   {"event":"timer.expired","id":3,"name":"pizza"}
 *
 * This module mirrors those timers and runs the countdown locally, so the
 * device rings immediately at expiry with a local jingle + LED effect - even
 * when the agent is busy, reconnecting, or announcing with TTS is disabled
 * (agent TIMERS_LOCAL=true). Rings wait for an idle device (no open voice
 * session) and repeat until stopped: wake word or any detected speech (see
 * voice_session.c) or the configured timeout.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int id;
    char name[40];
    uint32_t duration_s;
} timer_info_t;

/// Start the watchdog task (no-op when CONFIG_LK_TIMERS is disabled).
esp_err_t local_timers_init(void);

/// Handle an agent timer event. `event` is "timer.set", "timer.cancel" or
/// "timer.expired"; unknown events are ignored. Safe from any task.
void local_timers_on_agent_event(const char *event, int id,
                                 const char *name, float duration_s);

/// True while the timer-finished ring is playing (or waiting for idle).
bool local_timers_is_ringing(void);

/// Stop the ring (voice-stopped via wake word / speech, or timeout).
void local_timers_ring_stop(void);

/// Number of currently tracked (counting) timers.
int local_timers_count(void);

/// Copy up to `max` active timers; returns the number written.
int local_timers_snapshot(timer_info_t *out, int max);

#ifdef __cplusplus
}
#endif
