#include "voice_session.h"

#include <math.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "cJSON.h"
#include "sdkconfig.h"

#include "chime.h"
#include "example.h"
#include "led_ring.h"
#include "local_timers.h"
#include "mic_source.h"
#include "wake_word.h"
#include "xvf3800.h"

static const char *TAG = "session";

// Feature-flag strings for the ready log (unset bools are not defined).
#if CONFIG_LK_WAKE_WORD
#define WW_ENABLED_STR "on"
#else
#define WW_ENABLED_STR "off"
#endif
#if CONFIG_LK_WAKE_WORD_GATE
#define WW_GATE_STR "on"
#else
#define WW_GATE_STR "off"
#endif

#define SESSION_TICK_MS     100
#define BEAM_FRESH_MS       1000  // max age of an azimuth used for beam lock
#define AZIMUTH_POLL_MS     100   // direction refresh while listening
#define RING_STOP_SPEECH_MS 400   // speech age that stops a ringing timer
#define SRC_FRESH_MS        500   // max age of a source direction sample

// Fallbacks: unset Kconfig bools are not defined in sdkconfig.h.
#ifndef CONFIG_LK_SESSION_DIR_FILTER
#define CONFIG_LK_SESSION_DIR_FILTER 0
#endif
#ifndef CONFIG_LK_SESSION_DIR_TOLERANCE_DEG
#define CONFIG_LK_SESSION_DIR_TOLERANCE_DEG 40
#endif

typedef enum {
    SESSION_EVT_WAKE = 0,
} session_evt_t;

typedef struct {
    bool active;
    bool room_connected;
    int64_t started_ms;
    int64_t last_speech_ms;
    int64_t last_azimuth_ms;
    float azimuth_rad;
    bool azimuth_valid;
    bool speech_seen;
    // Directional speech attribution (background rejection): while the
    // beam is locked on the speaker, VAD activity counts only when the
    // dominant active source (XMOS auto-select azimuth) is within the
    // tolerance of the locked direction. Otherwise it is background
    // (TV, other voices) and does not extend the session.
    float locked_azimuth_rad;     // speaker direction the beam locked onto
    bool locked_valid;
    float src_azimuth_rad;        // latest dominant-source direction
    int64_t src_azimuth_ms;
    bool src_azimuth_valid;
    int32_t bg_speech_ms;         // VAD activity attributed to background
} session_ctx_t;

static session_ctx_t s_sess;
static QueueHandle_t s_events;

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

// Smallest angular distance between two azimuth angles (radians, wrap-safe).
static float angle_diff_rad(float a, float b)
{
    float d = a - b;
    while (d > (float)M_PI) {
        d -= 2.0f * (float)M_PI;
    }
    while (d < -(float)M_PI) {
        d += 2.0f * (float)M_PI;
    }
    return fabsf(d);
}

// True when the currently detected speech can be attributed to the locked
// speaker direction. Without a beam lock, with the filter disabled, or
// without a fresh dominant-source direction it falls back to "yes" - the
// plain VAD behavior used before the directional filter existed.
static bool speech_from_speaker(void)
{
#if CONFIG_LK_SESSION_DIR_FILTER
    if (!s_sess.locked_valid || !xvf3800_beam_is_locked()) {
        return true;
    }
    if (!s_sess.src_azimuth_valid ||
        (now_ms() - s_sess.src_azimuth_ms) > SRC_FRESH_MS) {
        return true;
    }
    const float tolerance = (float)CONFIG_LK_SESSION_DIR_TOLERANCE_DEG
                            * (float)M_PI / 180.0f;
    return angle_diff_rad(s_sess.src_azimuth_rad,
                          s_sess.locked_azimuth_rad) <= tolerance;
#else
    return true;
#endif
}

// ---------------------------------------------------------------------------
// Session lifecycle
// ---------------------------------------------------------------------------

static void session_open(void)
{
    s_sess.active = true;
    s_sess.started_ms = now_ms();
    s_sess.last_speech_ms = s_sess.started_ms;
    s_sess.speech_seen = false;
    s_sess.src_azimuth_valid = false;
    s_sess.bg_speech_ms = 0;

    led_ring_set_state(LED_RING_STATE_WAKE);
    chime_play(CHIME_WAKE);

    // Diagnostics: dump every AEC azimuth slot. While a previous beam lock
    // is still on, slot 0/1 carry the pinned direction and slot 3 (auto)
    // keeps jumping between sources - matching the reference driver, which
    // displays slot 0 while locked and slot 3 otherwise.
    float az[4];
    if (xvf3800_read_azimuth_all(az) == ESP_OK) {
        ESP_LOGI(TAG, "azimuths at wake (deg): fixed1=%.0f fixed2=%.0f free=%.0f auto=%.0f%s",
                 az[0] * 180.0f / (float)M_PI, az[1] * 180.0f / (float)M_PI,
                 az[2] * 180.0f / (float)M_PI, az[3] * 180.0f / (float)M_PI,
                 xvf3800_beam_is_locked() ? " (lock still on!)" : "");
    }

#if CONFIG_LK_BEAM_LOCK
    // Pin the AEC fixed beam to the direction the wake word came from so
    // the published signal keeps isolating that speaker. Only lock with a
    // FRESH direction (reference: <= 500 ms old poll value) - locking onto
    // a stale or chime-echo-corrupted azimuth steers the beam away from the
    // speaker and the agent never hears the command.
    if (s_sess.azimuth_valid &&
        (s_sess.started_ms - s_sess.last_azimuth_ms) <= BEAM_FRESH_MS) {
        xvf3800_beam_lock(s_sess.azimuth_rad);
        s_sess.locked_azimuth_rad = s_sess.azimuth_rad;
        s_sess.locked_valid = true;
    } else {
        ESP_LOGI(TAG, "No fresh azimuth (%s) - beam stays adaptive",
                 s_sess.azimuth_valid ? "stale" : "none yet");
        s_sess.locked_valid = false;
        led_ring_clear_beam();
    }
#endif

#if CONFIG_LK_WAKE_WORD && CONFIG_LK_WAKE_WORD_GATE
    mic_source_set_gate(true);
    ESP_LOGI(TAG, "Session open - mic gate open");
#else
    ESP_LOGI(TAG, "Wake word accepted (publish gating disabled)");
#endif

    wake_word_set_armed(false);
    example_publish_event("{\"event\":\"wake_word\"}");
}

static void session_close(void)
{
    s_sess.active = false;
#if CONFIG_LK_WAKE_WORD && CONFIG_LK_WAKE_WORD_GATE
    mic_source_set_gate(false);
#endif
    if (xvf3800_beam_is_locked()) {
        xvf3800_beam_unlock();
    }
    s_sess.locked_valid = false;
    wake_word_set_armed(true);
    led_ring_set_state(local_timers_is_ringing() ? LED_RING_STATE_TIMER
                                                 : LED_RING_STATE_IDLE);
    ESP_LOGI(TAG, "Session closed after %.1f s (speaker speech: %d, "
             "background-only speech: %.1f s)",
             (now_ms() - s_sess.started_ms) / 1000.0, s_sess.speech_seen,
             s_sess.bg_speech_ms / 1000.0);
}

// Called from the WakeNet fetch task; keep it short - the heavy lifting
// happens in the session task via the event queue.
static void on_wake_word(void *ctx)
{
    (void)ctx;
    if (s_events != NULL) {
        const session_evt_t evt = SESSION_EVT_WAKE;
        xQueueSend(s_events, &evt, 0);
    }
}

// ---------------------------------------------------------------------------
// Periodic task
// ---------------------------------------------------------------------------

static void session_task(void *arg)
{
    int azimuth_divider = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(SESSION_TICK_MS));

        session_evt_t evt;
        if (xQueueReceive(s_events, &evt, 0) == pdTRUE &&
            evt == SESSION_EVT_WAKE) {
            if (!s_sess.active) {
                if (local_timers_is_ringing()) {
                    // Reference flow: a wake word while the timer rings only
                    // stops the ring - the user wakes again for a command.
                    local_timers_ring_stop();
                    example_publish_event(
                        "{\"event\":\"timer_ring_stopped\",\"reason\":\"wake_word\"}");
                } else if (s_sess.room_connected) {
                    session_open();
                } else {
                    ESP_LOGW(TAG, "Wake word ignored - room not connected");
                    led_ring_set_state(LED_RING_STATE_ERROR);
                }
            }
        }

        // Direction tracking: always poll (10 Hz), like the reference's
        // always-on beam sensor - this keeps a fresh pre-wake direction for
        // the beam lock and the ring display. While the beam is locked the
        // auto-select slot jumps between sources, so display the pinned
        // fixed-beam slot instead (reference read_led_beam_direction) - and
        // additionally track the auto-select slot itself: it points at the
        // currently DOMINANT ACTIVE source, which drives the background
        // speech attribution below.
        if (++azimuth_divider >= AZIMUTH_POLL_MS / SESSION_TICK_MS) {
            azimuth_divider = 0;
            float az;
            xvf3800_beam_t slot = xvf3800_beam_is_locked()
                                      ? XVF3800_BEAM_FIXED_1
                                      : XVF3800_BEAM_AUTO;
            if (xvf3800_read_azimuth(&az, slot) == ESP_OK) {
                s_sess.azimuth_rad = az;
                s_sess.last_azimuth_ms = now_ms();
                s_sess.azimuth_valid = true;
                led_ring_set_beam_deg(az * 180.0f / (float)M_PI);
            }
            if (xvf3800_beam_is_locked() &&
                xvf3800_read_azimuth(&az, XVF3800_BEAM_AUTO) == ESP_OK) {
                s_sess.src_azimuth_rad = az;
                s_sess.src_azimuth_ms = now_ms();
                s_sess.src_azimuth_valid = true;
            }
        }

        // Track speech + session lifetime while a session is open.
        if (s_sess.active) {
            const int64_t now = now_ms();
            if (wake_word_ms_since_speech() < (uint32_t)(2 * SESSION_TICK_MS)) {
                // VAD sees speech - but whose? While the beam is locked,
                // only activity attributable to the speaker's direction
                // extends the session; TV/other voices count as background
                // and the end-of-utterance timer keeps running.
                if (speech_from_speaker()) {
                    s_sess.last_speech_ms = now;
                    s_sess.speech_seen = true;
                } else {
                    s_sess.bg_speech_ms += SESSION_TICK_MS;
                }
            }

            const bool silence_after_speech =
                s_sess.speech_seen &&
                (now - s_sess.last_speech_ms) > CONFIG_LK_SESSION_SILENCE_MS;
            const bool never_spoke =
                !s_sess.speech_seen &&
                (now - s_sess.started_ms) > CONFIG_LK_SESSION_NO_SPEECH_MS;
            const bool too_long =
                (now - s_sess.started_ms) > CONFIG_LK_SESSION_MAX_MS;
            if (silence_after_speech || never_spoke || too_long ||
                !s_sess.room_connected) {
                session_close();
            }
        } else if (local_timers_is_ringing() &&
                   wake_word_ms_since_speech() < RING_STOP_SPEECH_MS) {
            // Idle: any speech stops a ringing timer (e.g. "stop").
            local_timers_ring_stop();
            example_publish_event(
                "{\"event\":\"timer_ring_stopped\",\"reason\":\"speech\"}");
        }
    }
}

// ---------------------------------------------------------------------------
// Agent events (from the room's data-receive callback)
// ---------------------------------------------------------------------------

void voice_session_on_agent_event(const char *json, size_t len)
{
    if (json == NULL || len == 0) {
        return;
    }
    cJSON *root = cJSON_ParseWithLength(json, len);
    if (root == NULL) {
        return;
    }
    const cJSON *event = cJSON_GetObjectItem(root, "event");
    if (!cJSON_IsString(event)) {
        cJSON_Delete(root);
        return;
    }
    const char *ev = event->valuestring;

    if (strncmp(ev, "timer.", 6) == 0) {
        const cJSON *id = cJSON_GetObjectItem(root, "id");
        const cJSON *name = cJSON_GetObjectItem(root, "name");
        const cJSON *dur = cJSON_GetObjectItem(root, "duration_seconds");
        local_timers_on_agent_event(ev,
                                    cJSON_IsNumber(id) ? (int)id->valuedouble : 0,
                                    cJSON_IsString(name) ? name->valuestring : NULL,
                                    cJSON_IsNumber(dur) ? (float)dur->valuedouble : 0.0f);
    } else if (strcmp(ev, "session.state") == 0) {
        const cJSON *state = cJSON_GetObjectItem(root, "state");
        if (s_sess.active && cJSON_IsString(state)) {
            if (strcmp(state->valuestring, "thinking") == 0) {
                led_ring_set_state(LED_RING_STATE_THINKING);
            } else if (strcmp(state->valuestring, "speaking") == 0) {
                led_ring_set_state(LED_RING_STATE_SPEAKING);
            } else if (strcmp(state->valuestring, "listening") == 0) {
                led_ring_set_state(LED_RING_STATE_LISTENING);
            }
        }
    }
    cJSON_Delete(root);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

void voice_session_set_room_connected(bool connected)
{
    s_sess.room_connected = connected;
    led_ring_set_room_connected(connected);
    if (!connected && s_sess.active) {
        session_close();
    }
}

bool voice_session_is_active(void)
{
    return s_sess.active;
}

esp_err_t voice_session_init(void)
{
    memset(&s_sess, 0, sizeof(s_sess));
    s_events = xQueueCreate(4, sizeof(session_evt_t));
    if (s_events == NULL) {
        return ESP_ERR_NO_MEM;
    }
    const wake_word_config_t ww_cfg = {
        .on_detected = on_wake_word,
        .ctx = NULL,
    };
    esp_err_t err = wake_word_init(&ww_cfg);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Wake word init failed: %s (detection disabled)",
                 esp_err_to_name(err));
    }
    if (xTaskCreate(session_task, "session", 4096, NULL, 4, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "Voice session orchestrator ready (wake word %s, gate %s)",
             WW_ENABLED_STR, WW_GATE_STR);
    return ESP_OK;
}


