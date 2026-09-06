# NOTE: intentionally NO REQUIRES/PRIV_REQUIRES here. The ESP-IDF build
# system special-cases the main component: without explicit requirements it
# automatically requires every component in the build, which is how the
# example resolves the livekit/esp-sr/esp_capture/codec_board headers.
# Adding PRIV_REQUIRES (e.g. esp_wifi) disables that auto-require behavior
# and breaks the build (e.g. "esp_wn_iface.h: No such file or directory").
idf_component_register(SRCS "main.c" "example.c" "board.c" "media.c"
                           "wake_word.c" "wake_word_engine_esp_sr.c"
                       INCLUDE_DIRS ".")