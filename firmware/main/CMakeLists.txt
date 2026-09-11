idf_component_register(SRCS "mic_source.c" "xvf3800.c" "led_ring.c" "chime.c"
                            "wake_word.c" "local_timers.c" "voice_session.c"
                            "main.c" "example.c" "board.c" "media.c"
                       INCLUDE_DIRS "."
                       REQUIRES json espressif__esp-sr esp_lcd)
# esp_lcd is required publicly because main/chime.c includes
# av_render_default.h, whose (public) header includes esp_lcd_panel_ops.h -
# but the av_render component lists esp_lcd only as a private requirement.
