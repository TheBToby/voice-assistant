/*
 * Board bring-up for the Seeed Studio reSpeaker XVF3800 with the XIAO
 * ESP32-S3 module, for the LiveKit ESP32 client SDK.
 *
 * Unlike the SDK's board examples, there is no I2C-controlled codec chip on
 * the ESP32 side of this board: the ESP32 exchanges digitized audio with the
 * XMOS XVF3800 over a plain I2S link, and the XMOS handles microphone array
 * processing (beamforming, AEC, noise reduction) in hardware. The AIC3104
 * DAC that drives the speaker hangs off the XMOS; its I2C control port is
 * routed to the ESP32, which only needs to unmute its outputs at boot
 * (they power up muted).
 *
 * Hardware facts (XVF3800 schematic / Seeed's own XIAO ESP32-S3 client for
 * the Agora/TEN stack, reference-projects/XVF3800-esp32-client-agora):
 *
 *   I2S (ESP32 = master, XMOS = slave), standard I2S, no MCLK:
 *     WS   = GPIO7      BCLK = GPIO8
 *     DIN  = GPIO43     (XMOS -> ESP32, processed mic signal)
 *     DOUT = GPIO44     (ESP32 -> XMOS, playback signal)
 *     Format: 16 kHz, stereo, 32-bit slots
 *
 *   I2C (100 kHz): SDA = GPIO5, SCL = GPIO6
 *     0x2C = XMOS XVF3800, 0x18 = TI TLV320AIC3104 codec
 *
 * IMPORTANT: GPIO43/44 double as the ESP32-S3's default UART0 TX/RX pins.
 * The console must run on the USB Serial/JTAG controller instead
 * (CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y in sdkconfig.defaults).
 */

#include "board.h"

#include <stdlib.h>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "esp_check.h"
#include "esp_log.h"

#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"

static const char *TAG = "board";

// ---------------------------------------------------------------------------
// Pins - reSpeaker XVF3800 (see file header comment)
// ---------------------------------------------------------------------------

#define BOARD_I2C_SDA      GPIO_NUM_5
#define BOARD_I2C_SCL      GPIO_NUM_6

#define BOARD_I2S_BCLK     GPIO_NUM_8
#define BOARD_I2S_WS       GPIO_NUM_7
#define BOARD_I2S_DIN      GPIO_NUM_43  // XMOS -> ESP32 (microphones)
#define BOARD_I2S_DOUT     GPIO_NUM_44  // ESP32 -> XMOS (speaker path)

#define BOARD_I2S_SAMPLE_RATE  16000
#define BOARD_I2S_SLOT_BITS    32
#define BOARD_I2S_CHANNELS     2

#define AIC3104_I2C_ADDR       0x18

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

static i2c_master_bus_handle_t  i2c_bus;
static i2s_chan_handle_t        i2s_tx;
static i2s_chan_handle_t        i2s_rx;
static esp_codec_dev_handle_t   play_dev;
static esp_codec_dev_handle_t   rec_dev;

// ---------------------------------------------------------------------------
// Null codec interface
//
// esp_codec_dev expects a codec control interface even when - as here - the
// "codec" is the XMOS bridge, which needs no register config over that
// interface (the AIC3104 is managed separately below). This mirrors the
// proven dummy codec of Espressif's codec_board component, minus the GPIO
// handling: every operation is a no-op that simply reports success.
// ---------------------------------------------------------------------------

typedef struct {
    audio_codec_if_t base;
    bool is_open;
} null_codec_t;

static int null_codec_open(const audio_codec_if_t *h, void *cfg, int cfg_size)
{
    null_codec_t *codec = (null_codec_t *)h;
    (void)cfg;
    (void)cfg_size;
    codec->is_open = true;
    return 0;
}

static bool null_codec_is_open(const audio_codec_if_t *h)
{
    return ((null_codec_t *)h)->is_open;
}

static int null_codec_enable(const audio_codec_if_t *h, bool enable)
{
    (void)h;
    (void)enable;
    return 0;
}

static int null_codec_set_fs(const audio_codec_if_t *h, esp_codec_dev_sample_info_t *fs)
{
    (void)h;
    (void)fs;
    return 0;
}

static int null_codec_close(const audio_codec_if_t *h)
{
    ((null_codec_t *)h)->is_open = false;
    return 0;
}

static const audio_codec_if_t *null_codec_new(void)
{
    null_codec_t *codec = calloc(1, sizeof(null_codec_t));
    if (codec == NULL) {
        return NULL;
    }
    codec->base.open = null_codec_open;
    codec->base.is_open = null_codec_is_open;
    codec->base.enable = null_codec_enable;
    codec->base.set_fs = null_codec_set_fs;
    codec->base.close = null_codec_close;
    codec->base.open(&codec->base, NULL, 0);
    return &codec->base;
}

// ---------------------------------------------------------------------------
// Step 1: I2C (AIC3104 speaker codec control; the XMOS sits on the same bus)
// ---------------------------------------------------------------------------

static esp_err_t init_i2c(void)
{
    i2c_master_bus_config_t cfg = {
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .i2c_port = I2C_NUM_0,
        .scl_io_num = BOARD_I2C_SCL,
        .sda_io_num = BOARD_I2C_SDA,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    return i2c_new_master_bus(&cfg, &i2c_bus);
}

// ---------------------------------------------------------------------------
// Step 2: AIC3104 (speaker DAC, driven by the XMOS)
//
// The DAC outputs power up muted; without this register setup (ported from
// Seeed's working XIAO client, aic3104_ng_setup_default) the speaker stays
// silent even with a perfect I2S signal into the XMOS.
// ---------------------------------------------------------------------------

static esp_err_t aic3104_speaker_init(void)
{
    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = AIC3104_I2C_ADDR,
        .scl_speed_hz = 100000,
    };
    i2c_master_dev_handle_t dev = NULL;
    esp_err_t err = i2c_master_bus_add_device(i2c_bus, &dev_cfg, &dev);
    ESP_RETURN_ON_ERROR(err, TAG, "AIC3104 I2C device create failed");

    // Page 0 registers: 0 dB DAC gain, headphone/line-out drivers unmuted
    // and powered up (values from Seeed's reference client).
    static const uint8_t regs[][2] = {
        { 0x00, 0x00 },  // page control -> page 0
        { 0x2B, 0x00 },  // LEFT_DAC_VOLUME   0 dB
        { 0x2C, 0x00 },  // RIGHT_DAC_VOLUME  0 dB
        { 0x33, 0x0D },  // HPLOUT: driver on, gain 0 dB, unmuted
        { 0x41, 0x0D },  // HPROUT: driver on, gain 0 dB, unmuted
        { 0x56, 0x0B },  // LEFT_LOP:  driver on, gain 0 dB, unmuted
        { 0x5D, 0x0B },  // RIGHT_LOP: driver on, gain 0 dB, unmuted
    };
    for (int i = 0; i < (int)(sizeof(regs) / sizeof(regs[0])); i++) {
        uint8_t buf[2] = { regs[i][0], regs[i][1] };
        err = i2c_master_transmit(dev, buf, sizeof(buf), 100);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "AIC3104 reg 0x%02X write failed: %s",
                     regs[i][0], esp_err_to_name(err));
        }
    }
    ESP_LOGI(TAG, "AIC3104 speaker codec initialized (outputs unmuted)");
    return ESP_OK;
}

// ---------------------------------------------------------------------------
// Step 3: I2S bridge to the XMOS (16 kHz / 32-bit / stereo, ESP32 master)
// ---------------------------------------------------------------------------

static esp_err_t init_i2s(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, &i2s_tx, &i2s_rx), TAG,
                        "Failed to create I2S channels");

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(BOARD_I2S_SAMPLE_RATE),
        .slot_cfg = I2S_STD_MSB_SLOT_DEFAULT_CONFIG(BOARD_I2S_SLOT_BITS, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,  // the XVF3800 needs no MCLK from the host
            .bclk = BOARD_I2S_BCLK,
            .ws = BOARD_I2S_WS,
            .dout = BOARD_I2S_DOUT,
            .din = BOARD_I2S_DIN,
        },
    };
    // Channels stay disabled here; esp_codec_dev enables them when the
    // playback/capture devices are opened (and re-checks the format then).
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(i2s_tx, &std_cfg), TAG,
                        "Failed to init I2S TX");
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(i2s_rx, &std_cfg), TAG,
                        "Failed to init I2S RX");
    return ESP_OK;
}

// ---------------------------------------------------------------------------
// Step 4: raw-I2S playback/capture devices
// ---------------------------------------------------------------------------

static esp_err_t init_devices(void)
{
    const audio_codec_if_t *codec = null_codec_new();
    ESP_RETURN_ON_FALSE(codec, ESP_FAIL, TAG, "Null codec init failed");

    // I2S data interfaces (one per direction; esp_codec_dev pairs the TX/RX
    // channels of the same I2S port internally for full duplex operation).
    audio_codec_i2s_cfg_t i2s_out_cfg = {
        .port = I2S_NUM_0,
        .tx_handle = i2s_tx,
    };
    const audio_codec_data_if_t *data_out = audio_codec_new_i2s_data(&i2s_out_cfg);
    ESP_RETURN_ON_FALSE(data_out, ESP_FAIL, TAG, "I2S data interface (out) failed");

    audio_codec_i2s_cfg_t i2s_in_cfg = {
        .port = I2S_NUM_0,
        .rx_handle = i2s_rx,
    };
    const audio_codec_data_if_t *data_in = audio_codec_new_i2s_data(&i2s_in_cfg);
    ESP_RETURN_ON_FALSE(data_in, ESP_FAIL, TAG, "I2S data interface (in) failed");

    esp_codec_dev_cfg_t dev_cfg = {
        .codec_if = codec,
        .data_if = data_out,
        .dev_type = ESP_CODEC_DEV_TYPE_OUT,
    };
    play_dev = esp_codec_dev_new(&dev_cfg);
    ESP_RETURN_ON_FALSE(play_dev, ESP_FAIL, TAG, "Playback device creation failed");

    dev_cfg.data_if = data_in;
    dev_cfg.dev_type = ESP_CODEC_DEV_TYPE_IN;
    rec_dev = esp_codec_dev_new(&dev_cfg);
    ESP_RETURN_ON_FALSE(rec_dev, ESP_FAIL, TAG, "Record device creation failed");

    return ESP_OK;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

void board_init(void)
{
    ESP_LOGI(TAG, "Initializing reSpeaker XVF3800 (XIAO ESP32-S3)");
    ESP_LOGI(TAG, "I2S: %d Hz, %d-bit slots, stereo | BCLK=%d WS=%d DIN=%d DOUT=%d (no MCLK)",
             BOARD_I2S_SAMPLE_RATE, BOARD_I2S_SLOT_BITS,
             BOARD_I2S_BCLK, BOARD_I2S_WS, BOARD_I2S_DIN, BOARD_I2S_DOUT);
    ESP_LOGI(TAG, "I2C: SDA=%d SCL=%d (AIC3104 @ 0x%02X, XMOS @ 0x2C)",
             BOARD_I2C_SDA, BOARD_I2C_SCL, AIC3104_I2C_ADDR);

    // The AIC3104 may not respond depending on the XMOS firmware variant;
    // failures are logged but non-fatal - I2S audio still works.
    ESP_ERROR_CHECK(init_i2c());
    aic3104_speaker_init();
    ESP_ERROR_CHECK(init_i2s());
    ESP_ERROR_CHECK(init_devices());

    ESP_LOGI(TAG, "Board init complete - XMOS I2S bridge ready");
}

esp_codec_dev_handle_t get_playback_handle(void)
{
    return play_dev;
}

esp_codec_dev_handle_t get_record_handle(void)
{
    return rec_dev;
}
