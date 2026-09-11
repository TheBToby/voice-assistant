#include "local_timers.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "sdkconfig.h"

#include "chime.h"
#include "led_ring.h"
#include "voice_session.h"

static const char *TAG = "timers";

#define MAX_TIMERS 8

typedef struct {
    int id;
    char name[40];
    int64_t expire_at_ms;
    float total_s;   // original duration, for the LED countdown bar
    bool active;
} local_timer_t;

typedef struct {
    local_timer_t timers[MAX_TIMERS];
    SemaphoreHandle_t lock;
    volatile bool ring_pending;  // a timer expired; waiting for an idle device
    volatile bool ring_started;  // the chime ring loop is active
} timers_ctx_t;

static timers_ctx_t s_t;

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

static local_timer_t *find_free_slot(void)
{
    for (int i = 0; i < MAX_TIMERS; i++) {
        if (!s_t.timers[i].active) {
            return &s_t.timers[i];
        }
    }
    return NULL;
}

static local_timer_t *find_by_id(int id)
{
    if (id <= 0) {
        return NULL;
    }
    for (int i = 0; i < MAX_TIMERS; i++) {
        if (s_t.timers[i].active && s_t.timers[i].id == id) {
            return &s_t.timers[i];
        }
    }
    return NULL;
}

// Start the audible ring. Called only on the pending -> started transition
// so the chime's own timeout actually ends the ring.
static void ring_begin(void)
{
    if (chime_is_ringing()) {
        return;
    }
    ESP_LOGI(TAG, "Timer finished - ringing (wake word or any speech stops it)");
    chime_ring_start();
    led_ring_set_state(LED_RING_STATE_TIMER);
}

// Watchdog: expires mirrored timers, publishes the countdown progress for
// the LED ring's timer_tick bar, and starts the ring as soon as the device
// is idle. The ring itself (repeats + total timeout) lives in the chime
// task; stopping is driven by voice_session.c on wake word / speech.
static void timers_task(void *arg)
{
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(200));
        const int64_t now = now_ms();
        bool expired = false;
        float min_ratio = 0.0f;

        xSemaphoreTake(s_t.lock, portMAX_DELAY);
        for (int i = 0; i < MAX_TIMERS; i++) {
            local_timer_t *t = &s_t.timers[i];
            if (t->active && t->expire_at_ms <= now) {
                ESP_LOGI(TAG, "Timer '%s' (id %d) expired locally", t->name, t->id);
                t->active = false;
                expired = true;
                continue;
            }
            if (t->active && t->total_s > 0.0f) {
                float left = (float)((t->expire_at_ms - now) / 1000);
                float ratio = left / t->total_s;
                if (ratio > min_ratio) {
                    min_ratio = ratio; // bar shows the longest-running timer
                }
            }
        }
        xSemaphoreGive(s_t.lock);

        if (expired && !s_t.ring_pending && !s_t.ring_started) {
            s_t.ring_pending = true;
            if (!voice_session_is_active()) {
                ESP_LOGD(TAG, "Device idle - ringing now");
            } else {
                ESP_LOGD(TAG, "Session open - deferring ring until idle");
            }
        }
        if (s_t.ring_pending && !voice_session_is_active()) {
            s_t.ring_pending = false;
            s_t.ring_started = true;
            ring_begin();
        }
        // Idle countdown bar (reference timer_tick effect).
        led_ring_set_timer_progress(
            (s_t.ring_pending || s_t.ring_started) ? 0.0f : min_ratio);
    }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

void local_timers_on_agent_event(const char *event, int id,
                                 const char *name, float duration_s)
{
    if (event == NULL) {
        return;
    }
    if (strcmp(event, "timer.set") == 0) {
        if (duration_s <= 0) {
            return;
        }
        xSemaphoreTake(s_t.lock, portMAX_DELAY);
        local_timer_t *t = find_by_id(id);
        if (t == NULL) {
            t = find_free_slot();
        }
        if (t == NULL) {
            xSemaphoreGive(s_t.lock);
            ESP_LOGW(TAG, "No timer slot free (max %d)", MAX_TIMERS);
            return;
        }
        t->id = id;
        t->active = true;
        t->expire_at_ms = now_ms() + (int64_t)(duration_s * 1000.0f);
        t->total_s = duration_s;
        snprintf(t->name, sizeof(t->name), "%s", name ? name : "timer");
        xSemaphoreGive(s_t.lock);
        ESP_LOGI(TAG, "Timer '%s' (%.0f s) tracked locally", t->name, duration_s);
        return;
    }
    if (strcmp(event, "timer.cancel") == 0) {
        xSemaphoreTake(s_t.lock, portMAX_DELAY);
        local_timer_t *t = find_by_id(id);
        if (t != NULL) {
            ESP_LOGI(TAG, "Timer '%s' cancelled", t->name);
            t->active = false;
        }
        xSemaphoreGive(s_t.lock);
        return;
    }
    if (strcmp(event, "timer.expired") == 0) {
        // The agent's own countdown fired (e.g. the set event was missed).
        // Make sure the device rings either way.
        local_timer_t *t = NULL;
        xSemaphoreTake(s_t.lock, portMAX_DELAY);
        t = find_by_id(id);
        if (t != NULL) {
            t->expire_at_ms = now_ms(); // expire on the next watchdog pass
        }
        xSemaphoreGive(s_t.lock);
        if (t == NULL && !s_t.ring_pending && !s_t.ring_started) {
            // Unknown timer: ring right away so the announcement is not lost.
            s_t.ring_pending = true;
        }
        return;
    }
}

void local_timers_ring_stop(void)
{
    if (!s_t.ring_pending && !s_t.ring_started) {
        return;
    }
    s_t.ring_pending = false;
    s_t.ring_started = false;
    chime_ring_stop();
    if (led_ring_get_state() == LED_RING_STATE_TIMER) {
        led_ring_set_state(LED_RING_STATE_IDLE);
    }
    ESP_LOGI(TAG, "Timer ring stopped");
}

bool local_timers_is_ringing(void)
{
    return s_t.ring_pending || s_t.ring_started;
}

int local_timers_count(void)
{
    int n = 0;
    xSemaphoreTake(s_t.lock, portMAX_DELAY);
    for (int i = 0; i < MAX_TIMERS; i++) {
        if (s_t.timers[i].active) {
            n++;
        }
    }
    xSemaphoreGive(s_t.lock);
    return n;
}

int local_timers_snapshot(timer_info_t *out, int max)
{
    if (out == NULL || max <= 0) {
        return 0;
    }
    int n = 0;
    const int64_t now = now_ms();
    xSemaphoreTake(s_t.lock, portMAX_DELAY);
    for (int i = 0; i < MAX_TIMERS && n < max; i++) {
        const local_timer_t *t = &s_t.timers[i];
        if (t->active) {
            out[n].id = t->id;
            snprintf(out[n].name, sizeof(out[n].name), "%s", t->name);
            out[n].duration_s = (uint32_t)((t->expire_at_ms - now) / 1000);
            n++;
        }
    }
    xSemaphoreGive(s_t.lock);
    return n;
}

esp_err_t local_timers_init(void)
{
#if !CONFIG_LK_TIMERS
    ESP_LOGI(TAG, "Local timers disabled (CONFIG_LK_TIMERS=n)");
    return ESP_OK;
#else
    memset(&s_t, 0, sizeof(s_t));
    s_t.lock = xSemaphoreCreateMutex();
    if (s_t.lock == NULL) {
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreate(timers_task, "timers", 3072, NULL, 3, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "Local timer mirror ready (ring timeout %d min)",
             CONFIG_LK_TIMER_RING_MINUTES);
    return ESP_OK;
#endif
}

