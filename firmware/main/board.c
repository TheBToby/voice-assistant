/*
 * Patched copy of the voice_agent example's board.c
 * (livekit/client-sdk-esp32 v0.3.2) for the reSpeaker XVF3800.
 *
 * The stock file unconditionally initializes the esp32_s3_korvo_2 BSP
 * (bsp_i2c_init() + bsp_leds_init()), which probes a TCA9554 I/O expander
 * that only exists on the Korvo board - on the XVF3800 the assert inside
 * bsp_leds_init() halts the boot (esp32_s3_korvo_2.c:623). Here the BSP init
 * is skipped when CONFIG_LK_EXAMPLE_CODEC_BOARD_TYPE is "XVF3800"; audio and
 * I2C setup are then done exclusively by the codec_board component
 * (managed_components/tempotian__codec_board, see board_cfg.txt).
 *
 * See docs/esp32-xvf3800.md (3.2).
 */

#include <string.h>

#include "esp_log.h"
#include "codec_init.h"
#include "codec_board.h"
#include "driver/temperature_sensor.h"
#include "bsp/esp-bsp.h"

#include "board.h"

static const char *TAG = "board";

static temperature_sensor_handle_t temp_sensor = NULL;

void board_init()
{
    ESP_LOGI(TAG, "Initializing board");

    if (strcmp(CONFIG_LK_EXAMPLE_CODEC_BOARD_TYPE, "XVF3800") != 0) {
        // Korvo-style boards only: BSP and its I/O-expander LEDs.
        bsp_i2c_init();
        bsp_leds_init();
    }

    // Initialize temperature sensor
    temperature_sensor_config_t temp_sensor_config = TEMPERATURE_SENSOR_CONFIG_DEFAULT(10, 50);
    ESP_ERROR_CHECK(temperature_sensor_install(&temp_sensor_config, &temp_sensor));
    ESP_ERROR_CHECK(temperature_sensor_enable(temp_sensor));

    // Initialize codec board
    set_codec_board_type(CONFIG_LK_EXAMPLE_CODEC_BOARD_TYPE);
    codec_init_cfg_t cfg = {
        .in_mode = CODEC_I2S_MODE_TDM,
        .in_use_tdm = true,
        .reuse_dev = false
    };
    init_codec(&cfg);
}

float board_get_temp(void)
{
    float temp_out;
    ESP_ERROR_CHECK(temperature_sensor_get_celsius(temp_sensor, &temp_out));
    return temp_out;
}
