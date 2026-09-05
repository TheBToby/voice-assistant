/*
 * App entry - voice_agent example main with XVF3800 Wi-Fi tuning.
 *
 * Overlay copy of the example's main.c (livekit/client-sdk-esp32 0.3.2)
 * plus real-time audio Wi-Fi tuning:
 *   - Wi-Fi modem power save disabled (WIFI_PS_NONE): the default
 *     MIN_MODEM setting sleeps between DTIM beacons, adding 100 ms+
 *     latency bursts that break real-time audio.
 *   - Wi-Fi TX power capped to 8.5 dBm: less RF interference with the
 *     audio path on the XIAO/reSpeaker carrier. ONLY do this with good
 *     RSSI (> -70 dBm) - the cap cuts range noticeably.
 */

#include "esp_log.h"
#include "esp_wifi.h"
#include "board.h"
#include "esp_netif_sntp.h"
#include "example.h"
#include "livekit_example_utils.h"
#include "media.h"

#include "livekit.h"

void app_main(void)
{
    esp_log_level_set("*", ESP_LOG_INFO);

    livekit_system_init();
    board_init();
    media_init();
    esp_sntp_config_t sntp_config = ESP_NETIF_SNTP_DEFAULT_CONFIG_MULTIPLE(2,
        ESP_SNTP_SERVER_LIST("time.google.com", "pool.ntp.org"));
    esp_netif_sntp_init(&sntp_config);

    if (lk_example_network_connect()) {
        /* Real-time audio tuning - applies immediately and survives roaming. */
        esp_wifi_set_ps(WIFI_PS_NONE);
        esp_wifi_set_max_tx_power(34); /* 34 * 0.25 dB = 8.5 dBm */
        join_room(); // See example.c
    }
}
