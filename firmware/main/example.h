#pragma once

#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

void join_room();
void leave_room();

/// True while the room is in the CONNECTED state.
bool example_room_connected(void);

/// Human-readable reason why the room connection last failed ("Bad Token",
/// "Unreachable", ...), or NULL while no failure is pending. Lets the
/// connection supervisor log WHY an attempt failed (e.g. an expired token).
const char *example_failure_reason(void);

/// Publish a JSON event string to the room on the device-event topic
/// (see CONFIG_LK_DEVICE_EVENT_TOPIC). Returns false when not connected.
bool example_publish_event(const char *json);

#ifdef __cplusplus
}
#endif
