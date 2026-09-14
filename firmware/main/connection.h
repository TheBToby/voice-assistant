/*
 * Connection supervisor for the LiveKit room.
 *
 * The LiveKit engine retries dropped connections a few times with short
 * backoff (CONFIG_LK_MAX_RETRIES, <= ~7 s apart). When that is not enough -
 * server down during boot, network outage, expired token - the device used
 * to stay offline until reboot. This module keeps trying forever while the
 * network is up:
 *
 *   network up -> join room -> (engine's own quick retries) -> connected?
 *     -> stay, watch the connection, engine self-heals short drops
 *     -> still down -> leave, delay (5, 10, 20, ... capped at the interval
 *        configured in the console; fetched via server_config.c), retry
 *
 * A fresh IP event (device (re)connected to the network) shortens the
 * current wait so a device that sat out a router reboot reconnects promptly.
 */

#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/// Start the supervisor task. Handles the network connect (like the previous
/// one-shot app_main flow) and re-joins the room with escalating delays until
/// it stays connected. Never returns on failure - it retries.
esp_err_t connection_init(void);

#ifdef __cplusplus
}
#endif