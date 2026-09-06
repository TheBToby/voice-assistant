#include "esp_log.h"
#include "esp_netif_sntp.h"
#include "esp_system.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "board.h"
#include "example.h"
#include "livekit_example_utils.h"
#include "media.h"

#include "livekit.h"

static const char *TAG = "main";

/// Periodically prints heap status to aid on-site bring-up.
static void status_task(void *arg)
{
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(30000));
        ESP_LOGI(TAG, "status: free heap %6u KiB (largest block %6u KiB)",
                 (unsigned)(esp_get_free_heap_size() / 1024),
                 (unsigned)(heap_caps_get_largest_free_block(MALLOC_CAP_8BIT) / 1024));
    }
}

void app_main(void)
{
    esp_log_level_set("*", ESP_LOG_INFO);

    // Media stack thread/priority tuning (NVS + netif init happen inside
    // lk_example_network_connect, like in the SDK examples).
    livekit_system_init();
    board_init();
    media_init();

    // Wall-clock time for TLS certificate validation.
    esp_sntp_config_t sntp_config = ESP_NETIF_SNTP_DEFAULT_CONFIG_MULTIPLE(2,
        ESP_SNTP_SERVER_LIST("time.google.com", "pool.ntp.org"));
    esp_netif_sntp_init(&sntp_config);

    xTaskCreate(status_task, "status", 3072, NULL, 5, NULL);

    if (lk_example_network_connect()) {
        join_room(); // See example.c
    } else {
        ESP_LOGE(TAG, "Network connection failed - not joining room");
    }
}
