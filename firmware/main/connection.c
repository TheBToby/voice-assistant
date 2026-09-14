#include "connection.h"

#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#include "example.h"
#include "livekit_example_net.h"
#include "server_config.h"

static const char *TAG = "connect";

// Time given to the LiveKit engine's own retry cycle (CONFIG_LK_MAX_RETRIES
// quick attempts with short backoff, signaling timeouts included) before the
// supervisor tears the room down and starts a fresh, delayed cycle.
#define CONNECTION_SETTLE_S 60

// First, fast retry delays (seconds); the sequence doubles until it reaches
// the reconnect interval configured in the console.
#define CONNECTION_FAST_BASE_S 5

static volatile bool s_network_up;
static volatile bool s_network_kick; // fresh IP event -> shorten the wait

static void on_got_ip(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;
    (void)id;
    (void)data;
    if (!s_network_up) {
        ESP_LOGI(TAG, "network is up");
    }
    s_network_up = true;
    s_network_kick = true;
}

static void register_network_events(void)
{
    // The default event loop is created inside lk_example_network_connect();
    // creating it again is harmless as long as we ignore ESP_ERR_INVALID_STATE.
    esp_err_t err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "event loop unavailable (%s) - retry waits will not be "
                      "interrupted by network changes", esp_err_to_name(err));
        return;
    }
    // Both transports: WiFi (this board) and Ethernet (example utils).
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_got_ip, NULL);
    esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_GOT_IP, on_got_ip, NULL);
}

/// Sleep `seconds` in 1 s steps; returns true when woken early by a fresh
/// network event (device got IP - e.g. WiFi came back after an outage).
static bool sleep_watch_network(int seconds)
{
    for (int slept = 0; slept < seconds; slept++) {
        if (s_network_kick) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    bool kicked = s_network_kick;
    s_network_kick = false;
    return kicked;
}

/// Poll the room state for up to `timeout_s`; true once it is connected.
static bool wait_for_room(int timeout_s)
{
    for (int waited = 0; waited < timeout_s && !example_room_connected();
         waited++) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    return example_room_connected();
}

/// Retry delay for the n-th failed cycle: 5, 10, 20, 40, ... capped at the
/// console-configured interval (which the device reaches after a handful of
/// fast tries and then keeps using for as long as the server stays down).
static int delay_for_cycle(int cycle, int interval_s)
{
    int delay = CONNECTION_FAST_BASE_S;
    for (int i = 0; i < cycle && delay < interval_s; i++) {
        delay *= 2;
    }
    return delay > interval_s ? interval_s : delay;
}

static void log_failure_hint(void)
{
    const char *reason = example_failure_reason();
    ESP_LOGE(TAG, "Room connection failed%s%s",
             reason ? ": " : "", reason ? reason : "");
    if (reason != NULL && strcmp(reason, "Bad Token") == 0) {
        ESP_LOGE(TAG, "The access token was rejected - it most likely "
                      "expired. Mint a new token (console: Devices tab, or "
                      "'make token ID=... ROOM=...') and re-flash it.");
    }
}

static void connection_task(void *arg)
{
    (void)arg;
    if (lk_example_network_connect()) {
        s_network_up = true;
    } else {
        ESP_LOGW(TAG, "initial network connection failed - waiting for the "
                      "network before joining the room");
    }
    register_network_events();

    int cycle = 0;
    for (;;) {
        if (!s_network_up) {
            // WiFi layer keeps retrying on its own; resume as soon as we
            // have an IP (on_got_ip flips s_network_up).
            while (!s_network_up) {
                vTaskDelay(pdMS_TO_TICKS(1000));
            }
        }

        join_room();
        if (wait_for_room(CONNECTION_SETTLE_S)) {
            if (cycle > 0) {
                ESP_LOGI(TAG, "connected after %d failed cycle(s)", cycle);
            }
            cycle = 0;
            // Stay connected; short drops are handled by the engine's own
            // reconnect (quick backoff). Only if it stays down for the whole
            // settle window does this loop start a fresh delayed cycle.
            while (example_room_connected()) {
                vTaskDelay(pdMS_TO_TICKS(1000));
            }
            ESP_LOGW(TAG, "room connection lost - waiting for the engine's "
                          "own reconnect");
            if (wait_for_room(CONNECTION_SETTLE_S)) {
                continue;
            }
        }

        log_failure_hint();
        leave_room();

        int interval_s = server_config_reconnect_interval_s();
        int delay_s = delay_for_cycle(cycle++, interval_s);
        ESP_LOGI(TAG, "next connection attempt in %d s (interval %d s)",
                 delay_s, interval_s);
        sleep_watch_network(delay_s);
    }
}

esp_err_t connection_init(void)
{
    if (xTaskCreate(connection_task, "connect", 4096, NULL, 4, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}
