/*
 * Wake word gating - module API.
 *
 * Overlay for the livekit/client-sdk-esp32 `voice_agent` example.
 *
 * Connected standby: the LiveKit room stays joined as usual (one-time greeting
 * at boot, timer announcements and assistant.event data messages keep working),
 * but the microphone publish path is gated by the wake word. See
 * firmware/README.md and docs/architecture.md ("Wake word").
 */
#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    WAKE_WORD_ARMED = 0,  /*!< Listening locally; digital silence is published */
    WAKE_WORD_ACTIVE = 1, /*!< Awake; buffered pre-wake + live audio is published */
} wake_word_state_t;

/**
 * @brief  Initialize the module (engine, buffers, chime task)
 *
 * Called from media_init() before the capture system is built. If
 * CONFIG_WAKE_WORD_ENABLE is disabled, or initialization fails, the module
 * degrades to passthrough mode: audio is always published (the pre-wake-word
 * always-listening behavior).
 */
void wake_word_init(void);

/**
 * @brief  Deinitialize the module and stop background tasks
 */
void wake_word_deinit(void);

/**
 * @brief  Process one captured frame (called from the capture read path)
 *
 * @param[in,out]  samples      Mono 16-bit PCM samples, modified in place:
 *                              zeroed while WAKE_WORD_ARMED.
 * @param[in]      num_samples  Number of samples in `samples`
 */
void wake_word_process_frame(int16_t *samples, int num_samples);

/**
 * @brief  Inform the module of the negotiated capture format
 *
 * Called by the capture source after capability negotiation. If the format is
 * not mono 16 kHz (WakeNet requirement) the module disables itself.
 *
 * @param[in]  sample_rate  Negotiated sample rate in Hz
 * @param[in]  channels     Negotiated channel count
 */
void wake_word_note_format(uint32_t sample_rate, int channels);

/**
 * @brief  Take previously buffered pre-wake audio (flushed on wake)
 *
 * @param[out]  dst        Destination buffer
 * @param[in]   max_bytes  Destination capacity in bytes
 * @return Bytes copied (0 when no buffered audio is pending)
 */
int wake_word_take_pending(uint8_t *dst, int max_bytes);

/**
 * @brief  Re-arm the engine when the capture path (re)opens
 *
 * Called by the gating capture source before the inner source is opened. The
 * esp-capture AEC source frees the process-global esp-sr model list when the
 * capture path is closed, so the engine must be re-initialized on every open.
 */
void wake_word_on_capture_open(void);

/**
 * @brief  Release the engine when the capture path closes
 *
 * Counterpart to wake_word_on_capture_open(); called after the inner source
 * has been closed.
 */
void wake_word_on_capture_close(void);

/**
 * @brief  Current gate state
 *
 * Returns WAKE_WORD_ACTIVE in passthrough mode (gating disabled).
 */
wake_word_state_t wake_word_get_state(void);

/**
 * @brief  LED/UI feedback hooks
 *
 * Weak no-ops in wake_word.c; override them in board code (e.g. to drive the
 * XVF3800 LED ring) to get Echo-style visual feedback. They are called from
 * the capture pipeline task - keep handlers short and non-blocking.
 */
void wake_word_led_on_wake(void);
void wake_word_led_on_idle(void);

#ifdef __cplusplus
}
#endif