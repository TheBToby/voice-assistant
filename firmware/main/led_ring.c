/*
 * LED ring effects engine - a C port of the reference integration's
 * packages/leds.yaml animation logic. See led_ring.h for the state table.
 */

#include "led_ring.h"

#include <math.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "sdkconfig.h"
#include "xvf3800.h"

static const char *TAG = "led_ring";

// Fallbacks for when the feature (and thus the symbol) is disabled.
#ifndef CONFIG_LK_LED_BRIGHTNESS_PERCENT
#define CONFIG_LK_LED_BRIGHTNESS_PERCENT 80
#endif
#ifndef CONFIG_LK_LED_COLOR
#define CONFIG_LK_LED_COLOR 0xFF00FF
#endif

#define TICK_MS            50       // render interval (reference: 50 ms)
#define WAKE_SNAPSHOT_MS   700      // WAKE (beam snapshot) -> LISTENING
#define ERROR_HOLD_MS      2000     // ERROR breathe duration before fallback
#define MUTE_POLL_TICKS    40       // idle mute poll period (ticks) = 2 s

// User ring color (reference globals user_led_ring_color_*; default magenta).
#define USER_R  (((CONFIG_LK_LED_COLOR >> 16) & 0xFF))
#define USER_G  (((CONFIG_LK_LED_COLOR >> 8) & 0xFF))
#define USER_B  ((CONFIG_LK_LED_COLOR & 0xFF))
#define MUTED_R 200   // dim red for the muted ring

// Master level (reference global user_led_ring_brightness, default 0.8).
#define MASTER_BRIGHTNESS ((CONFIG_LK_LED_BRIGHTNESS_PERCENT) / 100.0f)

// Effect implementations (types mirror the reference's led_set_effect args).
typedef enum {
    EFFECT_OFF = 0,
    EFFECT_SOLID,
    EFFECT_BREATHE,
    EFFECT_COMET,      // counter-clockwise (reference "comet_ccw")
    EFFECT_LED_BEAM,
    EFFECT_TIMER_TICK,
} effect_type_t;

typedef struct {
    effect_type_t type;
    uint8_t r, g, b;
    float speed;       // cycles (breathe) / revolutions (comet) per second
    float brightness;  // effect brightness 0..1, on top of the master level
} led_effect_t;

typedef struct {
    led_ring_state_t state;
    led_ring_state_t prev;        // state to fall back to after ERROR
    int64_t state_since_us;
    uint32_t last_tick_ms;        // for dt-based animations
    led_effect_t effect;
    float beam_deg;               // latest beam azimuth (deg)
    bool beam_valid;
    float beam_pos;               // animated (eased) beam position, in LEDs
    float timer_ratio;            // countdown bar progress of the next timer
    bool room_connected;
    bool muted;                   // last polled XMOS mute state
    int mute_poll;
    int64_t tick_no;
} led_ctx_t;

static led_ctx_t s_led;

static uint8_t mix(uint8_t c, float factor)
{
    float v = (float)c * factor;
    if (v > 255.0f) {
        v = 255.0f;
    }
    return (uint8_t)v;
}

static uint32_t to_rgb(uint8_t r, uint8_t g, uint8_t b)
{
    return ((uint32_t)r << 16) | ((uint32_t)g << 8) | b;
}

// ---------------------------------------------------------------------------
// Effect renderers (ported from packages/leds.yaml)
// ---------------------------------------------------------------------------

static void render_solid(uint32_t *colors, const led_effect_t *e)
{
    float level = MASTER_BRIGHTNESS * e->brightness;
    for (int i = 0; i < XVF3800_LED_COUNT; i++) {
        colors[i] = to_rgb(mix(e->r, level), mix(e->g, level), mix(e->b, level));
    }
}

static void render_breathe(uint32_t *colors, const led_effect_t *e, float dt)
{
    // Reference update_breathe_effect: phase += dt * speed (cycles/s).
    static float phase;
    phase += dt * e->speed;
    while (phase >= 1.0f) {
        phase -= 1.0f;
    }
    float level = 0.5f * (1.0f + sinf(phase * 2.0f * (float)M_PI)) *
                  MASTER_BRIGHTNESS * e->brightness;
    for (int i = 0; i < XVF3800_LED_COUNT; i++) {
        colors[i] = to_rgb(mix(e->r, level), mix(e->g, level), mix(e->b, level));
    }
}

static void render_comet(uint32_t *colors, const led_effect_t *e, float dt)
{
    // Reference update_comet_ccw_effect: tail trails clockwise behind a
    // counter-clockwise head; tail length = 3 + speed.
    static float comet_pos;
    const int base_tail = 3;
    float leds_per_sec = e->speed * XVF3800_LED_COUNT;
    comet_pos -= dt * leds_per_sec;
    while (comet_pos < 0.0f) {
        comet_pos += XVF3800_LED_COUNT;
    }

    int head = (int)comet_pos % XVF3800_LED_COUNT;
    int tail_length = base_tail + (int)e->speed;
    if (tail_length > XVF3800_LED_COUNT - 1) {
        tail_length = XVF3800_LED_COUNT - 1;
    }

    float level = MASTER_BRIGHTNESS * e->brightness;
    memset(colors, 0, XVF3800_LED_COUNT * sizeof(uint32_t));
    colors[head] = to_rgb(mix(e->r, level), mix(e->g, level), mix(e->b, level));
    for (int i = 1; i <= tail_length; i++) {
        float tail_factor = (float)i / (tail_length + 1);
        float tail_level = (1.0f - tail_factor) * level;
        int idx = (head + i) % XVF3800_LED_COUNT; // clockwise behind the head
        colors[idx] = to_rgb(mix(e->r, tail_level), mix(e->g, tail_level),
                             mix(e->b, tail_level));
    }
}

static void render_led_beam(uint32_t *colors, const led_effect_t *e, float dt)
{
    // Reference update_led_beam_effect: target = (sensor_led + 5) % 12,
    // eased 0.5 s towards the target, 4-LED circular falloff.
    const int fade_leds = 3;
    const float transition_s = 0.5f;

    memset(colors, 0, XVF3800_LED_COUNT * sizeof(uint32_t));
    if (!s_led.beam_valid) {
        // No fresh DSP direction: keep a faint solid ring so the device
        // still looks alive (reference renders dark here).
        float level = 0.15f * MASTER_BRIGHTNESS * e->brightness;
        for (int i = 0; i < XVF3800_LED_COUNT; i++) {
            colors[i] = to_rgb(mix(e->r, level), mix(e->g, level),
                               mix(e->b, level));
        }
        return;
    }

    int sensor_led = (int)lroundf(s_led.beam_deg / 30.0f) % XVF3800_LED_COUNT;
    if (sensor_led < 0) {
        sensor_led += XVF3800_LED_COUNT;
    }
    float target = (float)((sensor_led + 5) % XVF3800_LED_COUNT);

    float diff = target - s_led.beam_pos;
    if (diff > XVF3800_LED_COUNT / 2.0f) {
        diff -= XVF3800_LED_COUNT;
    } else if (diff < -XVF3800_LED_COUNT / 2.0f) {
        diff += XVF3800_LED_COUNT;
    }
    if (fabsf(diff) > 0.01f) {
        s_led.beam_pos += (diff / transition_s) * dt;
    } else {
        s_led.beam_pos = target;
    }
    if (s_led.beam_pos >= XVF3800_LED_COUNT) {
        s_led.beam_pos -= XVF3800_LED_COUNT;
    }
    if (s_led.beam_pos < 0.0f) {
        s_led.beam_pos += XVF3800_LED_COUNT;
    }

    float master = MASTER_BRIGHTNESS * e->brightness;
    for (int i = 0; i < XVF3800_LED_COUNT; i++) {
        float dist = fabsf((float)i - s_led.beam_pos);
        if (dist > XVF3800_LED_COUNT / 2.0f) {
            dist = XVF3800_LED_COUNT - dist;
        }
        float factor = 1.0f - (dist / (fade_leds + 1.0f));
        if (factor > 0.0f) {
            float level = factor * master;
            colors[i] = to_rgb(mix(e->r, level), mix(e->g, level),
                               mix(e->b, level));
        }
    }
}

static void render_timer_tick(uint32_t *colors, const led_effect_t *e)
{
    // Reference update_timer_tick_effect: countdown bar (ratio * 12 LEDs)
    // with one LED dipping every 100 ms, walking backwards around the ring.
    static int tick_index;
    static int64_t last_tick;
    if (s_led.tick_no - last_tick >= 2) { // 100 ms at a 50 ms tick
        tick_index = (tick_index - 1 + XVF3800_LED_COUNT) % XVF3800_LED_COUNT;
        last_tick = s_led.tick_no;
    }

    float master = MASTER_BRIGHTNESS * e->brightness;
    memset(colors, 0, XVF3800_LED_COUNT * sizeof(uint32_t));
    for (int i = 0; i < XVF3800_LED_COUNT; i++) {
        float bar = s_led.timer_ratio * XVF3800_LED_COUNT - i;
        if (bar > 0.0f) {
            if (bar > 1.0f) {
                bar = 1.0f;
            }
            float dip = (i == tick_index) ? 0.1f : 0.0f;
            float level = bar * (1.0f - dip) * master;
            colors[i] = to_rgb(mix(e->r, level), mix(e->g, level),
                               mix(e->b, level));
        }
    }
}

// ---------------------------------------------------------------------------
// State -> effect mapping (control_leds_* scripts in packages/leds.yaml)
// ---------------------------------------------------------------------------

static const led_effect_t *effect_for_state(led_ring_state_t state)
{
    static const led_effect_t idle      = {EFFECT_SOLID, USER_R, USER_G, USER_B, 0.0f, 1.0f};
    static const led_effect_t wake      = {EFFECT_LED_BEAM, USER_R, USER_G, USER_B, 0.0f, 0.8f};
    static const led_effect_t listening = {EFFECT_LED_BEAM, USER_R, USER_G, USER_B, 0.0f, 1.0f};
    static const led_effect_t thinking  = {EFFECT_BREATHE, USER_R, USER_G, USER_B, 1.0f, 0.6f};
    static const led_effect_t speaking  = {EFFECT_COMET, USER_R, USER_G, USER_B, 1.0f, 0.8f};
    static const led_effect_t timer     = {EFFECT_BREATHE, USER_R, USER_G, USER_B, 5.0f, 1.0f};
    static const led_effect_t error     = {EFFECT_BREATHE, 255, 0, 0, 3.0f, 0.8f};
    switch (state) {
    case LED_RING_STATE_IDLE:      return &idle;
    case LED_RING_STATE_WAKE:      return &wake;
    case LED_RING_STATE_LISTENING: return &listening;
    case LED_RING_STATE_THINKING:  return &thinking;
    case LED_RING_STATE_SPEAKING:  return &speaking;
    case LED_RING_STATE_TIMER:     return &timer;
    case LED_RING_STATE_ERROR:     return &error;
    default:                       return &idle;
    }
}

static void render_frame(uint32_t *colors, float dt)
{
    memset(colors, 0, XVF3800_LED_COUNT * sizeof(uint32_t));
    if (s_led.state == LED_RING_STATE_OFF) {
        return;
    }
    if (s_led.state == LED_RING_STATE_IDLE && s_led.muted) {
        // Local extension: dim red ring while the DSP mic is muted.
        for (int i = 0; i < XVF3800_LED_COUNT; i++) {
            colors[i] = to_rgb(mix(MUTED_R, 70.0f / 255.0f), 0, 0);
        }
        return;
    }
    if (s_led.state == LED_RING_STATE_IDLE && s_led.timer_ratio > 0.0f) {
        // A countdown is running: show the timer_tick bar while idle
        // (reference: control_leds -> control_leds_timer_ticking).
        static const led_effect_t tick = {EFFECT_TIMER_TICK, USER_R, USER_G, USER_B, 1.0f, 0.7f};
        render_timer_tick(colors, &tick);
        return;
    }
    const led_effect_t *e = effect_for_state(s_led.state);
    switch (e->type) {
    case EFFECT_SOLID:      render_solid(colors, e); break;
    case EFFECT_BREATHE:    render_breathe(colors, e, dt); break;
    case EFFECT_COMET:      render_comet(colors, e, dt); break;
    case EFFECT_LED_BEAM:   render_led_beam(colors, e, dt); break;
    case EFFECT_TIMER_TICK: render_timer_tick(colors, e); break;
    default: break;
    }
}

static void led_task(void *arg)
{
    uint32_t colors[XVF3800_LED_COUNT];
    uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(TICK_MS));
        uint32_t now2 = (uint32_t)(esp_timer_get_time() / 1000);
        float dt = (float)(now2 - now) / 1000.0f;
        if (dt <= 0.0f || dt > 0.5f) {
            dt = (float)TICK_MS / 1000.0f;
        }
        now = now2;
        s_led.tick_no++;

        // State auto-transitions.
        int64_t elapsed_ms = (esp_timer_get_time() - s_led.state_since_us) / 1000;
        if (s_led.state == LED_RING_STATE_WAKE && elapsed_ms >= WAKE_SNAPSHOT_MS) {
            led_ring_set_state(LED_RING_STATE_LISTENING);
        } else if (s_led.state == LED_RING_STATE_ERROR && elapsed_ms >= ERROR_HOLD_MS) {
            led_ring_set_state(s_led.prev);
        }
        // Poll the XMOS mute state occasionally while idle (cheap: 0.5 Hz).
        if (s_led.state == LED_RING_STATE_IDLE &&
            ++s_led.mute_poll >= MUTE_POLL_TICKS) {
            s_led.mute_poll = 0;
            bool muted = false;
            if (xvf3800_read_mute(&muted) == ESP_OK) {
                s_led.muted = muted;
            }
        }

        render_frame(colors, dt);
        xvf3800_set_led_ring(colors);
    }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

void led_ring_set_state(led_ring_state_t state)
{
    if (state == s_led.state) {
        return;
    }
    if (s_led.state != LED_RING_STATE_ERROR) {
        s_led.prev = s_led.state;
    }
    s_led.state = state;
    s_led.state_since_us = esp_timer_get_time();
    s_led.mute_poll = 0;
}

led_ring_state_t led_ring_get_state(void)
{
    return s_led.state;
}

void led_ring_set_beam_deg(float degrees)
{
    s_led.beam_deg = degrees;
    s_led.beam_valid = true;
}

void led_ring_clear_beam(void)
{
    s_led.beam_valid = false;
}

void led_ring_set_timer_progress(float ratio)
{
    s_led.timer_ratio = (ratio > 0.0f && ratio <= 1.0f) ? ratio : 0.0f;
}

void led_ring_set_room_connected(bool connected)
{
    s_led.room_connected = connected;
    if (!connected) {
        led_ring_set_state(LED_RING_STATE_OFF);
    } else if (s_led.state == LED_RING_STATE_OFF) {
        led_ring_set_state(LED_RING_STATE_IDLE);
    }
}

esp_err_t led_ring_init(void)
{
#if !CONFIG_LK_LEDS
    ESP_LOGI(TAG, "LED ring disabled (CONFIG_LK_LEDS=n)");
    return ESP_OK;
#else
    memset(&s_led, 0, sizeof(s_led));
    s_led.room_connected = true;
    s_led.state = LED_RING_STATE_IDLE;
    s_led.last_tick_ms = (uint32_t)(esp_timer_get_time() / 1000);
    if (xTaskCreate(led_task, "led_ring", 3072, NULL, 3, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "LED ring engine started (%d%% master, color #%06X)",
             CONFIG_LK_LED_BRIGHTNESS_PERCENT, CONFIG_LK_LED_COLOR);
    return ESP_OK;
#endif
}



