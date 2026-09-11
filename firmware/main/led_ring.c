#include "led_ring.h"

#include <math.h>
#include <string.h>

#include "esp_check.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "sdkconfig.h"
#include "xvf3800.h"

static const char *TAG = "led_ring";

// Fallback for when the feature (and thus the symbol) is disabled.
#ifndef CONFIG_LK_LED_BRIGHTNESS_PERCENT
#define CONFIG_LK_LED_BRIGHTNESS_PERCENT 40
#endif

#define TICK_MS            50
#define WAKE_SPIN_MS       700    // one-shot duration of the WAKE spin
#define ERROR_BLINK_MS     900    // total duration of the ERROR blink
#define MUTE_POLL_TICKS    40     // idle mute poll period (ticks) = 2 s

// Master brightness (0..255) applied to every rendered frame.
#define BRIGHTNESS_MAX     ((255 * CONFIG_LK_LED_BRIGHTNESS_PERCENT) / 100)

// Palette (0xRRGGBB), each scaled by the master brightness at render time.
#define COLOR_IDLE     0x483A28u   // warm white
#define COLOR_WAKE     0x0050FFu   // blue
#define COLOR_LISTEN   0x0080FFu   // cyan-blue
#define COLOR_THINK    0xFFA000u   // amber
#define COLOR_SPEAK    0x00C040u   // green
#define COLOR_TIMER    0xFF5000u   // orange
#define COLOR_MUTED    0xC00000u   // red
#define COLOR_ERROR    0xFF0000u   // red

typedef struct {
    led_ring_state_t state;
    led_ring_state_t prev;      // state to fall back to after ERROR
    int64_t state_since_us;     // timestamp of last state change
    int64_t tick_no;            // frame counter (drives animations)
    float beam_deg;             // speaker direction (valid when beam_valid)
    bool beam_valid;
    bool room_connected;
    bool muted;                 // last polled XMOS mute state
    int mute_poll;
} led_ctx_t;

static led_ctx_t s_led;

static int scale_channel(int c, int level)
{
    return (c * level) / 255;
}

static uint32_t dim(uint32_t rgb, int level)
{
    if (level < 0) {
        level = 0;
    }
    if (level > 255) {
        level = 255;
    }
    uint32_t r = scale_channel((rgb >> 16) & 0xFF, level);
    uint32_t g = scale_channel((rgb >> 8) & 0xFF, level);
    uint32_t b = scale_channel(rgb & 0xFF, level);
    return (r << 16) | (g << 8) | b;
}

// Breathing curve 0..1 with period `period_ms`.
static float breathe(int64_t tick, int period_ms)
{
    float phase = (float)((tick * TICK_MS) % period_ms) / period_ms;
    return 0.5f - 0.5f * (float)cos(2.0 * M_PI * phase);
}

// ---------------------------------------------------------------------------
// Per-state renderers (write all 12 colors into `colors`)
// ---------------------------------------------------------------------------

static void render_idle(uint32_t *colors)
{
    if (s_led.muted) {
        // Dim red ring while the DSP mic is muted.
        for (int i = 0; i < XVF3800_LED_COUNT; i++) {
            colors[i] = dim(COLOR_MUTED, 70);
        }
        return;
    }
    int level = 40 + (int)(90 * breathe(s_led.tick_no, 4000));
    for (int i = 0; i < XVF3800_LED_COUNT; i++) {
        colors[i] = dim(COLOR_IDLE, (level * BRIGHTNESS_MAX) / 255);
    }
}

static void render_wake(uint32_t *colors)
{
    int elapsed = (int)((esp_timer_get_time() - s_led.state_since_us) / 1000);
    // Two full revolutions over WAKE_SPIN_MS.
    float pos_f = (float)elapsed / (WAKE_SPIN_MS / (2.0f * XVF3800_LED_COUNT));
    int pos = (int)pos_f % XVF3800_LED_COUNT;
    for (int i = 0; i < XVF3800_LED_COUNT; i++) {
        colors[i] = dim(COLOR_IDLE, (25 * BRIGHTNESS_MAX) / 255);
    }
    colors[pos] = dim(COLOR_WAKE, BRIGHTNESS_MAX);
}

static void render_listening(uint32_t *colors)
{
    // Beam direction lit; neighbors at 35%; the rest breathes dimly.
    int base = 35 + (int)(40 * breathe(s_led.tick_no, 3000));
    for (int i = 0; i < XVF3800_LED_COUNT; i++) {
        colors[i] = dim(COLOR_LISTEN, (base * BRIGHTNESS_MAX) / 255 / 4);
    }
    if (s_led.beam_valid) {
        int idx = (int)lroundf(s_led.beam_deg / 30.0f) % XVF3800_LED_COUNT;
        if (idx < 0) {
            idx += XVF3800_LED_COUNT;
        }
        colors[idx] = dim(COLOR_LISTEN, BRIGHTNESS_MAX);
        colors[(idx + 1) % XVF3800_LED_COUNT] =
            dim(COLOR_LISTEN, (35 * BRIGHTNESS_MAX) / 255);
        colors[(idx + XVF3800_LED_COUNT - 1) % XVF3800_LED_COUNT] =
            dim(COLOR_LISTEN, (35 * BRIGHTNESS_MAX) / 255);
    } else {
        // No fresh direction: fall back to a slow breathing ring.
        int level = 30 + (int)(120 * breathe(s_led.tick_no, 2500));
        for (int i = 0; i < XVF3800_LED_COUNT; i++) {
            colors[i] = dim(COLOR_LISTEN, (level * BRIGHTNESS_MAX) / 255);
        }
    }
}

static void render_thinking(uint32_t *colors)
{
    // Comet: 3-LED head chasing around the ring.
    int head = (int)((s_led.tick_no * TICK_MS / 60) % XVF3800_LED_COUNT);
    for (int i = 0; i < XVF3800_LED_COUNT; i++) {
        colors[i] = 0;
    }
    colors[head] = dim(COLOR_THINK, BRIGHTNESS_MAX);
    colors[(head + XVF3800_LED_COUNT - 1) % XVF3800_LED_COUNT] =
        dim(COLOR_THINK, (120 * BRIGHTNESS_MAX) / 255);
    colors[(head + XVF3800_LED_COUNT - 2) % XVF3800_LED_COUNT] =
        dim(COLOR_THINK, (50 * BRIGHTNESS_MAX) / 255);
}

static void render_speaking(uint32_t *colors)
{
    int level = 45 + (int)(200 * breathe(s_led.tick_no, 1100));
    for (int i = 0; i < XVF3800_LED_COUNT; i++) {
        colors[i] = dim(COLOR_SPEAK, (level * BRIGHTNESS_MAX) / 255);
    }
}

static void render_timer(uint32_t *colors)
{
    bool on = (s_led.tick_no * TICK_MS / 250) % 2 == 0;
    for (int i = 0; i < XVF3800_LED_COUNT; i++) {
        colors[i] = on ? dim(COLOR_TIMER, BRIGHTNESS_MAX) : 0;
    }
}

static void render_error(uint32_t *colors)
{
    int elapsed = (int)((esp_timer_get_time() - s_led.state_since_us) / 1000);
    bool on = (elapsed / 150) % 2 == 0;
    for (int i = 0; i < XVF3800_LED_COUNT; i++) {
        colors[i] = on ? dim(COLOR_ERROR, BRIGHTNESS_MAX) : 0;
    }
}

// ---------------------------------------------------------------------------
// Task
// ---------------------------------------------------------------------------

static void render_frame(uint32_t *colors)
{
    for (int i = 0; i < XVF3800_LED_COUNT; i++) {
        colors[i] = 0;
    }
    switch (s_led.state) {
    case LED_RING_STATE_OFF:
        break;
    case LED_RING_STATE_IDLE:
        render_idle(colors);
        break;
    case LED_RING_STATE_WAKE:
        render_wake(colors);
        break;
    case LED_RING_STATE_LISTENING:
        render_listening(colors);
        break;
    case LED_RING_STATE_THINKING:
        render_thinking(colors);
        break;
    case LED_RING_STATE_SPEAKING:
        render_speaking(colors);
        break;
    case LED_RING_STATE_TIMER:
        render_timer(colors);
        break;
    case LED_RING_STATE_ERROR:
        render_error(colors);
        break;
    }
}

static void led_task(void *arg)
{
    uint32_t colors[XVF3800_LED_COUNT];
    for (;;) {
        s_led.tick_no++;
        vTaskDelay(pdMS_TO_TICKS(TICK_MS));

        // State auto-transitions.
        int64_t elapsed_ms = (esp_timer_get_time() - s_led.state_since_us) / 1000;
        if (s_led.state == LED_RING_STATE_WAKE && elapsed_ms >= WAKE_SPIN_MS) {
            led_ring_set_state(LED_RING_STATE_LISTENING);
        } else if (s_led.state == LED_RING_STATE_ERROR && elapsed_ms >= ERROR_BLINK_MS) {
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

        render_frame(colors);
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
    if (xTaskCreate(led_task, "led_ring", 3072, NULL, 3, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "LED ring engine started (%d%% brightness)",
             CONFIG_LK_LED_BRIGHTNESS_PERCENT);
    return ESP_OK;
#endif
}


