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
#include "driver/i2c_master.h"
#include "bsp/esp-bsp.h"

#include "board.h"

static const char *TAG = "board";

static temperature_sensor_handle_t temp_sensor = NULL;

/*
 * AIC3104 speaker codec init - ported from Seeed's Agora/TEN XIAO client
 * (main/aic3104_ng.c + llm_main.c). The XVF3800 playback path is
 * Host -> XVF3800 -> DAC (TLV320AIC3104), and the DAC outputs are muted at
 * power-on: without this register setup the speaker stays silent. The
 * registers live on page 0; the writes go over the codec_board I2C bus
 * (SDA 5 / SCL 6, XMOS at 0x2C, AIC3104 at 0x18).
 */
#define AIC3104_I2C_ADDR 0x18

static esp_err_t aic3104_speaker_init(void)
{
    i2c_master_bus_handle_t bus = (i2c_master_bus_handle_t)get_i2c_bus_handle(0);
    if (bus == NULL) {
        ESP_LOGW(TAG, "No I2C bus - AIC3104 speaker init skipped");
        return ESP_ERR_INVALID_STATE;
    }

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = AIC3104_I2C_ADDR,
        .scl_speed_hz = 100000,
    };
    i2c_master_dev_handle_t dev = NULL;
    esp_err_t err = i2c_master_bus_add_device(bus, &dev_cfg, &dev);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "AIC3104 device create failed: %s", esp_err_to_name(err));
        return err;
    }

    /* Seeed aic3104_ng_setup_default(): page 0, DAC gain 0 dB, HP/LO outputs
     * unmuted (values from Seeed's working Agora client). */
    static const uint8_t regs[][2] = {
        { 0x00, 0x00 },  /* page control -> page 0       */
        { 0x2B, 0x00 },  /* LEFT_DAC_VOLUME   0 dB       */
        { 0x2C, 0x00 },  /* RIGHT_DAC_VOLUME  0 dB       */
        { 0x33, 0x0D },  /* HPLOUT_LEVEL unmuted, 0xD    */
        { 0x41, 0x0D },  /* HPROUT_LEVEL unmuted, 0xD    */
        { 0x56, 0x0B },  /* LEFT_LOP_LEVEL unmuted, 0xB  */
        { 0x5D, 0x0B },  /* RIGHT_LOP_LEVEL unmuted, 0xB */
    };
    for (int i = 0; i < sizeof(regs) / sizeof(regs[0]); i++) {
        uint8_t buf[2] = { regs[i][0], regs[i][1] };
        err = i2c_master_transmit(dev, buf, sizeof(buf), 100);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "AIC3104 reg 0x%02X write failed: %s",
                     regs[i][0], esp_err_to_name(err));
        }
    }
    ESP_LOGI(TAG, "AIC3104 speaker codec initialized");
    return ESP_OK;
}

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
    // XVF3800: the XMOS exposes 2-ch/32-bit STANDARD I2S at 16 kHz (Seeed's
    // verified I2S sketches for this hardware). The stock example config
    // (TDM, 4ch, 16-bit) is shaped for the Korvo's ES7210 mic array and
    // yields silence here. Also note: the XMOS must run its I2S firmware
    // (not the default USB firmware) - see the Seeed XVF3800 XIAO wiki.
    codec_init_cfg_t cfg = {
        .in_mode = CODEC_I2S_MODE_STD,
        .out_mode = CODEC_I2S_MODE_STD,
        .in_use_tdm = false,
        .reuse_dev = false
    };
    init_codec(&cfg);

    // AIC3104 speaker codec: unmute/enable the DAC outputs (Seeed Agora
    // client port). Without it the speaker stays silent.
    aic3104_speaker_init();
}

float board_get_temp(void)
{
    float temp_out;
    ESP_ERROR_CHECK(temperature_sensor_get_celsius(temp_sensor, &temp_out));
    return temp_out;
}
