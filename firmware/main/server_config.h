/*
 * Runtime device settings fetched from the web console.
 *
 * The firmware is flashed with compile-time defaults, but some knobs should
 * stay changeable from the server. The console exposes a tiny public,
 * read-only endpoint (GET /api/device-config) that currently carries exactly
 * one value: the reconnect interval the connection supervisor (connection.c)
 * settles on after its fast early retries.
 *
 * The value is cached; the HTTP fetch (best-effort, 3 s timeout) runs at most
 * every SERVER_CONFIG_REFRESH_S. When the console is unreachable or
 * CONFIG_LK_CONSOLE_URL is empty, the Kconfig fallback applies.
 */

#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/// Reconnect interval in seconds: the value fetched from the console
/// (clamped to 5..3600) or CONFIG_LK_RECONNECT_INTERVAL_S when unavailable.
int server_config_reconnect_interval_s(void);

#ifdef __cplusplus
}
#endif