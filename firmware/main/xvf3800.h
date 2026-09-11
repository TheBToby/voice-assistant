/*
 * XMOS XVF3800 control-port driver (I2C address 0x2C).
 *
 * The XVF3800 exposes a control/servicer interface over the shared I2C bus
 * that lets the host drive the on-board features the audio wire does not
 * cover: the 12-LED ring, the mic mute GPIO, and the AEC beam steering
 * (direction readout + fixed-beam "beam lock").
 *
 * Transport protocol (matches the XMOS sln_voice control servicer, ported
 * from the Respeaker-XVF3800-ESPHome-integration component and Seeed's
 * xvf_host.py):
 *
 *   write : { resid, cmd, len, data[len] }
 *   read  : write { resid, cmd | READ_BIT, len }, then read len bytes,
 *           where the first byte is the status:
 *             0x00 DONE, 0x01 WAIT, 0x03 INVALID, 0x40 RETRY
 *
 * A WAIT/RETRY status means "no fresh data yet" (typical for the azimuth
 * read during silence). Reads are single-attempt and never block; callers
 * simply poll again later.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/// Number of LEDs on the XVF3800 ring.
#define XVF3800_LED_COUNT 12

/// AEC azimuth beam slots returned by the azimuth read command.
typedef enum {
    XVF3800_BEAM_FIXED_1 = 0,   ///< Fixed beam 1 (valid while beam lock is on)
    XVF3800_BEAM_FIXED_2 = 1,   ///< Fixed beam 2 (valid while beam lock is on)
    XVF3800_BEAM_FREE = 2,      ///< Free-running adaptive beam
    XVF3800_BEAM_AUTO = 3,      ///< Auto-selected beam (default output)
} xvf3800_beam_t;

/// Initialize the control interface: registers the 0x2C device on the shared
/// board I2C bus and reads the XMOS firmware version.
///
/// Waits up to ~3 s for the XMOS DSP to finish booting. Non-fatal on failure
/// (the audio path does not depend on the control port); all functions then
/// return ESP_ERR_INVALID_STATE until a later `xvf3800_init()` succeeds.
esp_err_t xvf3800_init(void);

/// True once `xvf3800_init()` succeeded (control port usable).
bool xvf3800_ready(void);

/// XMOS application firmware version (read at init).
esp_err_t xvf3800_get_fw_version(uint8_t *major, uint8_t *minor, uint8_t *patch);

/// Write one full ring frame (12 colors, 0x00RRGGBB each).
/// Skips the bus transaction when the frame equals the previous one.
esp_err_t xvf3800_set_led_ring(const uint32_t colors[XVF3800_LED_COUNT]);

/// Read the current AEC beam azimuth in radians (-pi..pi) for `beam`.
/// Single attempt: returns ESP_ERR_NOT_FOUND on WAIT/RETRY (no fresh data).
esp_err_t xvf3800_read_azimuth(float *azimuth_rad, xvf3800_beam_t beam);

/// Read all four AEC azimuth slots in one transaction (radians):
/// [fixed 1, fixed 2, free-running, auto-select]. Diagnostics helper.
esp_err_t xvf3800_read_azimuth_all(float azimuth_rad[4]);

/// Pin the AEC fixed beams to `azimuth_rad` and enable fixed-beam mode, so
/// the published mic signal keeps pointing at the speaker that triggered the
/// wake word. Call `xvf3800_beam_unlock()` at the end of the utterance.
esp_err_t xvf3800_beam_lock(float azimuth_rad);

/// Return the AEC to its adaptive (auto-select) beam.
esp_err_t xvf3800_beam_unlock(void);

/// True while the fixed-beam lock is engaged.
bool xvf3800_beam_is_locked(void);

/// Read the DSP mic mute state (true = muted).
esp_err_t xvf3800_read_mute(bool *muted);

/// Mute/unmute the XMOS microphone path (GPO 30).
esp_err_t xvf3800_set_mute(bool mute);

#ifdef __cplusplus
}
#endif
