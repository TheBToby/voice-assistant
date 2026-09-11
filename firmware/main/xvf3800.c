/*
 * XMOS XVF3800 control-port driver - see xvf3800.h for the protocol notes.
 *
 * Register map used here (servicer ResID, command):
 *
 *   GPO servicer (20):
 *     cmd 0  (rw) GPO_READ_VALUES   5 bytes, bit-packed GPIO readback
 *     cmd 1  (wo) GPO_WRITE_VALUE   { gpio, value }
 *     cmd 18 (wo) LED_RING_VALUE    48 bytes = 12 LEDs x [r, g, b, 0]
 *     GPIO 30 = DSP mic mute (1 = muted)
 *
 *   AEC servicer (33):
 *     cmd 37 (rw) FIXEDBEAMS_ONOFF      int32, 0 = adaptive, 1 = fixed
 *     cmd 75 (r)  AZIMUTH_VALUES        4 x float32 rad:
 *                                       [fixed1, fixed2, free, auto-select]
 *     cmd 81 (wo) FIXEDBEAMS_AZIMUTH    2 x float32 rad (fixed beam 1 + 2)
 *
 *   DFU controller servicer (240):
 *     cmd 88 (r)  GETVERSION            [status, major, minor, patch]
 */

#include "xvf3800.h"

#include <math.h>
#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "board.h"

static const char *TAG = "xvf3800";

// ---------------------------------------------------------------------------
// Servicer / command constants (see file header)
// ---------------------------------------------------------------------------

#define XMOS_I2C_ADDR              0x2C

#define GPO_SERVICER_RESID         20
#define GPO_CMD_READ_VALUES        0
#define GPO_CMD_WRITE_VALUE        1
#define GPO_CMD_LED_RING_VALUE     18
#define GPO_READ_NUM_BYTES         5
#define GPO_MUTE_GPIO              30
#define GPO_MUTE_BYTE              1   // GPO_READ_VALUES byte holding GPIO30
#define GPO_MUTE_BIT               0x01

#define AEC_SERVICER_RESID         33
#define AEC_CMD_FIXEDBEAMS_ONOFF   37
#define AEC_CMD_AZIMUTH_VALUES     75
#define AEC_CMD_FIXEDBEAMS_AZIMUTH 81

#define DFU_SERVICER_RESID         240
#define DFU_CMD_GETVERSION         88

#define XMOS_CMD_READ_BIT          0x80

// Transport status codes (first byte of a read response).
#define XMOS_STATUS_DONE           0x00
#define XMOS_STATUS_WAIT           0x01
#define XMOS_STATUS_INVALID        0x03
#define XMOS_STATUS_RETRY          0x40

#define I2C_TIMEOUT_MS             50
#define XMOS_BOOT_TIMEOUT_MS       3000
#define XMOS_BOOT_POLL_MS          200

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

typedef struct {
    i2c_master_bus_handle_t bus;
    i2c_master_dev_handle_t dev;
    SemaphoreHandle_t lock;
    bool ready;
    bool beam_locked;
    uint8_t fw_major;
    uint8_t fw_minor;
    uint8_t fw_patch;
    // Last LED frame written; an unchanged frame skips the bus.
    uint32_t last_led_frame[XVF3800_LED_COUNT];
    bool last_led_valid;
} xvf3800_state_t;

static xvf3800_state_t s_xvf;

// ---------------------------------------------------------------------------
// Low-level transport
// ---------------------------------------------------------------------------

static esp_err_t xmos_write(uint8_t resid, uint8_t cmd,
                            const uint8_t *data, uint8_t len)
{
    uint8_t buf[3 + 48]; // largest payload is the 48-byte LED frame
    if (len > sizeof(buf) - 3) {
        return ESP_ERR_INVALID_SIZE;
    }
    buf[0] = resid;
    buf[1] = cmd;
    buf[2] = len;
    if (len > 0 && data != NULL) {
        memcpy(&buf[3], data, len);
    }
    return i2c_master_transmit(s_xvf.dev, buf, len + 3, I2C_TIMEOUT_MS);
}

static esp_err_t xmos_read(uint8_t resid, uint8_t cmd,
                           uint8_t *data, uint8_t len)
{
    uint8_t req[3] = { resid, (uint8_t)(cmd | XMOS_CMD_READ_BIT), len };
    esp_err_t err = i2c_master_transmit(s_xvf.dev, req, sizeof(req), I2C_TIMEOUT_MS);
    if (err != ESP_OK) {
        return err;
    }
    return i2c_master_receive(s_xvf.dev, data, len, I2C_TIMEOUT_MS);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

bool xvf3800_ready(void)
{
    return s_xvf.ready;
}

esp_err_t xvf3800_get_fw_version(uint8_t *major, uint8_t *minor, uint8_t *patch)
{
    if (!s_xvf.ready) {
        return ESP_ERR_INVALID_STATE;
    }
    if (major) {
        *major = s_xvf.fw_major;
    }
    if (minor) {
        *minor = s_xvf.fw_minor;
    }
    if (patch) {
        *patch = s_xvf.fw_patch;
    }
    return ESP_OK;
}

esp_err_t xvf3800_init(void)
{
    if (s_xvf.ready) {
        return ESP_OK;
    }
    if (s_xvf.lock == NULL) {
        s_xvf.lock = xSemaphoreCreateMutex();
        if (s_xvf.lock == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }

    s_xvf.bus = board_get_i2c_bus();
    if (s_xvf.bus == NULL) {
        ESP_LOGE(TAG, "No I2C bus - XMOS control port unavailable");
        return ESP_ERR_INVALID_STATE;
    }

    // The XMOS DSP needs ~2 s to boot after power-up; poll for it.
    esp_err_t err = ESP_ERR_NOT_FOUND;
    for (int delay = 0; delay <= XMOS_BOOT_TIMEOUT_MS; delay += XMOS_BOOT_POLL_MS) {
        err = i2c_master_probe(s_xvf.bus, XMOS_I2C_ADDR, I2C_TIMEOUT_MS);
        if (err == ESP_OK) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(XMOS_BOOT_POLL_MS));
    }
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "XMOS control port 0x2C not responding (audio still works)");
        return err;
    }

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = XMOS_I2C_ADDR,
        .scl_speed_hz = 100000,
    };
    err = i2c_master_bus_add_device(s_xvf.bus, &dev_cfg, &s_xvf.dev);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Failed to register XMOS I2C device: %s", esp_err_to_name(err));
        return err;
    }

    // Read the firmware version as the bring-up check.
    uint8_t ver[4] = {0};
    xSemaphoreTake(s_xvf.lock, portMAX_DELAY);
    err = xmos_read(DFU_SERVICER_RESID, DFU_CMD_GETVERSION, ver, sizeof(ver));
    xSemaphoreGive(s_xvf.lock);
    if (err != ESP_OK || ver[0] != XMOS_STATUS_DONE) {
        ESP_LOGW(TAG, "XMOS version read failed (err=%s status=0x%02X)",
                 esp_err_to_name(err), ver[0]);
        return (err != ESP_OK) ? err : ESP_ERR_INVALID_RESPONSE;
    }
    s_xvf.fw_major = ver[1];
    s_xvf.fw_minor = ver[2];
    s_xvf.fw_patch = ver[3];
    s_xvf.last_led_valid = false;
    s_xvf.beam_locked = false;
    s_xvf.ready = true;

    ESP_LOGI(TAG, "XMOS XVF3800 control port ready (fw %u.%u.%u)",
             s_xvf.fw_major, s_xvf.fw_minor, s_xvf.fw_patch);
    return ESP_OK;
}

esp_err_t xvf3800_set_led_ring(const uint32_t colors[XVF3800_LED_COUNT])
{
    if (!s_xvf.ready) {
        return ESP_ERR_INVALID_STATE;
    }
    if (colors == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_xvf.last_led_valid &&
        memcmp(s_xvf.last_led_frame, colors, sizeof(s_xvf.last_led_frame)) == 0) {
        return ESP_OK;
    }

    // Frame layout: 12 x [r, g, b, 0] (reference driver byte order).
    uint8_t payload[XVF3800_LED_COUNT * 4];
    for (int i = 0; i < XVF3800_LED_COUNT; i++) {
        payload[i * 4 + 0] = (uint8_t)(colors[i] & 0xFF);         // red
        payload[i * 4 + 1] = (uint8_t)((colors[i] >> 8) & 0xFF);  // green
        payload[i * 4 + 2] = (uint8_t)((colors[i] >> 16) & 0xFF); // blue
        payload[i * 4 + 3] = 0x00;
    }

    xSemaphoreTake(s_xvf.lock, portMAX_DELAY);
    esp_err_t err = xmos_write(GPO_SERVICER_RESID, GPO_CMD_LED_RING_VALUE,
                               payload, sizeof(payload));
    xSemaphoreGive(s_xvf.lock);
    if (err == ESP_OK) {
        memcpy(s_xvf.last_led_frame, colors, sizeof(s_xvf.last_led_frame));
        s_xvf.last_led_valid = true;
    } else {
        ESP_LOGD(TAG, "LED ring write failed: %s", esp_err_to_name(err));
    }
    return err;
}

esp_err_t xvf3800_read_azimuth(float *azimuth_rad, xvf3800_beam_t beam)
{
    if (!s_xvf.ready) {
        return ESP_ERR_INVALID_STATE;
    }
    if (azimuth_rad == NULL || beam > XVF3800_BEAM_AUTO) {
        return ESP_ERR_INVALID_ARG;
    }

    // Response: [status][4 x float32 rad] (little-endian XS3 layout).
    uint8_t resp[17] = {0};
    xSemaphoreTake(s_xvf.lock, portMAX_DELAY);
    esp_err_t err = xmos_read(AEC_SERVICER_RESID, AEC_CMD_AZIMUTH_VALUES,
                              resp, sizeof(resp));
    xSemaphoreGive(s_xvf.lock);
    if (err != ESP_OK) {
        return err;
    }
    if (resp[0] != XMOS_STATUS_DONE) {
        // WAIT/RETRY is normal while the DSP has no sound source to localize.
        return ESP_ERR_NOT_FOUND;
    }
    float radians;
    memcpy(&radians, &resp[1 + (int)beam * sizeof(float)], sizeof(float));
    *azimuth_rad = radians;
    return ESP_OK;
}

esp_err_t xvf3800_beam_lock(float azimuth_rad)
{
    if (!s_xvf.ready) {
        return ESP_ERR_INVALID_STATE;
    }
    // Point both fixed beams at the same azimuth so whichever beam is gated
    // picks up the source (per the reference integration).
    uint8_t az[2 * sizeof(float)];
    memcpy(&az[0], &azimuth_rad, sizeof(float));
    memcpy(&az[sizeof(float)], &azimuth_rad, sizeof(float));

    xSemaphoreTake(s_xvf.lock, portMAX_DELAY);
    esp_err_t err = xmos_write(AEC_SERVICER_RESID, AEC_CMD_FIXEDBEAMS_AZIMUTH,
                               az, sizeof(az));
    if (err == ESP_OK) {
        // int32, little-endian on the XS3.
        const uint8_t on[4] = {0x01, 0x00, 0x00, 0x00};
        err = xmos_write(AEC_SERVICER_RESID, AEC_CMD_FIXEDBEAMS_ONOFF, on, sizeof(on));
    }
    xSemaphoreGive(s_xvf.lock);
    if (err == ESP_OK) {
        s_xvf.beam_locked = true;
        ESP_LOGI(TAG, "Beam locked at %.1f deg", azimuth_rad * 180.0f / (float)M_PI);
    } else {
        ESP_LOGW(TAG, "Beam lock failed: %s", esp_err_to_name(err));
    }
    return err;
}

esp_err_t xvf3800_beam_unlock(void)
{
    if (!s_xvf.ready) {
        return ESP_ERR_INVALID_STATE;
    }
    const uint8_t off[4] = {0x00, 0x00, 0x00, 0x00};
    xSemaphoreTake(s_xvf.lock, portMAX_DELAY);
    esp_err_t err = xmos_write(AEC_SERVICER_RESID, AEC_CMD_FIXEDBEAMS_ONOFF,
                               off, sizeof(off));
    xSemaphoreGive(s_xvf.lock);
    if (err == ESP_OK) {
        s_xvf.beam_locked = false;
        ESP_LOGI(TAG, "Beam lock released");
    }
    return err;
}

bool xvf3800_beam_is_locked(void)
{
    return s_xvf.beam_locked;
}

esp_err_t xvf3800_read_mute(bool *muted)
{
    if (!s_xvf.ready) {
        return ESP_ERR_INVALID_STATE;
    }
    if (muted == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    uint8_t resp[1 + GPO_READ_NUM_BYTES] = {0};
    xSemaphoreTake(s_xvf.lock, portMAX_DELAY);
    esp_err_t err = xmos_read(GPO_SERVICER_RESID, GPO_CMD_READ_VALUES,
                              resp, sizeof(resp));
    xSemaphoreGive(s_xvf.lock);
    if (err != ESP_OK) {
        return err;
    }
    if (resp[0] != XMOS_STATUS_DONE) {
        return ESP_ERR_NOT_FOUND;
    }
    *muted = (resp[1 + GPO_MUTE_BYTE] & GPO_MUTE_BIT) != 0;
    return ESP_OK;
}

esp_err_t xvf3800_set_mute(bool mute)
{
    if (!s_xvf.ready) {
        return ESP_ERR_INVALID_STATE;
    }
    const uint8_t payload[2] = { GPO_MUTE_GPIO, mute ? 1 : 0 };
    xSemaphoreTake(s_xvf.lock, portMAX_DELAY);
    esp_err_t err = xmos_write(GPO_SERVICER_RESID, GPO_CMD_WRITE_VALUE,
                               payload, sizeof(payload));
    xSemaphoreGive(s_xvf.lock);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Mute write failed: %s", esp_err_to_name(err));
    }
    return err;
}


