#include "server_config.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "cJSON.h"
#include "sdkconfig.h"

static const char *TAG = "server_cfg";

// Unset Kconfig string symbols are not defined in sdkconfig.h - fallback so
// the code compiles with the feature disabled.
#ifndef CONFIG_LK_CONSOLE_URL
#define CONFIG_LK_CONSOLE_URL ""
#endif

#define SERVER_CONFIG_REFRESH_S   300   // re-fetch at most every 5 minutes
#define SERVER_CONFIG_HTTP_TIMEOUT_MS 3000
#define SERVER_CONFIG_BUF_LEN     256

// Limits enforced both here and by the console (settings validation).
#define INTERVAL_MIN_S 5
#define INTERVAL_MAX_S 3600

static int s_interval_s;         // 0 = not fetched yet
static int64_t s_fetched_ms;     // esp_timer epoch of the last fetch

static int clamp_interval(int value)
{
    if (value < INTERVAL_MIN_S) {
        return INTERVAL_MIN_S;
    }
    if (value > INTERVAL_MAX_S) {
        return INTERVAL_MAX_S;
    }
    return value;
}

static int fetch_interval(void)
{
    if (strlen(CONFIG_LK_CONSOLE_URL) == 0) {
        return -1; // feature disabled at build time
    }
    char url[192];
    int needed = snprintf(url, sizeof(url), "%s/api/device-config",
                          CONFIG_LK_CONSOLE_URL);
    if (needed <= 0 || (size_t)needed >= sizeof(url)) {
        ESP_LOGW(TAG, "Console URL too long, using fallback");
        return -1;
    }

    esp_http_client_config_t http_cfg = {
        .url = url,
        .timeout_ms = SERVER_CONFIG_HTTP_TIMEOUT_MS,
        .buffer_size = SERVER_CONFIG_BUF_LEN,
        .buffer_size_tx = SERVER_CONFIG_BUF_LEN,
        .keep_alive_enable = false,
    };
    esp_http_client_handle_t client = esp_http_client_init(&http_cfg);
    if (client == NULL) {
        ESP_LOGW(TAG, "HTTP init failed for %s", url);
        return -1;
    }

    int result = -1;
    char buf[SERVER_CONFIG_BUF_LEN] = {0};
    // open/fetch_headers/read (the documented pattern for reading a body;
    // esp_http_client_perform consumes the response internally).
    if (esp_http_client_open(client, 0) == ESP_OK) {
        esp_http_client_fetch_headers(client);
        if (esp_http_client_get_status_code(client) == 200) {
            int read_total = 0;
            int read_len;
            while (read_total < (int)sizeof(buf) - 1 &&
                   (read_len = esp_http_client_read(client, buf + read_total,
                                                    sizeof(buf) - 1 - read_total)) > 0) {
                read_total += read_len;
            }
            cJSON *root = cJSON_Parse(buf);
            if (root != NULL) {
                const cJSON *item =
                    cJSON_GetObjectItem(root, "reconnect_interval_s");
                if (cJSON_IsNumber(item)) {
                    result = clamp_interval((int)item->valuedouble);
                }
                cJSON_Delete(root);
            }
        } else {
            ESP_LOGD(TAG, "console responded %d",
                     esp_http_client_get_status_code(client));
        }
    } else {
        ESP_LOGD(TAG, "console unreachable");
    }
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    return result;
}

int server_config_reconnect_interval_s(void)
{
    const int64_t now_ms = esp_timer_get_time() / 1000;
    const int64_t refresh_ms = (int64_t)SERVER_CONFIG_REFRESH_S * 1000;
    if (s_interval_s > 0 && (now_ms - s_fetched_ms) < refresh_ms) {
        return s_interval_s;
    }

    int fetched = fetch_interval();
    if (fetched > 0) {
        if (fetched != s_interval_s) {
            ESP_LOGI(TAG, "reconnect interval from console: %d s", fetched);
        }
        s_interval_s = fetched;
        s_fetched_ms = now_ms;
        return s_interval_s;
    }

    if (s_interval_s > 0) {
        // Keep serving the last known value; try again after the refresh
        // window instead of hammering an unreachable console.
        s_fetched_ms = now_ms - refresh_ms / 2;
        return s_interval_s;
    }
    return clamp_interval(CONFIG_LK_RECONNECT_INTERVAL_S);
}
